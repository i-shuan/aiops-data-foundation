#!/usr/bin/env bash
# metrics 路線端到端驗證：OpenTelemetry Collector → 依 namespace 的 dept 分 group → ES 的 metrics data stream。
#
# 前提：run-e2e.sh 跑過（測試 ES 與 log 的測試 namespace 都在），KUBECONFIG 指向測試 cluster。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/run-metrics-e2e.sh
# 會建立 namespace otel-collector，並在 it-mes-prod、imc-wip-prod、legacy 各部署一個 metrics-app。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
KCTX="${KCTX:-default}"
k() { kubectl --context "${KCTX}" "$@"; }

fail=0
check() {  # check <說明> <實際> <預期>
  if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi
}
check_gt0() {
  if [[ "$2" -gt 0 ]]; then echo "  PASS  $1（$2）"; else echo "  FAIL  $1：得到 $2"; fail=1; fi
}
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }

echo "== 1. API key（正式腳本）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/platform/central/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
IT_KEY="$(key_of '寫入 key：es-writer-it')"
IMC_KEY="$(key_of '寫入 key：es-writer-imc')"
UNASSIGNED_KEY="$(key_of '寫入 key：es-writer-unassigned')"
AIOPS_KEY="$(key_of 'AIOps 唯讀 key')"
for key in "${IT_KEY}" "${IMC_KEY}" "${UNASSIGNED_KEY}" "${AIOPS_KEY}"; do
  [[ -n "${key}" && "${key}" != null ]] || { echo "沒拿到 API key："; echo "${setup_out}"; exit 1; }
done
# log 與 metrics 用同一組 key，都由 otel-collector 送
k create namespace otel-collector --dry-run=client -o yaml | k apply -f - >/dev/null
k -n otel-collector create secret generic es-writer-keys \
  --from-literal=IT_ES_API_KEY="${IT_KEY}" \
  --from-literal=IMC_ES_API_KEY="${IMC_KEY}" \
  --from-literal=UNASSIGNED_ES_API_KEY="${UNASSIGNED_KEY}" \
  --dry-run=client -o yaml | k apply -f - >/dev/null

echo "== 2. Collector 與測試應用"
k apply -k "${here}/otel-collector" >/dev/null
k apply -f "${here}/metrics-apps.yaml" >/dev/null
k -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
k -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s
k -n otel-collector rollout status deployment/otel-cluster-collector --timeout=180s
for ns in it-mes-prod imc-wip-prod legacy; do k -n "${ns}" rollout status deployment/metrics-app --timeout=180s >/dev/null; done
start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "   只看 ${start} 之後的資料，等 Collector 送兩輪（約 90 秒）"
sleep 90
es -X POST "${ES_URL}/metrics-*.otel-*/_refresh" >/dev/null

since="{\"range\":{\"@timestamp\":{\"gte\":\"${start}\"}}}"
count() {  # count <index> <額外的 filter（JSON 陣列內容，可空）>
  es "${ES_URL}/$1/_count" -d "{\"query\":{\"bool\":{\"filter\":[${since}${2:+,$2}]}}}" | jq -r '.count // 0'
}
ns_f() { echo "{\"term\":{\"resource.attributes.k8s.namespace.name\":\"$1\"}}"; }
not_dept() {  # 這個 index 裡 dept 不在清單內（或沒有 dept）的筆數
  es "${ES_URL}/$1/_count" -d "{\"query\":{\"bool\":{\"filter\":[${since}],\"must_not\":[{\"terms\":{\"resource.attributes.dept\":$2}}]}}}" | jq -r '.count // 0'
}

echo "== 3. 分流"
for src in kubeletstatsreceiver k8sclusterreceiver prometheusreceiver; do
  check_gt0 "${src}：it-mes-prod 進 IT"          "$(count "metrics-${src}.otel-it" "$(ns_f it-mes-prod)")"
  check_gt0 "${src}：imc-wip-prod 進 IMC"        "$(count "metrics-${src}.otel-imc" "$(ns_f imc-wip-prod)")"
  check_gt0 "${src}：legacy（沒標 dept）進 unassigned" "$(count "metrics-${src}.otel-unassigned" "$(ns_f legacy)")"
  check "${src}：IT 裡沒有別的部門"              "$(not_dept "metrics-${src}.otel-it" '["D3120","D3130"]')" 0
  check "${src}：IMC 裡沒有別的部門"             "$(not_dept "metrics-${src}.otel-imc" '["D1110","D1120"]')" 0
  check "${src}：IT、IMC 的資料沒有落到 unassigned" \
    "$(count "metrics-${src}.otel-unassigned" '{"terms":{"resource.attributes.dept":["D3120","D3130","D1110","D1120"]}}')" 0
