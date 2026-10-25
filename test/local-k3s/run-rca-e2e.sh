#!/usr/bin/env bash
# 根因分析（aiops/rca）端到端驗證：Metrics 發現異常 → Trace 縮小到哪一段 → Log 找出原因。
#
# 兩個模擬服務（rca-apps.yaml）：rca-mes-api → rca-wip-worker → Oracle，資料走真正的路線（Collector → ES）。
#   1. 正常跑 7 分鐘（當「平常」）
#   2. 製造事故 5 分鐘：wip-worker 查 Oracle 從 30ms 變 1.5 秒，印出 connection pool timeout，約 1/4 的請求失敗
#   3. 模擬 baseline 在事故開始時記到一筆 rca-mes-api 延遲異常（寫進測試用的 aiops-anomalies-rcatest）
#   4. 不指定服務跑 investigate：要從那筆異常找到 rca-mes-api，再一路查到 Oracle 和那行 log
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector、測試 namespace 都在）。約 14 分鐘。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/run-rca-e2e.sh      （KEEP=1：跑完不刪模擬服務）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
has() {  # has <說明> <固定字串>：報告裡有這段
  if grep -qF -- "$2" "${report}"; then echo "  PASS  $1"; else echo "  FAIL  $1：報告裡沒有 [$2]"; fail=1; fi
}
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
wip() { kubectl -n imc-wip-prod exec deploy/rca-wip-worker -- sh -c "$1"; }

echo "== 1. API key（正式腳本；分析用 baseline 的 key：能讀 log、trace、aiops-*）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/platform/central/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY="$(key_of '寫入 key：es-writer')" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
BASELINE_KEY="$(key_of 'AIOps baseline key')"

echo "== 2. 模擬服務"
kubectl apply -k "${here}/otel-collector" >/dev/null
kubectl -n otel-collector rollout restart daemonset/otel-node-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
for ns in it-mes-prod imc-wip-prod; do
  kubectl -n "${ns}" create configmap rca-lib --from-file=lib.sh="${here}/rca-lib.sh" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
kubectl apply -f "${here}/rca-apps.yaml" >/dev/null
# 重新開，確保沒有上次留下的事故開關
kubectl -n imc-wip-prod rollout restart deployment/rca-wip-worker >/dev/null
kubectl -n it-mes-prod rollout restart deployment/rca-mes-api >/dev/null
kubectl -n imc-wip-prod rollout status deployment/rca-wip-worker --timeout=180s >/dev/null
kubectl -n it-mes-prod rollout status deployment/rca-mes-api --timeout=180s >/dev/null

echo "== 3. 正常跑 7 分鐘"
sleep 420
t1=$(date +%s)
echo "== 4. $(iso "${t1}") 製造事故：查 Oracle 變 1.5 秒、connection pool timeout，跑 5 分鐘"
wip 'echo 1.5 > /tmp/db_delay; touch /tmp/slow'
sleep 300
t2=$(date +%s)
wip 'rm -f /tmp/db_delay /tmp/slow'
echo "   $(iso "${t2}") 事故結束"

echo "== 5. 模擬 baseline 記到的異常：$(iso "${t1}") rca-mes-api GET /lots/{id} 延遲 p95"
es -X DELETE "${ES_URL}/aiops-anomalies-rcatest" >/dev/null
es -X PUT "${ES_URL}/aiops-anomalies-rcatest" -d '{"mappings":{"dynamic":false,"properties":{
  "@timestamp":{"type":"date"},"series":{"type":"keyword"},"keys":{"type":"flattened"},"value":{"type":"double"},
  "median":{"type":"double"},"z":{"type":"double"}}}}' >/dev/null
es -X POST "${ES_URL}/aiops-anomalies-rcatest/_doc?refresh=true" -d "{\"@timestamp\":\"$(iso "${t1}")\",
  \"series\":\"http.latency_p95_s\",\"value\":1.55,\"median\":0.08,\"expected_low\":0.07,\"expected_high\":0.1,\"z\":41.2,
  \"level\":\"dow_hour\",\"direction\":\"up\",\"keys\":{\"resource.attributes.service.name\":\"rca-mes-api\",
  \"attributes.http.request.method\":\"GET\",\"attributes.http.route\":\"/lots/{id}\"}}" >/dev/null
sleep 20   # 等最後的 span、log 寫進 ES
es -X POST "${ES_URL}/traces-*.otel-*,k8s-*/_refresh" >/dev/null

echo "== 6. investigate（不指定服務，從異常開始）"
report="$(mktemp)"
(cd "${repo}/aiops" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" \
  python3 -m aiops_rca investigate --config tests/rca_k3s.yaml --at "$(iso "${t2}")" --window 15m --out "${report}" >/dev/null)
echo "   報告：${report}"
has "從異常找到服務與 API"                 "rca-mes-api（GET /lots/{id}）"
has "列出那筆異常（Metrics）"              '`http.latency_p95_s` |  | 1.55 | 0.08 | 41.2 倍'
has "「平常」用出事前那一段（一週前沒資料）" "平常（出事前一段時間）"
has "瓶頸：Oracle 那一段（Trace）"          "主要多在 **rca-wip-worker › SELECT LOT_HISTORY → oracle**"
has "錯誤源頭：Oracle 那一段，不是上層"      "錯誤最早出在 **rca-wip-worker › SELECT LOT_HISTORY → oracle**"
has "新出現的 log 樣式（Log）"               "🆕 \`Connection pool timeout waiting for connection to <ip> after <n>ms"
check "最慢那個請求的 log：兩個服務都撈到（trace_id 串起來）" \
  "$(sed -n '/最慢那個請求的 log/,/^## /p' "${report}" | grep -oE '^20[^ ]+  rca-(mes-api|wip-worker)' | awk '{print $2}' | sort -u | paste -sd,)" \
  "rca-mes-api,rca-wip-worker"
has "呼叫關係：mes-api → wip-worker"         'rca_mes_api["rca-mes-api"] -->'
has "呼叫關係：wip-worker → oracle"          'oracle["oracle"]'

echo "== 7. topology"
topo="$(cd "${repo}/aiops" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" \
  python3 -m aiops_rca topology --config tests/rca_k3s.yaml --at "$(iso "${t2}")" --window 15m)"
check "rca-mes-api → rca-wip-worker"  "$(grep -c '^| rca-mes-api | rca-wip-worker |' <<<"${topo}")" 1
check "rca-wip-worker → oracle"       "$(grep -c '^| rca-wip-worker | oracle |' <<<"${topo}")" 1

es -X DELETE "${ES_URL}/aiops-anomalies-rcatest" >/dev/null
if [[ -z "${KEEP:-}" ]]; then
  kubectl delete -f "${here}/rca-apps.yaml" --wait=false >/dev/null
  for ns in it-mes-prod imc-wip-prod; do kubectl -n "${ns}" delete configmap rca-lib --wait=false >/dev/null; done
fi

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
