#!/usr/bin/env bash
# Kafka 的 trace 端到端驗證（docs/aiops-trace.md 場景四）：真的 Kafka ＋ 兩個 .NET 8 服務，只掛自動埋點、不改程式（apps.yaml）。
#
#   串接：生產者 HTTP 請求 → publish 同一個 trace；
#         消費者：receive 是新的 trace、只用 link 指回 publish；處理時寫 DB 的 span 自成一個 trace（串不回訊息）
#   指標：真的 kafka_exporter 的名稱（kafka_consumergroup_lag_sum{consumergroup, topic}），跟 rules.yaml 一樣
#   baseline：正常跑 15 分鐘學平常（測試用 1 分鐘一點）→ 正常 3 分鐘不能誤報 →
#     消費者變慢（每筆多 150ms，跟不上每秒 10 筆）：「排隊」那段時間變長（從 link 找生產者算）、lag 一直漲
#     寫 DB 失敗（程式 catch 掉、記 error log）：消費者自成一個 trace 的 postgres 那段錯誤率
#
# 前提：run-e2e.sh 跑過（ES、Collector）。約 40 分鐘。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/kafka/run-kafka-e2e.sh      （KEEP=1：跑完不刪）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
ns=kafka-cases
fail=0
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
ok() { if [[ "$2" == true ]]; then echo "  PASS  $1${3:+（$3）}"; else echo "  FAIL  $1${3:+（$3）}"; fail=1; fi; }
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
ctl() { kubectl -n "${ns}" exec "deploy/$1" -c app -- sh -c "$2"; }
cfg=tests/trace_k3s.yaml
aiops() { (cd "${repo}/aiops" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" python3 -m aiops_baseline "$@"); }
SVCS='["order-producer","order-consumer"]'
# anomalies <開始秒> <結束秒>：這段時間兩個服務 trace 每一段的異常，一行一個「數字|服務|段」
anomalies() {
  es -X POST "${ES_URL}/aiops-anomalies/_refresh" >/dev/null
  es "${ES_URL}/aiops-anomalies/_search" -d "{\"size\":1000,\"query\":{\"bool\":{\"filter\":[
    {\"prefix\":{\"series\":\"trace.segment.\"}},{\"range\":{\"@timestamp\":{\"gte\":\"$(iso "$1")\",\"lt\":\"$(iso "$2")\"}}},
    {\"terms\":{\"keys.entry.service\":${SVCS}}}]}}}" \
    | jq -r '.hits.hits[]._source | "\(.series | sub("trace.segment.";""))|\(.keys["entry.service"])|\(.keys.segment)"' | sort -u
}
lag() {  # lag <group>：最近一分鐘的消費落後
  es "${ES_URL}/metrics-*/_search?size=1&sort=@timestamp:desc" -d "{\"query\":{\"bool\":{\"filter\":[
    {\"term\":{\"attributes.consumergroup\":\"$1\"}},{\"exists\":{\"field\":\"metrics.kafka_consumergroup_lag_sum\"}}]}}}" \
    | jq '.hits.hits[0]._source.metrics.kafka_consumergroup_lag_sum // -1'
}

echo "== 1. API key、Collector"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/platform/central/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=IT_ES_API_KEY="$(key_of '寫入 key：es-writer-it')" \
  --from-literal=IMC_ES_API_KEY="$(key_of '寫入 key：es-writer-imc')" \
  --from-literal=UNASSIGNED_ES_API_KEY="$(key_of '寫入 key：es-writer-unassigned')" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
BASELINE_KEY="$(key_of 'AIOps baseline key')"
kubectl apply -k "${here}/../otel-collector" >/dev/null
kubectl -n otel-collector rollout restart daemonset/otel-node-collector statefulset/trace-sampler >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
kubectl -n otel-collector rollout status statefulset/trace-sampler --timeout=180s >/dev/null

echo "== 2. Kafka、兩個 .NET 服務（namespace ${ns}，第一次要編譯幾分鐘）"
kubectl delete namespace "${ns}" --ignore-not-found --wait=true >/dev/null
kubectl create namespace "${ns}" >/dev/null
kubectl label namespace "${ns}" dept=D1110 >/dev/null
kubectl -n "${ns}" create configmap kafka-app-src --from-file="${here}/app/Program.cs" --from-file="${here}/app/app.csproj" >/dev/null
[[ -f /root/.ccr/ca-bundle.crt ]] && kubectl -n "${ns}" create configmap proxy-ca --from-file=ca-bundle.crt=/root/.ccr/ca-bundle.crt >/dev/null
kubectl apply -f "${here}/kafka.yaml" >/dev/null
kubectl -n "${ns}" rollout status deploy/kafka --timeout=300s >/dev/null
kubectl apply -f "${here}/apps.yaml" >/dev/null
for d in order-producer order-consumer kafka-exporter; do kubectl -n "${ns}" rollout status "deploy/${d}" --timeout=1200s >/dev/null; done
echo "   起來了，跑 4 分鐘（消費者第一次從頭讀，先等它追上）"
sleep 240
es -X POST "${ES_URL}/traces-*,metrics-*/_refresh" >/dev/null

echo "== 3. 串接"
# 拿 30 秒以前的：最新的那幾筆，對方那一段可能還在 trace-sampler 裡等著決定、還沒寫進 ES
q() { es "${ES_URL}/traces-*/_search?size=1&sort=@timestamp:desc" -d "{\"query\":{\"bool\":{\"filter\":[$1,{\"range\":{\"@timestamp\":{\"gte\":\"now-3m\",\"lte\":\"now-30s\"}}}]}}}"; }
pub="$(q '{"term":{"name":"orders publish"}}' | jq -c '.hits.hits[0]._source')"
check "生產者：publish 接在 HTTP 請求底下（同一個 trace）" \
  "$(es "${ES_URL}/traces-*/_search?size=1" -d "{\"query\":{\"term\":{\"span_id\":$(jq '.parent_span_id' <<<"${pub}")}}}" | jq -r '.hits.hits[0]._source.kind')" "Server"
recv="$(q '{"term":{"resource.attributes.service.name":"order-consumer"}},{"term":{"name":"orders receive"}}' | jq -c '.hits.hits[0]._source')"
link="$(jq -r '.links[0].span_id // empty' <<<"${recv}")"
ok "消費者：receive 是新的 trace（沒有上一層）" "$(jq '.parent_span_id == null' <<<"${recv}")"
check "消費者：receive 用 link 指回生產者的 publish" \
  "$(es "${ES_URL}/traces-*/_search?size=1" -d "{\"query\":{\"term\":{\"span_id\":\"${link}\"}}}" | jq -r '.hits.hits[0]._source.name')" "orders publish"
orphan="$(es "${ES_URL}/traces-*/_search?size=0" -d '{"query":{"bool":{"filter":[{"term":{"resource.attributes.service.name":"order-consumer"}},{"term":{"name":"postgres"}},{"range":{"@timestamp":{"gte":"now-2m"}}}]}},"aggs":{"root":{"missing":{"field":"parent_span_id"}}}}' | jq '[.hits.total.value, .aggregations.root.doc_count]')"
ok "消費者：處理時寫 DB 的 span 每一筆都自成一個 trace（串不回訊息）" "$(jq '.[0] > 0 and .[0] == .[1]' <<<"${orphan}")" "$(jq -r '"\(.[1])／\(.[0]) 筆沒有上一層"' <<<"${orphan}")"
ok "kafka_exporter 的名稱：kafka_consumergroup_lag_sum{consumergroup, topic}（跟 rules.yaml 一樣）" \
  "$( [[ $(lag order-push) != -1 ]] && echo true)" "order-push 落後 $(lag order-push) 筆"

echo "== 4. 正常跑 15 分鐘，學出平常"
sleep 900
t_end=$(date +%s); t_start=$(( t_end - 1080 ))
aiops trace-collect --config "${cfg}" --start "$(iso "${t_start}")" --end "$(iso "${t_end}")" | tail -1
aiops trace-train --config "${cfg}" --history 18m --end "$(iso "${t_end}")" | grep -E "calls|time_ms" | head -2
echo "   學到的消費者（平常幾毫秒）："
es "${ES_URL}/aiops-baseline/_search?size=10" -d '{"query":{"bool":{"filter":[{"term":{"series":"trace.segment.time_ms"}},{"term":{"keys.entry.service":"order-consumer"}},{"term":{"dow":0}},{"term":{"hour":0}}]}}}' \
  | jq -r '.hits.hits[]._source | "        \(.keys.segment)：中位數 \(.median * 10 | floor / 10) ms"'

run_case() {  # run_case <說明> <開始做什麼> <結束做什麼> <預期的異常（; 分開）>
  local a b want
  a=$(( ($(date +%s) / 60 + 1) * 60 ))
  sleep $(( a - $(date +%s) ))
  eval "$2"
  b=$(( a + 180 ))
  sleep $(( b - $(date +%s) + 15 ))
  aiops trace-detect --config "${cfg}" --window 3min --at "$(iso "${b}")" >/dev/null
  got="$(anomalies "${a}" "${b}")"
  eval "$3"
  if [[ -z "$4" ]]; then check "$1：沒有誤報" "${got}" ""; return; fi
  while IFS= read -r want; do
    if grep -qxF -- "${want}" <<<"${got}"; then echo "  PASS  $1：抓到 ${want}"; else echo "  FAIL  $1：沒抓到 ${want}；抓到的是 [${got}]"; fail=1; fi
  done < <(tr ';' '\n' <<<"$4")
}

echo "== 5. 正常 3 分鐘：不能有誤報"
run_case "正常" ":" ":" ""

echo "== 6. 消費者變慢（每筆多 150ms，跟不上每秒 10 筆），3 分鐘"
lag0=$(lag order-push)
run_case "消費者變慢" "ctl order-consumer 'echo 150 > /ctl/slow'" \
  "lag1=\$(lag order-push); ctl order-consumer 'rm -f /ctl/slow'" \
  "time_ms|order-consumer|order-consumer › 排隊（orders）← order-producer"
ok "消費者變慢：kafka_exporter 的落後一直漲" "$( (( lag1 > lag0 + 300 )) && echo true)" "${lag0} → ${lag1} 筆"
echo "   等消費者追上（2 分鐘）"; sleep 120

echo "== 7. 寫 DB 失敗（程式 catch 掉、記 error log），3 分鐘"
run_case "寫 DB 失敗" "ctl order-consumer 'touch /ctl/fail'" "ctl order-consumer 'rm -f /ctl/fail'" \
  "error_rate|order-consumer|order-consumer › postgres → postgresql"

[[ -z "${KEEP:-}" ]] && kubectl delete namespace "${ns}" --wait=false >/dev/null
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
