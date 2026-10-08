#!/usr/bin/env bash
# ES 整台停機測試：產生器持續送 log，中途把 ES 關掉一段時間再開回來，
# 比對送出與收到的筆數，確認停機期間的資料有沒有補齊。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/load/run-outage.sh
# 前提：測試 ES 有掛硬碟（elasticsearch.yaml 的 volumeClaimTemplates），否則重啟後資料全沒了。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
RATE="${RATE:-100}"
DOWN_SECONDS="${DOWN_SECONDS:-180}"
BEFORE_SECONDS=60
AFTER_SECONDS=120
DURATION=$(( BEFORE_SECONDS + DOWN_SECONDS + AFTER_SECONDS + 60 ))
es() { curl -sS -m 30 -u elastic:changeme-test-only -H 'Content-Type: application/json' "$@"; }
fb_metric() {
  local ip; ip=$(kubectl -n logging get pod -l app.kubernetes.io/name=fluent-bit -o jsonpath='{.items[0].status.podIP}')
  curl -s "http://${ip}:2020/api/v2/metrics/prometheus" | awk -v m="$1" '$1 ~ "^"m"\\{name=\"es\\.1\"\\}" {print $2}'
}
ts() { date -u +%H:%M:%S; }

run="outage-$(date +%H%M%S)"
q="{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"${run}\"}},{\"exists\":{\"field\":\"app.seq\"}}]}}"
kubectl apply -f "${here}/generator.yaml" >/dev/null
sed -e "s/\${RUN}/${run}/g" -e "s/\${RATE}/${RATE}/g" -e "s/\${DURATION}/${DURATION}/g" "${here}/job.tpl.yaml" | kubectl apply -f - >/dev/null
echo "$(ts) 產生器開始：每秒 ${RATE} 筆，共 ${DURATION} 秒（run=${run}）"
sleep "${BEFORE_SECONDS}"

retries0=$(fb_metric fluentbit_output_retries_total)
down_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
es -X POST "${ES_URL}/k8s-*/_refresh" >/dev/null
total_before=$(es "${ES_URL}/k8s-*/_count" | jq .count)
echo "$(ts) 關掉 ES（此時 ES 裡有 ${total_before} 筆）"
kubectl -n es-test scale statefulset/elasticsearch --replicas=0 >/dev/null
kubectl -n es-test wait --for=delete pod/elasticsearch-0 --timeout=120s >/dev/null 2>&1 || true
sleep "${DOWN_SECONDS}"
fb_ip=$(kubectl -n logging get pod -l app.kubernetes.io/name=fluent-bit -o jsonpath='{.items[0].status.podIP}')
backlog=$(du -sh /var/fluent-bit/state/flb-storage 2>/dev/null | cut -f1)
retries1=$(fb_metric fluentbit_output_retries_total)
echo "$(ts) 停機 ${DOWN_SECONDS} 秒期間：Fluent Bit 重試 $((retries1 - retries0)) 次，節點上堆積 ${backlog}"

echo "$(ts) 開回 ES"
up_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
kubectl -n es-test scale statefulset/elasticsearch --replicas=1 >/dev/null
until [[ "$(es -m 5 "${ES_URL}/_cluster/health" 2>/dev/null | jq -r .status 2>/dev/null)" =~ green|yellow ]]; do sleep 2; done
ready_at=$(date +%s)
echo "$(ts) ES 可以用了"

until kubectl -n load-test get job "gen-${run}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do sleep 5; done
emitted=$(cat /var/log/pods/load-test_gen-${run}-*/gen/*.log | grep -o '"emitted": [0-9]*' | grep -o '[0-9]*')
prev=-1; same=0
for _ in $(seq 1 120); do
  es -X POST "${ES_URL}/k8s-imc-local/_refresh" >/dev/null
  got=$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":${q}}" | jq .count)
  [[ "$got" == "$prev" ]] && same=$((same+1)) || same=0
  prev=$got
  [[ "$got" -ge "$emitted" || $same -ge 6 ]] && break
  sleep 5
done
in_window=$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":{\"bool\":{\"filter\":[${q},{\"range\":{\"@timestamp\":{\"gte\":\"${down_at}\",\"lt\":\"${up_at}\"}}}]}}}" | jq .count)
catchup=$(es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":0,\"query\":{\"bool\":{\"filter\":[${q},{\"range\":{\"@timestamp\":{\"gte\":\"${down_at}\",\"lt\":\"${up_at}\"}}}]}},\"aggs\":{\"last\":{\"max\":{\"field\":\"event.ingested\"}}}}" | jq '.aggregations.last.value/1000|floor')
total_after_old=$(es "${ES_URL}/k8s-*/_count" -d "{\"query\":{\"range\":{\"event.ingested\":{\"lte\":\"${down_at}\"}}}}" | jq .count)
dropped=$(fb_metric fluentbit_output_dropped_records_total)

echo
echo "結果："
echo "  送出 ${emitted} 筆，收到 ${prev} 筆，遺失 $((emitted - prev)) 筆"
echo "  停機期間（${down_at} ~ ${up_at}）產生的資料：${in_window} 筆（每秒 ${RATE} 筆 × ${DOWN_SECONDS} 秒 ≈ $((RATE * DOWN_SECONDS))）"
echo "  ES 可用後 $((catchup - ready_at)) 秒補完停機期間的資料"
echo "  停機前已在 ES 的資料：關機時 ${total_before} 筆，重啟後查得到 ${total_after_old} 筆"
echo "  Fluent Bit 這條出口累計丟棄：${dropped} 筆"
