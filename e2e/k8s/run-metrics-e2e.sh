#!/usr/bin/env bash
# metrics 路線端到端驗證：OpenTelemetry Collector → 依 namespace 的 dept 分 group → ES 的 metrics data stream。
#
# 前提：run-e2e.sh 跑過（測試 ES 與 log 的測試 namespace 都在），KUBECONFIG 指向測試 cluster。
# 用法：ES_URL=http://<ip>:9200 e2e/k8s/run-metrics-e2e.sh
# 會建立 namespace otel-collector，並在 it-mes-prod、imc-wip-prod、legacy 各部署一個 metrics-app，imc-wip-prod 再加一個 http-metrics-app（模擬 Java agent 的請求直方圖）。
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
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/elasticsearch/code/scripts/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
WRITER_KEY="$(key_of '寫入 key：es-writer')"
AIOPS_KEY="$(key_of 'AIOps 唯讀 key')"
for key in "${WRITER_KEY}" "${AIOPS_KEY}"; do
  [[ -n "${key}" && "${key}" != null ]] || { echo "沒拿到 API key："; echo "${setup_out}"; exit 1; }
done
# log、metrics、trace 用同一把 key，都由 otel-collector 送
k create namespace otel-collector --dry-run=client -o yaml | k apply -f - >/dev/null
k -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY="${WRITER_KEY}" \
  --dry-run=client -o yaml | k apply -f - >/dev/null

echo "== 2. Collector 與測試應用"
KCTX="${KCTX}" "${here}/deploy-collector.sh"
k apply -f "${here}/metrics-apps.yaml" >/dev/null
k apply -f "${here}/http-metrics-app.yaml" >/dev/null
k -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
k -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s
k -n otel-collector rollout status deployment/otel-cluster-collector --timeout=180s
for ns in it-mes-prod imc-wip-prod legacy; do k -n "${ns}" rollout status deployment/metrics-app --timeout=180s >/dev/null; done
k -n imc-wip-prod rollout status deployment/http-metrics-app --timeout=180s >/dev/null
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

echo "== 5. 應用的請求直方圖（AIOps baseline 算流量、錯誤率、延遲用）"
H=metrics.http.server.request.duration   # Java 的 Prometheus 名字由 node Collector 改成 OTel 名字（node.yaml metricstransform/java-http）
hq="{\"bool\":{\"filter\":[${since},{\"exists\":{\"field\":\"${H}\"}},$(ns_f imc-wip-prod)]}}"
hdocs="$(es "${ES_URL}/metrics-*.otel-imc/_search" -d "{\"size\":100,\"query\":${hq}}" | jq '[.hits.hits[]._source]')"
check_gt0 "進 IMC（metrics-generic.otel-imc）" "$(jq length <<<"${hdocs}")"
check "直方圖的資料流不降解析度（ILM metrics-hist-otel；ES 8.19 的 histogram 降了會錯）" \
  "$(es "${ES_URL}/metrics-generic.otel-imc/_ilm/explain" | jq -r '[.indices[].policy] | unique | join(",")')" metrics-hist-otel
check "存成直方圖（histogram）" \
  "$(es "${ES_URL}/metrics-generic.otel-imc/_mapping/field/${H}" | jq -r '[.[].mappings[].mapping[].type] | unique | join(",")')" histogram
# ES 8.19 的 histogram 不存 temporality，差值只能從數量看：測試應用每 5 秒 20 個成功的請求，Collector 30 秒抓一次 → 每筆約 120 個。
# 累加值的話會一直變大。_source 是 {"values": [...], "counts": [...]}，counts 加起來是請求數
ok_n="$(jq '[.[] | select(.attributes["http.response.status_code"] == 200) | .metrics["http.server.request.duration"].counts | add // 0]' <<<"${hdocs}")"
check_gt0 "讀得到每筆的請求數（counts 加總）" "$(jq 'max // 0' <<<"${ok_n}")"
check "每筆只有那 30 秒的量（成功的請求 ≤ 140 個）" "$(jq 'max <= 140' <<<"${ok_n}")" true
# histogram 不存 sum；Collector（transform/hist-sum）把每個直方圖的總和另存成 <名稱>_sum，GC 時間比例的規則靠這個
check_gt0 "直方圖的總和另存成 _sum（跟直方圖同一份文件）" \
  "$(jq '[.[] | select(.metrics["http.server.request.duration_sum"] != null)] | length' <<<"${hdocs}")"
agg="$(es "${ES_URL}/metrics-*.otel-imc/_search" -d "{\"size\":0,\"query\":${hq},\"aggs\":{
  \"n\":{\"value_count\":{\"field\":\"${H}\"}},\"p\":{\"percentiles\":{\"field\":\"${H}\",\"percents\":[95]}},
  \"e\":{\"filter\":{\"range\":{\"attributes.http.response.status_code\":{\"gte\":500}}},\"aggs\":{\"n\":{\"value_count\":{\"field\":\"${H}\"}}}}}}")"
check "錯誤率算得出來（應該約 1/21 ≈ 4.8%）" "$(jq '(.aggregations.e.n.value / .aggregations.n.value) as $r | $r > 0.04 and $r < 0.056' <<<"${agg}")" true
check "p95 算得出來（應該落在 0.25～0.5 秒）" "$(jq '.aggregations.p.values["95.0"] as $p | $p > 0.25 and $p <= 0.5' <<<"${agg}")" true

echo "== 6. 權限"
can() {  # can <api key> <index> <privilege> → true / false
  curl -sS -m 30 -H "Authorization: ApiKey $1" -H 'Content-Type: application/json' "${ES_URL}/_security/user/_has_privileges" \
    -d "{\"index\":[{\"names\":[\"$2\"],\"privileges\":[\"$3\"]}]}" | jq -r ".index[\"$2\"][\"$3\"]"
}
check "寫入 key 可以寫 IT 的 metrics"         "$(can "${WRITER_KEY}" metrics-x.otel-it create_doc)" true
check "寫入 key 可以寫 IMC 的 metrics"        "$(can "${WRITER_KEY}" metrics-x.otel-imc create_doc)" true
check "寫入 key 不能讀 metrics"               "$(can "${WRITER_KEY}" metrics-x.otel-it read)" false
check "寫入 key 不能寫 aiops-*（只給 baseline 工具）" "$(can "${WRITER_KEY}" aiops-anomalies create_doc)" false
check "AIOps 唯讀 key 可以讀全部 metrics"    "$(can "${AIOPS_KEY}" metrics-x.otel-imc read)" true
check "AIOps 唯讀 key 不能寫 metrics"        "$(can "${AIOPS_KEY}" metrics-x.otel-it create_doc)" false
check "IT 的唯讀角色看得到 IT 的 metrics"     "$(es "${ES_URL}/_security/role/logs-reader-it" | jq -r '.["logs-reader-it"].indices[0].names | index("metrics-*-it") != null')" true

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
