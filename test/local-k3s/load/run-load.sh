#!/usr/bin/env bash
# log 收集的可靠度、效能測試：產生器每行帶序號，數遺失、重複、延遲、補送時間，以及節點 Collector 的 CPU／記憶體
#（節點 Collector 同時也在收 metrics，量到的是兩者合計）。
# 用 agent.type 分收集器（預設只數 otelcol；邊緣 VM 之類的其他收集器可用 AGENTS="otelcol fluent-bit"）。
#
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/load/run-compare.sh <情境>
#   volume [速率...]  大流量（預設 2000 5000，各 DURATION 秒，預設 120）
#   outage            ES 正常關機 DOWN_SECONDS 秒（預設 180）
#   crash             kill -9 ES 兩次（第 2 次在補送中）
#   keyfail           寫入 key 作廢 DOWN_SECONDS 秒，再換新 key
#   restart           ES、key 都正常，只重啟收集器兩次
# 前提：run-e2e.sh 跑過；測試 ES 單節點；要在節點上執行（讀 /proc、kill ES）。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
A=elastic:changeme-test-only
es() { curl -sS -m 60 -u "${A}" -H 'Content-Type: application/json' "$@"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ts() { date -u +%H:%M:%S; }
read -r -a AGENTS <<<"${AGENTS:-otelcol}"

ot_pid() { pgrep -f '^/otelcol-contrib --config=/conf/common.yaml --config=/conf/node.yaml' | head -1; }
es_pid() { pgrep -f '^/usr/share/elasticsearch/jdk/bin/java .*-Des.networkaddress.cache.ttl' | head -1; }
cpu_ticks() { awk '{print $14+$15}' "/proc/$1/stat" 2>/dev/null || echo 0; }
rss_mb() { awk '/VmRSS/{printf "%d", $2/1024}' "/proc/$1/status" 2>/dev/null || echo 0; }
wait_es() { until [[ "$(es -m 5 "${ES_URL}/_cluster/health" 2>/dev/null | jq -r .status 2>/dev/null)" =~ green|yellow ]]; do sleep 2; done; }

start_gen() {  # start_gen <run> <rate> <duration>
  kubectl apply -f "${here}/generator.yaml" >/dev/null
  sed -e "s/\${RUN}/$1/g" -e "s/\${RATE}/$2/g" -e "s/\${DURATION}/$3/g" "${here}/job.tpl.yaml" | kubectl apply -f - >/dev/null
}
wait_gen() {  # wait_gen <run> → 印出送出筆數
  until kubectl -n load-test get job "gen-$1" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do sleep 5; done
  cat /var/log/pods/load-test_gen-$1-*/gen/*.log | grep -o '"emitted": [0-9]*' | grep -o '[0-9]*'
}

q_run() {  # q_run <run> <agent>
  echo "{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"$1\"}},{\"exists\":{\"field\":\"app.seq\"}},{\"term\":{\"agent.type\":\"$2\"}}]}}"
}
received() { es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":$(q_run "$1" "$2")}" | jq .count; }
# 不同序號有幾個（逐頁數 composite，數量大也精確）
unique_seqs() {  # unique_seqs <run> <agent>
  local after="" n=0 resp
  while :; do
    resp="$(es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":0,\"query\":$(q_run "$1" "$2"),
      \"aggs\":{\"s\":{\"composite\":{\"size\":10000,\"sources\":[{\"seq\":{\"terms\":{\"field\":\"app.seq\"}}}]${after:+,\"after\":${after}}}}}}")"
    n=$(( n + $(jq '.aggregations.s.buckets | length' <<<"${resp}") ))
    after="$(jq -c '.aggregations.s.after_key // empty' <<<"${resp}")"
    [[ -n "${after}" ]] || break
  done
  echo "${n}"
}
lag() {  # lag <run> <agent> → p50/p99/最大（秒）
  es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":0,\"query\":$(q_run "$1" "$2"),
    \"runtime_mappings\":{\"lag\":{\"type\":\"long\",\"script\":\"emit(doc['event.ingested'].value.toInstant().toEpochMilli() - doc['@timestamp'].value.toInstant().toEpochMilli())\"}},
    \"aggs\":{\"p\":{\"percentiles\":{\"field\":\"lag\",\"percents\":[50,99]}},\"mx\":{\"max\":{\"field\":\"lag\"}}}}" \
    | jq -r '"\(.aggregations.p.values["50.0"]/1000|.*10|floor/10)s/\(.aggregations.p.values["99.0"]/1000|.*10|floor/10)s/\(.aggregations.mx.value/1000|.*10|floor/10)s"'
}
# 某段時間產生的資料，最後一筆是幾點進 ES 的
last_ingested() {  # last_ingested <run> <agent> <from> <to>
  es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":0,\"query\":{\"bool\":{\"filter\":[$(q_run "$1" "$2"),
    {\"range\":{\"@timestamp\":{\"gte\":\"$3\",\"lt\":\"$4\"}}}]}},\"aggs\":{\"m\":{\"max\":{\"field\":\"event.ingested\"}}}}" \
    | jq -r '.aggregations.m.value // 0 | ./1000 | floor'
}

# 等兩邊都收齊（或逾時），每 5 秒看一次
wait_both() {  # wait_both <run> <emitted> <最多幾秒>
  local s=$SECONDS a done
  while (( SECONDS - s < $3 )); do
    es -X POST "${ES_URL}/k8s-imc-local/_refresh" >/dev/null
    done=1
    for a in "${AGENTS[@]}"; do [[ "$(received "$1" "${a}")" -ge "$2" ]] || done=0; done
    (( done )) && return
    sleep 5
  done
}

report() {  # report <run> <emitted> [<恢復時間 epoch> <受影響的開始> <受影響的結束>]
  local a got uniq
  printf "   %-11s %-8s %-8s %-6s %-6s %-20s %s\n" 收集器 送出 收到 遺失 重複 "延遲 p50/p99/最大" "${3:+補完（恢復後）}"
  for a in "${AGENTS[@]}"; do
    got="$(received "$1" "${a}")"; uniq="$(unique_seqs "$1" "${a}")"
    local catchup=""
    if [[ -n "${3:-}" ]]; then
      local li; li="$(last_ingested "$1" "${a}" "$4" "$5")"
      (( li > 0 )) && catchup="+$(( li - $3 ))s" || catchup="（那段 0 筆）"
    fi
    printf "   %-11s %-8s %-8s %-6s %-6s %-20s %s\n" "${a}" "$2" "${got}" "$(( $2 - uniq ))" "$(( got - uniq ))" "$(lag "$1" "${a}")" "${catchup}"
  done
}

do_volume() {
  local rates=("$@"); [[ ${#rates[@]} -eq 0 ]] && rates=(2000 5000)
  local d=${DURATION:-120} rate run ot t0 otb ot_rss ot_rss0 emitted t1
  for rate in "${rates[@]}"; do
    run="cmp${rate}-$(date +%H%M%S)"
    echo "== 每秒 ${rate} 行，${d} 秒（run=${run}）"
    ot="$(ot_pid)"; otb="$(cpu_ticks "${ot}")"; t0=$(date +%s); ot_rss0="$(rss_mb "${ot}")"; ot_rss=0
    start_gen "${run}" "${rate}" "${d}"
    until kubectl -n load-test get job "gen-${run}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do
      ot_rss=$(( $(rss_mb "${ot}") > ot_rss ? $(rss_mb "${ot}") : ot_rss ))
      sleep 3
    done
    emitted="$(wait_gen "${run}")"
    wait_both "${run}" "${emitted}" 300
    t1=$(date +%s)
    report "${run}" "${emitted}"
    echo "   節點 Collector（含 metrics）：CPU 平均 $(( ($(cpu_ticks "${ot}") - otb) / (t1 - t0) ))%（100% = 1 核），記憶體 開始 ${ot_rss0}MB、最高 ${ot_rss}MB"
  done
}

do_outage() {
  local secs=${DOWN_SECONDS:-180} run emitted down up ready
  run="out-$(date +%H%M%S)"
  echo "== ES 正常關機 ${secs} 秒（每秒 200 行，run=${run}）"
  start_gen "${run}" 200 $(( secs + 180 ))
  sleep 60
  down="$(now)"; kubectl -n es-test scale statefulset/elasticsearch --replicas=0 >/dev/null
  kubectl -n es-test wait --for=delete pod/elasticsearch-0 --timeout=120s >/dev/null 2>&1 || true
  echo "   $(ts) ES 關掉"; sleep "${secs}"
  up="$(now)"; kubectl -n es-test scale statefulset/elasticsearch --replicas=1 >/dev/null; wait_es; ready=$(date +%s)
  echo "   $(ts) ES 可以用了"
  emitted="$(wait_gen "${run}")"; wait_both "${run}" "${emitted}" 600
  report "${run}" "${emitted}" "${ready}" "${down}" "${up}"
}

do_crash() {
  local run emitted pid t0 ready
  run="crash-$(date +%H%M%S)"
  echo "== kill -9 ES 兩次（每秒 200 行，run=${run}）"
  start_gen "${run}" 200 300
  sleep 60
  t0="$(now)"
  pid="$(es_pid)"; kill -9 "${pid}"; echo "   $(ts) 第 1 次 kill -9"; sleep 5; wait_es; echo "   $(ts) ES 自己重啟完成"
  sleep 10
  pid="$(es_pid)"; kill -9 "${pid}"; echo "   $(ts) 第 2 次 kill -9（補送中）"; sleep 5; wait_es; ready=$(date +%s); echo "   $(ts) ES 自己重啟完成"
  emitted="$(wait_gen "${run}")"; wait_both "${run}" "${emitted}" 600
  report "${run}" "${emitted}" "${ready}" "${t0}" "$(now)"
}

do_keyfail() {
  local secs=${DOWN_SECONDS:-180} run emitted bad good newkey ns
  run="key-$(date +%H%M%S)"
  echo "== 寫入 key 作廢 ${secs} 秒（每秒 200 行，產生器在 IMC，run=${run}）"
  start_gen "${run}" 200 $(( secs + 180 ))
  sleep 60
  bad="$(now)"
  es -X DELETE "${ES_URL}/_security/api_key" -d '{"name":"es-writer"}' >/dev/null
  echo "   $(ts) key 作廢"; sleep "${secs}"; good="$(now)"
  newkey="$(es -X POST "${ES_URL}/_security/api_key" -d '{"name":"es-writer","role_descriptors":{"es-writer":{"index":[
    {"names":["k8s-*","metrics-*.otel-*","logs-*.otel-*","traces-*.otel-*"],"privileges":["create_doc","create_index","auto_configure"]}]}}}' | jq -r .encoded)"
  kubectl -n otel-collector get secret es-writer-keys -o json \
    | jq --arg v "$(printf %s "${newkey}" | base64 -w0)" '.data.ES_WRITER_API_KEY = $v' | kubectl apply -f - >/dev/null
  restart_collectors
  echo "   $(ts) 換上新 key、重啟收集器"
  emitted="$(wait_gen "${run}")"; wait_both "${run}" "${emitted}" 300
  report "${run}" "${emitted}" "$(date -u -d "${good}" +%s)" "${bad}" "${good}"
}

restart_collectors() {
  kubectl -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
  kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=300s >/dev/null
}

do_restart() {
  local run emitted
  run="rst-$(date +%H%M%S)"
  echo "== 只重啟收集器（ES、key 都正常；每秒 200 行，run=${run}）"
  start_gen "${run}" 200 240
  sleep 60
  restart_collectors
  echo "   $(ts) 第 1 次重啟收集器"
  sleep 60
  restart_collectors
  echo "   $(ts) 第 2 次重啟收集器"
  emitted="$(wait_gen "${run}")"; wait_both "${run}" "${emitted}" 300
  report "${run}" "${emitted}"
}

case "${1:-}" in
  restart) do_restart ;;
  volume) shift; do_volume "$@" ;;
  outage) do_outage ;;
  crash) do_crash ;;
  keyfail) do_keyfail ;;
  *) echo "用法：$0 volume [速率...]|outage|crash|keyfail|restart"; exit 2 ;;
esac