done
check_gt0 "Pod 的 CPU（kubelet 抓的）依 Pod 的 namespace 進 IT" \
  "$(count metrics-kubeletstatsreceiver.otel-it '{"exists":{"field":"metrics.k8s.pod.cpu.usage"}}')"
check_gt0 "Deployment 的可用副本數（沒有 Pod 的物件）進 IT" \
  "$(count metrics-k8sclusterreceiver.otel-it '{"exists":{"field":"metrics.k8s.deployment.available"}}')"
check_gt0 "節點本身的資料（沒有 namespace）進 unassigned" \
  "$(es "${ES_URL}/metrics-kubeletstatsreceiver.otel-unassigned/_count" -d "{\"query\":{\"bool\":{\"filter\":[${since},{\"exists\":{\"field\":\"metrics.k8s.node.cpu.usage\"}}]}}}" | jq -r .count)"

echo "== 4. 欄位"
doc="$(es "${ES_URL}/metrics-prometheusreceiver.otel-it/_search" -d "{\"size\":1,\"query\":{\"bool\":{\"filter\":[${since},{\"exists\":{\"field\":\"metrics.demo_requests_total\"}}]}}}" | jq '.hits.hits[0]._source')"
check "service.name（來自 Pod label，不是抓取工作名稱）" "$(jq -r '.resource.attributes["service.name"]' <<<"${doc}")" metrics-app
check "service.version"                 "$(jq -r '.resource.attributes["service.version"]' <<<"${doc}")" 1.2.3
check "service.namespace（part-of）"    "$(jq -r '.resource.attributes["service.namespace"]' <<<"${doc}")" demo
check "group"                           "$(jq -r '.resource.attributes.group' <<<"${doc}")" it
check "dept（來自 namespace label）"    "$(jq -r '.resource.attributes.dept' <<<"${doc}")" D3120
check "k8s.cluster.name"                "$(jq -r '.resource.attributes["k8s.cluster.name"]' <<<"${doc}")" local
check "應用自稱的 dept、group 被拿掉"    "$(jq -r '.attributes | has("dept") or has("group")' <<<"${doc}")" false
check "應用自己的 label 保留（path）"    "$(jq -r '.attributes.path' <<<"${doc}")" /orders
check "存成 time series data stream" \
  "$(es "${ES_URL}/metrics-prometheusreceiver.otel-it/_settings" | jq -r '[.[].settings.index.mode] | unique | join(",")')" time_series

check "TSDS 可寫入時間往後 2 小時（ES 停機重啟後才不會拒收）" \
  "$(es "${ES_URL}/metrics-prometheusreceiver.otel-it/_settings?include_defaults=true" | jq -r '[.[] | (.settings.index.look_ahead_time // .defaults.index.look_ahead_time)] | last')" 2h

echo "== 5. 權限"
can() {  # can <api key> <index> <privilege> → true / false
  curl -sS -m 30 -H "Authorization: ApiKey $1" -H 'Content-Type: application/json' "${ES_URL}/_security/user/_has_privileges" \
    -d "{\"index\":[{\"names\":[\"$2\"],\"privileges\":[\"$3\"]}]}" | jq -r ".index[\"$2\"][\"$3\"]"
}
check "IT 的 key 可以寫 IT 的 metrics"       "$(can "${IT_KEY}" metrics-x.otel-it create_doc)" true
check "IT 的 key 不能寫 IMC 的 metrics"      "$(can "${IT_KEY}" metrics-x.otel-imc create_doc)" false
check "IT 的 key 不能讀 metrics"             "$(can "${IT_KEY}" metrics-x.otel-it read)" false
check "AIOps 唯讀 key 可以讀全部 metrics"    "$(can "${AIOPS_KEY}" metrics-x.otel-imc read)" true
check "AIOps 唯讀 key 不能寫 metrics"        "$(can "${AIOPS_KEY}" metrics-x.otel-it create_doc)" false
check "IT 的唯讀角色看得到 IT 的 metrics"     "$(es "${ES_URL}/_security/role/logs-reader-it" | jq -r '.["logs-reader-it"].indices[0].names | index("metrics-*-it") != null')" true

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
