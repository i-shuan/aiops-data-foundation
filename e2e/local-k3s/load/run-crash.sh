#!/usr/bin/env bash
# ES 當機測試（單節點）：不是正常關機，而是直接 kill -9 ES 程序（像斷電、OOM），而且砍兩次：
#   第 1 次：平常寫入中
#   第 2 次：剛恢復、正在補送第 1 次累積的資料時
# 確認：當機前已寫入的資料還在；log 每個序號都有、沒有重複；metrics 沒有缺、沒有重複；沒有進 failure store。
# 用法：ES_URL=http://<ip>:9200 e2e/local-k3s/load/run-crash.sh
# 前提：測試 ES 是單節點（run-es-2node.sh down）、run-metrics-e2e.sh 跑過；要在節點上執行（kill 用主機上的 PID）。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
RATE="${RATE:-200}"
DURATION="${DURATION:-360}"
es() { curl -sS -m 30 -u elastic:changeme-test-only -H 'Content-Type: application/json' "$@"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ts() { date -u +%H:%M:%S; }
es_pid() { pgrep -f '^/usr/share/elasticsearch/jdk/bin/java .*-Des.networkaddress.cache.ttl' | head -1; }  # ES 主程序（不是啟動用的 CLI）
wait_es() { until [[ "$(es -m 5 "${ES_URL}/_cluster/health" 2>/dev/null | jq -r .status 2>/dev/null)" =~ green|yellow ]]; do sleep 2; done; }

fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
check_ge() { if [[ "$2" -ge "$3" ]]; then echo "  PASS  $1（$2）"; else echo "  FAIL  $1：得到 $2，預期至少 $3"; fail=1; fi; }

# 當機前 ES 已經確認寫入的資料（以 ES 收到的時間 event.ingested 為準），重啟後要一筆不少
crash() {  # crash <第幾次>
  local cut before after pid
  es -X POST "${ES_URL}/k8s-*/_refresh" >/dev/null
  cut="$(now)"; sleep 2
  before="$(es "${ES_URL}/k8s-*/_count" -d "{\"query\":{\"range\":{\"event.ingested\":{\"lt\":\"${cut}\"}}}}" | jq .count)"
  pid="$(es_pid)"
  kill -9 "${pid}"
  echo "   $(ts) 第 $1 次 kill -9 ES（PID ${pid}）；當機前 ES 裡有 ${before} 筆 log"
  sleep 5
  wait_es
  echo "   $(ts) ES 自己重啟完成"
  after="$(es "${ES_URL}/k8s-*/_count" -d "{\"query\":{\"range\":{\"event.ingested\":{\"lt\":\"${cut}\"}}}}" | jq .count)"
  check "第 $1 次當機前已寫入的 log 都還在" "${after}" "${before}"
}

run="crash-$(date +%H%M%S)"
q="{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"${run}\"}},{\"exists\":{\"field\":\"app.seq\"}}]}}"
kubectl apply -f "${here}/generator.yaml" >/dev/null
start="$(now)"
sed -e "s/\${RUN}/${run}/g" -e "s/\${RATE}/${RATE}/g" -e "s/\${DURATION}/${DURATION}/g" "${here}/job.tpl.yaml" | kubectl apply -f - >/dev/null
echo "== $(ts) 產生器開始：每秒 ${RATE} 筆 log，共 ${DURATION} 秒（run=${run}）"
sleep 60
crash 1
sleep 10   # 剛恢復，Fluent Bit 和 Collector 正在補送
crash 2

until kubectl -n load-test get job "gen-${run}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do sleep 5; done
end="$(now)"
emitted=$(cat /var/log/pods/load-test_gen-${run}-*/gen/*.log | grep -o '"emitted": [0-9]*' | grep -o '[0-9]*')
echo "   $(ts) 產生器結束，送出 ${emitted} 筆；等全部補進 ES（最多 10 分鐘）"
got=0
for _ in $(seq 1 120); do
  es -X POST "${ES_URL}/k8s-imc-local,metrics-*.otel-*/_refresh" >/dev/null
  got=$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":${q}}" | jq .count)
  [[ "${got}" -ge "${emitted}" ]] && break
  sleep 5
done
sleep 30; es -X POST "${ES_URL}/k8s-imc-local,metrics-*.otel-*/_refresh" >/dev/null

echo
echo "== log（序號 0 到 $((emitted - 1))）"
got=$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":${q}}" | jq .count)
dup=$(es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":0,\"query\":${q},\"aggs\":{\"d\":{\"terms\":{\"field\":\"app.seq\",\"min_doc_count\":2,\"size\":65000}}}}" | jq '[.aggregations.d.buckets[].doc_count - 1] | add // 0')
uniq=$(( got - dup ))
echo "   送出 ${emitted}、收到 ${got}、不同序號 ${uniq}"
check "沒有遺失" "$(( emitted - uniq ))" 0
check "沒有重複" "${dup}" 0

echo "== metrics（${start} ~ ${end}，測試應用每 30 秒一筆）"
expect=$(( ($(date -u -d "${end}" +%s) - $(date -u -d "${start}" +%s)) / 30 - 1 ))
for g in it:it-mes-prod imc:imc-wip-prod unassigned:legacy; do
  n=$(es "${ES_URL}/metrics-prometheusreceiver.otel-${g%%:*}/_count" -d "{\"query\":{\"bool\":{\"filter\":[
    {\"exists\":{\"field\":\"metrics.demo_requests_total\"}},{\"term\":{\"resource.attributes.k8s.namespace.name\":\"${g#*:}\"}},
    {\"range\":{\"@timestamp\":{\"gte\":\"${start}\",\"lt\":\"${end}\"}}}]}}}" | jq .count)
  check_ge "${g%%:*}：應用 metrics 沒有缺" "${n}" "${expect}"
done
mdup=$(es "${ES_URL}/metrics-prometheusreceiver.otel-*/_search" -d "{\"size\":0,\"query\":{\"bool\":{\"filter\":[
  {\"exists\":{\"field\":\"metrics.demo_requests_total\"}},{\"range\":{\"@timestamp\":{\"gte\":\"${start}\",\"lt\":\"${end}\"}}}]}},
  \"aggs\":{\"d\":{\"multi_terms\":{\"terms\":[{\"field\":\"resource.attributes.k8s.pod.name\"},{\"field\":\"@timestamp\"}],\"min_doc_count\":2,\"size\":1000}}}}" \
  | jq '[.aggregations.d.buckets[].doc_count - 1] | add // 0')
check "metrics 沒有重複" "${mdup}" 0
check "沒有資料被丟進 failure store" \
  "$(es "${ES_URL}/.fs-*/_count?expand_wildcards=all&ignore_unavailable=true" -d "{\"query\":{\"range\":{\"@timestamp\":{\"gte\":\"${start}\"}}}}" | jq -r '.count // 0')" 0
dropped=0
for f in /var/log/pods/otel-collector_*/otelcol/*.log; do dropped=$(( dropped + $(grep -c "Dropping data" "${f}" || true) )); done
check "Collector 沒有丟資料的紀錄" "${dropped}" 0

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
