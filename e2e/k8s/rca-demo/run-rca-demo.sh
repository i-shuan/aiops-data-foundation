#!/usr/bin/env bash
# 從應用 Pod 連續送 metrics、trace、log（三種用 trace_id 串起來）→ 真的偵測 → 合成一件 → 根因分析＋LLM → Grafana 告警與儀表板。
#
# 跟 run-rca-e2e.sh 的差別：那支直接塞一筆異常進 ES；這支的異常是 detector rules 從 metrics 真的算出來的
# （rca-mes-api 在 :9464/metrics 開 HTTP 直方圖，rca-apps.yaml）。
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector、測試 namespace 都在），ES 連得到（例如 kubectl port-forward）。
# 用法（ES_URL 預設 http://127.0.0.1:29200）：
#   run-rca-demo.sh up          發 key、部署模擬服務與 Grafana（aiops-demo）
#   run-rca-demo.sh incident    製造事故：wip-worker 查 Oracle 變 1.5 秒、connection pool timeout
#   run-rca-demo.sh heal        事故結束
#   run-rca-demo.sh tick        跑一輪：detector trace-merge → rules → investigator alert（正式是排程器每分鐘）
#   run-rca-demo.sh loop <分鐘>  每分鐘 tick 一次
# LLM：LLM_ENV_FILE=<.env 路徑> 只載入 LLM_PROVIDER、LLM_API_KEY、LLM_CHAT_MODEL、OPENAI_BASE_URL；沒給就用 template。
# 發的 key 存在 RCA_DEMO_STATE（預設 ~/.rca-demo），不進 repo。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../../.." && pwd)"
ES_URL="${ES_URL:-http://127.0.0.1:29200}"
ES_PASSWORD=changeme-test-only
STATE="${RCA_DEMO_STATE:-${HOME}/.rca-demo}"
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
key() {  # key <名稱> <role_descriptors JSON>：發一把 API key，印出 encoded
  es -X POST "${ES_URL}/_security/api_key" -d "{\"name\":\"$1\",\"role_descriptors\":{\"$1\":$2}}" | jq -r .encoded
}
wip() { kubectl -n imc-wip-prod exec deploy/rca-wip-worker -- sh -c "$1"; }

load_llm() {
  [[ -n "${LLM_ENV_FILE:-}" ]] || { export LLM_PROVIDER="${LLM_PROVIDER:-template}"; return; }
  local k v
  while IFS='=' read -r k v || [[ -n "$k" ]]; do
    case "$k" in
      LLM_PROVIDER|LLM_API_KEY|LLM_CHAT_MODEL|OPENAI_BASE_URL)
        v="${v%$'\r'}"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; export "$k=$v" ;;
    esac
  done < "${LLM_ENV_FILE}"
}

