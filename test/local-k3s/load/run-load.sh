#!/usr/bin/env bash
# 大流量測試：依序用不同速率產生 log，量遺失、延遲、資源用量。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/load/run-load.sh [速率...]（預設 500 2000 5000）
# 前提：run-e2e.sh 已經跑過（ES、Fluent Bit 都在）。資源採樣讀 /proc，所以要在節點上執行。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
DURATION="${DURATION:-120}"
RATES=("${@:-500 2000 5000}"); [[ $# -eq 0 ]] && RATES=(500 2000 5000)
es() { curl -sS -m 30 -u elastic:changeme-test-only -H 'Content-Type: application/json' "$@"; }

cpu_ticks() { awk '{print $14+$15}' "/proc/$1/stat"; }
rss_mb() { awk '/VmRSS/{printf "%d", $2/1024}' "/proc/$1/status"; }
fb_pid() { pgrep -x fluent-bit | head -1; }
es_pid() { pgrep -f 'Des.networkaddress.cache.ttl' | head -1; }  # ES 主程序（不是啟動用的 CLI）

pipeline_stats() { es "${ES_URL}/_nodes/stats/ingest" | jq -r '[.nodes[].ingest.pipelines["k8s-logs"] | .count, .time_in_millis] | @tsv'; }

kubectl apply -f "${here}/generator.yaml" >/dev/null
printf "%-6s %-8s %-8s %-6s %-12s %-12s %-10s %-14s %-14s %-12s\n" \
  速率 送出 收到 遺失 延遲p50/p99 最大延遲 消化完 FB_CPU/RSS ES_CPU/RSS pipeline/筆
for rate in "${RATES[@]}"; do
  run="r${rate}-$(date +%H%M%S)"
  # 只數產生器的資料行（有 seq），不數它結束時回報的那一行
  q="{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"${run}\"}},{\"exists\":{\"field\":\"app.seq\"}}]}}"
  read -r p_cnt0 p_ms0 < <(pipeline_stats)
  fb=$(fb_pid); esp=$(es_pid)
  fb_t0=$(cpu_ticks "$fb"); es_t0=$(cpu_ticks "$esp"); t0=$(date +%s)
  fb_rss=0; es_rss=0

  sed -e "s/\${RUN}/${run}/g" -e "s/\${RATE}/${rate}/g" -e "s/\${DURATION}/${DURATION}/g" "${here}/job.tpl.yaml" | kubectl apply -f - >/dev/null
  until kubectl -n load-test get job "gen-${run}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do
    fb_rss=$(( $(rss_mb "$fb") > fb_rss ? $(rss_mb "$fb") : fb_rss ))
    es_rss=$(( $(rss_mb "$esp") > es_rss ? $(rss_mb "$esp") : es_rss ))
    sleep 5
  done
  gen_end=$(date +%s)
  # 產生器結束時在 stderr 回報實際送出筆數；直接讀節點上的 log 檔（不經過 Fluent Bit）
  emitted=$(cat /var/log/pods/load-test_gen-${run}-*/gen/*.log 2>/dev/null | grep -o '"emitted": [0-9]*' | grep -o '[0-9]*' || echo "?")
  # 等資料消化完：連續 3 次筆數不變，或最多 5 分鐘
  prev=-1; same=0
  for _ in $(seq 1 60); do
    es -X POST "${ES_URL}/k8s-imc-local/_refresh" >/dev/null
    got=$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":${q}}" | jq .count)
    [[ "$got" == "$prev" ]] && same=$((same+1)) || same=0
    prev=$got; [[ $same -ge 3 ]] && break
    fb_rss=$(( $(rss_mb "$fb") > fb_rss ? $(rss_mb "$fb") : fb_rss ))
    sleep 5
  done
  t1=$(date +%s)
  fb_cpu=$(awk -v d="$(( $(cpu_ticks "$fb") - fb_t0 ))" -v s="$((t1-t0))" 'BEGIN{printf "%d%%", d/s}')
  es_cpu=$(awk -v d="$(( $(cpu_ticks "$esp") - es_t0 ))" -v s="$((t1-t0))" 'BEGIN{printf "%d%%", d/s}')
  stats=$(es "${ES_URL}/k8s-imc-local/_search" -d "{
    \"size\":0, \"query\":${q},
    \"runtime_mappings\":{\"lag\":{\"type\":\"long\",\"script\":\"emit(doc['event.ingested'].value.toInstant().toEpochMilli() - doc['@timestamp'].value.toInstant().toEpochMilli())\"}},
    \"aggs\":{\"p\":{\"percentiles\":{\"field\":\"lag\",\"percents\":[50,99]}},\"mx\":{\"max\":{\"field\":\"lag\"}},\"last\":{\"max\":{\"field\":\"event.ingested\"}}}}")
  p50=$(jq '.aggregations.p.values["50.0"]/1000|.*10|floor/10' <<<"$stats")
  p99=$(jq '.aggregations.p.values["99.0"]/1000|.*10|floor/10' <<<"$stats")
  mx=$(jq '.aggregations.mx.value/1000|.*10|floor/10' <<<"$stats")
  last_ms=$(jq '.aggregations.last.value|floor' <<<"$stats")
  drain=$(( last_ms/1000 - gen_end )); (( drain < 0 )) && drain=0
  read -r p_cnt1 p_ms1 < <(pipeline_stats)
  per_doc=$(awk -v c="$((p_cnt1-p_cnt0))" -v m="$((p_ms1-p_ms0))" 'BEGIN{ if (c>0) printf "%.3fms", m/c; else print "-"}')
  printf "%-6s %-8s %-8s %-6s %-12s %-12s %-10s %-14s %-14s %-12s\n" \
    "$rate" "$emitted" "$prev" "$((emitted-prev))" "${p50}s/${p99}s" "${mx}s" "+${drain}s" "${fb_cpu}/${fb_rss}MB" "${es_cpu}/${es_rss}MB" "$per_doc"
done
fb_ip=$(kubectl -n logging get pod -l app.kubernetes.io/name=fluent-bit -o jsonpath='{.items[0].status.podIP}')
echo; echo "Fluent Bit 出口統計（累計）："
curl -s "http://${fb_ip}:2020/api/v2/metrics/prometheus" | grep -E '^fluentbit_output_(proc_records|dropped_records|retries|retries_failed)_total' | grep -E 'es\.[0-9]'