up() {
  mkdir -p "${STATE}"
  echo "== 1. key（權限跟 elasticsearch/code/scripts/elasticsearch-setup.sh 一樣）"
  # engine：AIOps baseline key（讀三種資料、寫 aiops-*）
  key aiops-baseline '{"index":[{"names":["k8s-*","metrics-*.otel-*","traces-*.otel-*"],"privileges":["read","view_index_metadata","read_failure_store"]},{"names":["aiops-*","traces-merged-*"],"privileges":["read","write","create_index","view_index_metadata"]}]}' > "${STATE}/engine-key"
  # Grafana 告警：只讀 aiops-alerts（正式的權限）
  alerts_key="$(key grafana-aiops-alerts '{"cluster":["monitor"],"index":[{"names":["aiops-alerts"],"privileges":["read","view_index_metadata"]}]}')"
  # Grafana 儀表板（demo 才有）：多讀 metrics、trace、log
  demo_key="$(key grafana-rca-demo '{"cluster":["monitor"],"index":[{"names":["k8s-*","metrics-*.otel-*","traces-*.otel-*","aiops-alerts"],"privileges":["read","view_index_metadata"]}]}')"

  echo "== 2. 模擬服務（rca-apps.yaml）"
  # Windows 上 checkout 成 CRLF 的話 busybox sh 跑不了：轉成 LF 再建 ConfigMap
  tr -d '\r' < "${repo}/e2e/k8s/rca-lib.sh" > "${STATE}/rca-lib.sh"
  for ns in it-mes-prod imc-wip-prod; do
    kubectl -n "${ns}" create configmap rca-lib --from-file=lib.sh="${STATE}/rca-lib.sh" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  done
  kubectl apply -f "${repo}/e2e/k8s/rca-apps.yaml" >/dev/null
  kubectl -n imc-wip-prod rollout status deployment/rca-wip-worker --timeout=180s >/dev/null
  kubectl -n it-mes-prod rollout status deployment/rca-mes-api --timeout=180s >/dev/null

  echo "== 3. Grafana（aiops-demo）"
  kubectl apply -f "${here}/notify-sink.yaml" >/dev/null
  local sink=http://notify-sink.aiops-demo.svc:8000
  kubectl -n aiops-demo create secret generic grafana-env \
    --from-literal=ES_URL=http://elasticsearch.es-test.svc:9200 \
    --from-literal=GRAFANA_ES_API_KEY="${alerts_key}" --from-literal=GRAFANA_DEMO_API_KEY="${demo_key}" \
    --from-literal=NOTIFY_IT_URL="${sink}/it" --from-literal=NOTIFY_IMC_URL="${sink}/imc" --from-literal=NOTIFY_PLATFORM_URL="${sink}/platform" \
    --from-literal=GF_SECURITY_ADMIN_PASSWORD="${ES_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  cm() { local n="$1"; shift; kubectl -n aiops-demo create configmap "${n}" "$@" --dry-run=client -o yaml | kubectl apply -f - >/dev/null; }
  local g="${repo}/grafana/code"
  cm notify-sink --from-file=webhook.py="${here}/webhook.py"
  # Grafana：正式的 chart＋demo 的 values；告警設定從 grafana/code 帶，demo 的 data source 與儀表板從這個資料夾帶
  # Windows 的 helm 看不懂 Git Bash 的 /c/... 路徑，先轉成 C:/...
  local gw hw; gw="$(cygpath -m "${g}" 2>/dev/null || echo "${g}")"; hw="$(cygpath -m "${here}" 2>/dev/null || echo "${here}")"
  helm upgrade --install grafana "$(cygpath -m "${repo}/grafana/helmchart" 2>/dev/null || echo "${repo}/grafana/helmchart")" -n aiops-demo \
    -f "$(cygpath -m "${repo}/grafana/helmchart/values-test.yaml" 2>/dev/null || echo "${repo}/grafana/helmchart/values-test.yaml")" \
    --set-file "provisioning.datasources.aiops-alerts=${gw}/datasource.yaml,provisioning.datasources.demo=${hw}/datasources.yaml" \
    --set-file "provisioning.alerting.alert-rules=${gw}/alert-rules.yaml,provisioning.alerting.contact-points=${gw}/contact-points.yaml,provisioning.alerting.policies=${gw}/policies.yaml" \
    --set-file "provisioning.dashboards.rca-demo=${hw}/dashboard.json" >/dev/null
  kubectl -n aiops-demo rollout restart deployment/notify-sink >/dev/null
  kubectl -n aiops-demo rollout status deployment/grafana --timeout=180s >/dev/null
  kubectl -n aiops-demo rollout status deployment/notify-sink --timeout=180s >/dev/null
  echo "   Grafana：http://localhost:3300（不登入可看；改設定用 admin／${ES_PASSWORD}）"
  echo "   通知：kubectl -n aiops-demo logs -f deploy/notify-sink"
}

tick() {
  load_llm
  export ES_URL ES_API_KEY; ES_API_KEY="$(cat "${STATE}/engine-key")"
  (cd "${repo}/aiops-engine/code"
   python3 -m detector trace-merge --config configs/baseline/trace.yaml | sed 's/^/   [trace-merge] /'
   python3 -m detector rules --config configs/baseline/rules.yaml | sed 's/^/   [rules] /'
   python3 -m investigator alert --config configs/rca/rca.yaml | sed 's/^/   [alert] /')
}

case "${1:-}" in
  up) up ;;
  incident) wip 'echo 1.5 > /tmp/db_delay; touch /tmp/slow'; echo "$(date -u +%H:%M:%SZ) 事故開始" ;;
  heal) wip 'rm -f /tmp/db_delay /tmp/slow'; echo "$(date -u +%H:%M:%SZ) 事故結束" ;;
  tick) echo "== $(date -u +%H:%M:%SZ) tick"; tick ;;
  loop) for ((m = 0; m < ${2:?分鐘}; m++)); do echo "== $(date -u +%H:%M:%SZ) tick"; tick; sleep 60; done ;;
  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
