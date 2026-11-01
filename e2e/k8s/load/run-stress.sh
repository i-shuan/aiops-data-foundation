#!/usr/bin/env bash
# phase1 收值壓測：log、metrics、trace 經 OpenTelemetry Collector 寫進 ES，壓到開始掉、加上邊緣測資。
# 每個情境印兩張表：每一條資料（送出、Collector 拒收、ES 收到、遺失、重複、延遲 p50／p95／最大），
# 以及 Collector（節點）與 ES 的 CPU、記憶體、佇列、失敗計數；最後是 Collector log 的 error／warn。
#
# 用法：e2e/k8s/load/run-stress.sh <情境> [參數]
#   log    [每秒行數...]     log 加量（預設 5000 10000 20000，各 60 秒）
#   metric [每秒資料點...]   OTLP 推 metrics 加量（預設 5000 20000 50000 100000，各 60 秒）
#   trace  [每秒 span...]    trace 加量（預設 1000 3000 6000 10000，各 60 秒，每個 trace 5 段）
#   smoke                    三條線平常量 30 秒（確認環境、欄位）
#   burst                    三條線一起：平常 60 秒 → 10 倍 60 秒 → 平常 60 秒（log 200、metrics 1,000、trace 200）
#   bigline                  64 KB 的 log 每秒 20 行、1 MB 的 log 每秒 2 行，各 60 秒
#   pods50                   50 個 Pod 同時送：每個每秒 40 行 log、20 個 span，120 秒
#   cardinality              高基數：一個 Pod 開 20,000 條序列給 Collector 抓，id 每 30 秒全換，300 秒
#   bigtrace                 一個 trace 上千段：每個 trace 1,000 段，每秒 2 個 trace，120 秒
#   reject                   ES 拒收（index 擋寫入，像磁碟滿）DOWN 秒（預設 180），平常量
#   slow [秒]                ES 每個回應慢 N 秒（預設 5；> 30 就超過 Collector 的逾時），平常量 180 秒
#   queuefull                佇列縮小（log、metrics 20,000 筆；trace 20 批；只在這個情境），ES 拒收 DOWN 秒，看佇列滿了怎麼樣
# 環境變數：SIGNALS 只跑哪幾條線（預設 "log metric trace"；smoke、burst、pods50、reject、slow、queuefull 有效），DOWN 拒收秒數，
#   ROLLOVER_MB 每個情境開始前 log 寫入 index 超過幾 MB 就 rollover（預設 800）。
# 前提：run-e2e.sh 跑過（ES、Collector、測試應用）；KUBECONFIG 指到本機 cluster。
# cluster 只有一個、幾個任務共用：先 source e2e/k8s/cluster-lock.sh; cluster_lock "<名字>"。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../../.." && pwd)"
NS=load-test
OC=otel-collector
IMG="${IMG:-docker.io/library/python:3.13-slim}"
DOWN="${DOWN:-180}"
SIGNALS="${SIGNALS:-log metric trace}"
ROLLOVER_MB="${ROLLOVER_MB:-800}"
has() { [[ " ${SIGNALS} " == *" $1 "* ]]; }
ts() { date -u +%H:%M:%S; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
probe() { MSYS_NO_PATHCONV=1 kubectl -n "${NS}" exec -i probe -- python /gen/probe.py "$@"; }
es() { MSYS_NO_PATHCONV=1 kubectl -n "${NS}" exec probe -- python -c "
import base64,sys,urllib.request
r=urllib.request.Request('http://elasticsearch.es-test.svc:9200'+sys.argv[1],data=(sys.argv[3].encode() if len(sys.argv)>3 else None),method=sys.argv[2],
  headers={'Authorization':'Basic '+base64.b64encode(b'elastic:changeme-test-only').decode(),'Content-Type':'application/json'})
print(urllib.request.urlopen(r,timeout=60).read().decode())" "$@"; }

# ---------------------------------------------------------------- 準備
# 部署的 Collector 設定要跟 repo 一樣（只比設定，不比註解）；別的任務改過就換回來
config_body() { tr -d '\r' | grep -vE '^\s*(#|$)'; }
ensure_baseline() {
  local cm f diff=0
  cm="$(kubectl -n "${OC}" get ds otel-node-collector -o jsonpath='{.spec.template.spec.volumes[?(@.name=="config")].configMap.name}')"
  for f in common node cluster; do
    if ! diff -q <(kubectl -n "${OC}" get cm "${cm}" -o go-template="{{index .data \"${f}.yaml\"}}" | config_body) \
                 <(config_body < "${repo}/collector/code/${f}.yaml") >/dev/null; then diff=1; fi
  done
  if (( diff )) || [[ "$(kubectl -n "${OC}" get cm -o name | grep -c otel-collector-env)" == 0 ]] \
     || ! kubectl -n "${OC}" get cm "$(kubectl -n "${OC}" get ds otel-node-collector -o jsonpath='{.spec.template.spec.containers[0].envFrom[0].configMapRef.name}')" \
          -o jsonpath='{.data.ES_ENDPOINT}' | grep -q '^http://elasticsearch.es-test.svc:9200$'; then
    echo "   $(ts) 部署的 Collector 跟 repo 不同，換回 repo 的設定"
    "${repo}/e2e/k8s/deploy-collector.sh"
    wait_collectors
  fi
}
wait_collectors() {
  kubectl -n "${OC}" rollout status daemonset/otel-node-collector --timeout=300s >/dev/null
  kubectl -n "${OC}" rollout status deployment/otel-cluster-collector --timeout=300s >/dev/null
}
# log 用指紋 _id 去重，每筆寫入都在寫入 index 查 _id：寫入 index 大過 ES 的 page cache 後每筆讀磁碟、bulk 超過 Collector 逾時，
# 佇列永遠追不上（docs/troubleshooting.md）。壓測灌的量很大，每個情境開始前寫入 index 超過 ROLLOVER_MB 就換一個新的。
rollover_big() {
  MSYS_NO_PATHCONV=1 kubectl -n "${NS}" exec probe -- python -c "
import base64,json,time,urllib.request
H={'Authorization':'Basic '+base64.b64encode(b'elastic:changeme-test-only').decode(),'Content-Type':'application/json'}
def es(p,m='GET'): return json.load(urllib.request.urlopen(urllib.request.Request('http://elasticsearch.es-test.svc:9200'+p,method=m,headers=H),timeout=60))
for d in es('/_data_stream/k8s-*')['data_streams']:
  w=d['indices'][-1]['index_name']
  mb=es('/'+w+'/_stats/store')['_all']['primaries']['store']['size_in_bytes']/2**20
  if mb>=${ROLLOVER_MB}:
    es('/'+d['name']+'/_rollover','POST'); print('   %s %s 的寫入 index %d MB ≥ ${ROLLOVER_MB}MB，rollover' % (time.strftime('%H:%M:%S',time.gmtime()),d['name'],mb))
"
}
setup() {
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata: {name: ${NS}, labels: {dept: D1110}}
EOF
  # Windows 的 kubectl 讀不到 <(...)，先寫成暫存檔（順便去掉 CRLF）
  local tmp; tmp="$(mktemp -d)"
  tr -d '\r' < "${here}/loadgen.py" > "${tmp}/loadgen.py"; tr -d '\r' < "${here}/probe.py" > "${tmp}/probe.py"
  kubectl -n "${NS}" create configmap loadgen --dry-run=client -o yaml \
    --from-file=loadgen.py="${tmp}/loadgen.py" --from-file=probe.py="${tmp}/probe.py" | kubectl apply -f - >/dev/null
  rm -rf "${tmp}"
  # probe 沒有 app.kubernetes.io/name 以外的 label，也不吐 log：不影響量測
  if ! kubectl -n "${NS}" get pod probe >/dev/null 2>&1; then
    kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: probe, namespace: ${NS}, labels: {app.kubernetes.io/name: load-probe}}
spec:
  containers:
    - name: probe
      image: ${IMG}
      imagePullPolicy: IfNotPresent
      command: ["sleep", "infinity"]
      volumeMounts: [{name: gen, mountPath: /gen}]
  volumes: [{name: gen, configMap: {name: loadgen}}]
EOF
  fi
  kubectl -n "${NS}" wait --for=condition=Ready pod/probe --timeout=180s >/dev/null
  ensure_baseline
  rollover_big
}

# Collector 的 Pod IP（量 :8888）
collectors_json() {
  kubectl -n "${OC}" get pods -o go-template='{{range .items}}{{if .status.podIP}}{{.metadata.name}} {{.status.podIP}}{{"\n"}}{{end}}{{end}}' \
    | awk 'BEGIN{printf "["} /^otel-node/{printf "%s{\"name\":\"%s\",\"ip\":\"%s\"}", (n++?",":""), $1, $2} END{printf "]"}'
}

# ---------------------------------------------------------------- 產生器
# job <名稱> <Pod 數> <MODE> [環境變數 K=V ...]（prom 的 Pod 會被 Collector 抓）
job() {
  local name="$1" pods="$2" mode="$3"; shift 3
  local env="" kv ann=""
  for kv in "MODE=${mode}" "RUN=${name}" "$@"; do env+="            - {name: ${kv%%=*}, value: \"${kv#*=}\"}"$'\n'; done
  [[ "${mode}" == prom ]] && ann='      annotations: {prometheus.io/scrape: "true", prometheus.io/port: "9464"}'
  kubectl apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: ${name}, namespace: ${NS}}
spec:
  completionMode: Indexed
  completions: ${pods}
  parallelism: ${pods}
  backoffLimit: 0
  template:
    metadata:
      labels: {app.kubernetes.io/name: load-gen, app.kubernetes.io/version: "1.0.0"}
${ann}
    spec:
      restartPolicy: Never
      containers:
        - name: gen
          image: ${IMG}
          imagePullPolicy: IfNotPresent
          command: ["python", "-u", "/gen/loadgen.py"]
          resources: {requests: {cpu: 10m, memory: 16Mi}}
          env:
${env}          volumeMounts: [{name: gen, mountPath: /gen}]
      volumes: [{name: gen, configMap: {name: loadgen}}]
EOF
}
wait_jobs() {  # wait_jobs <job...>：等全部結束，印出每個 Pod 的結果行
  local j pod
  for j in "$@"; do
    until kubectl -n "${NS}" get job "${j}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do sleep 5; done
  done
  for j in "$@"; do
    for pod in $(kubectl -n "${NS}" get pods -l "job-name=${j}" -o name); do
      # log+trace 的 Pod 有兩行結果，兩個子程序先後結束，中間可能夾著幾百行 log
      kubectl -n "${NS}" logs "${pod}" --tail=2000 | grep '"result"' || echo "{\"result\":{\"mode\":\"?\",\"run\":\"${j}\",\"sent\":0,\"refused\":0,\"errors\":{\"沒有結果\":1}}}"
    done
  done
}
# 結束：等產生器、等收齊、印表、印 Collector log
finish() {  # finish <情境> <最多等幾秒> <job...>
  local name="$1" limit="$2"; shift 2
  local res; res="$(mktemp)"
  wait_jobs "$@" > "${res}"
  echo "   $(ts) 產生器結束，等 ES 收齊（最多 ${limit} 秒）"
  probe wait "${name}" "${limit}" < "${res}"
  probe report "${name}" < "${res}"
  collector_log "${START_AT}"
  kubectl -n "${NS}" delete job "$@" --wait=false >/dev/null
  rm -f "${res}"
}
collector_log() {  # 這段時間 Collector 印了哪些 error／warn（前 6 種）
  local p out
  out="$(for p in $(kubectl -n "${OC}" get pods -o name | grep -E 'otel-node|otel-cluster'); do
    kubectl -n "${OC}" logs "${p}" --since-time="$1" 2>/dev/null | tr -d '\r' \
      | awk -F'\t' -v p="${p#pod/}" '$2=="error"||$2=="warn"{m=$4; gsub(/[0-9]+/,"N",m); print p" "$2" "substr(m,1,110)}'
  done | sort | uniq -c | sort -rn)"
  echo
  echo "== Collector log 的 error／warn（$1 之後）：$(awk '{s+=$1} END{print s+0}' <<<"${out}") 行"
  [[ -n "${out}" ]] && head -6 <<<"${out}" | sed 's/^/   /'
  # error 每種印一個例子（帶 error 欄位），看得出原因
  for p in $(kubectl -n "${OC}" get pods -o name | grep -E 'otel-node|otel-cluster'); do
    kubectl -n "${OC}" logs "${p}" --since-time="$1" 2>/dev/null | tr -d '
'       | awk -F'	' '$2=="error"{m=$4; gsub(/[0-9]+/,"N",m); if (!(m in seen)) {seen[m]=1; e=$5; sub(/.*"error": "/, "", e); print "   例：" m "：" substr(e,1,300)}}'
  done | sort -u | head -4
  return 0
}
pods_for() { echo $(( ($1 + $2 - 1) / $2 )); }  # pods_for <總速率> <每個 Pod 最多>

# ---------------------------------------------------------------- 情境
ramp() {  # ramp <kind> <每個 Pod 最多> <速率...>
  local kind="$1" per="$2" rate run pods extra=(); shift 2
  for rate in "$@"; do
    run="${kind}${rate}-$(date +%H%M%S)"; pods="$(pods_for "${rate}" "${per}")"
    echo; echo "== $(ts) ${kind} 每秒 ${rate}，60 秒，${pods} 個 Pod（run=${run}）"
    START_AT="$(now)"
    echo "{\"runs\":[{\"run\":\"${run}\",\"kind\":\"${kind}\"}],\"collectors\":$(collectors_json)}" | probe start "${run}"
    job "${run}" "${pods}" "${kind}" "PROFILE=60:$(( rate / pods ))"
    finish "${run}" 300 "${run}"
  done
}

base_load() {  # base_load <前綴> <PROFILE 的秒數:倍數...>：SIGNALS 的幾條線一起（job 名稱放在 RUNS），平常量 log 200 行、metrics 1,000 點、trace 200 span（每秒）
  local p="$1" seg prof_l="" prof_m="" prof_t=""; shift
  for seg in "$@"; do
    prof_l+="${prof_l:+,}${seg%%:*}:$(( 200 * ${seg#*:} ))"
    prof_m+="${prof_m:+,}${seg%%:*}:$(( 1000 * ${seg#*:} ))"
    prof_t+="${prof_t:+,}${seg%%:*}:$(( 200 * ${seg#*:} ))"
  done
  START_AT="$(now)"
  RUNS=(); local runs=""
  has log && RUNS+=("${p}-log") && runs+="${runs:+,}{\"run\":\"${p}-log\",\"kind\":\"log\"}"
  has metric && RUNS+=("${p}-met") && runs+="${runs:+,}{\"run\":\"${p}-met\",\"kind\":\"metric\"}"
  has trace && RUNS+=("${p}-trc") && runs+="${runs:+,}{\"run\":\"${p}-trc\",\"kind\":\"trace\"}"
  echo "{\"runs\":[${runs}],\"collectors\":$(collectors_json)}" | probe start "${p}"
  has log && job "${p}-log" 1 log "PROFILE=${prof_l}"
  has metric && job "${p}-met" 1 metric "PROFILE=${prof_m}"
  has trace && job "${p}-trc" 1 trace "PROFILE=${prof_t}"
  return 0
}

do_smoke() {
  local p="smoke-$(date +%H%M%S)"
  echo "== $(ts) 先試一次：三條線平常量 30 秒（${p}）"
  base_load "${p}" 30:1
  finish "${p}" 120 "${RUNS[@]}"
}

do_burst() {
  local p="burst-$(date +%H%M%S)"
  echo "== $(ts) 突發：平常 60 秒 → 10 倍 60 秒 → 平常 60 秒（${p}）"
  base_load "${p}" 60:1 60:10 60:1
  finish "${p}" 300 "${RUNS[@]}"
}

do_bigline() {
  local p="big-$(date +%H%M%S)"
  echo "== $(ts) 大行：64 KB 每秒 20 行、1 MB 每秒 2 行，各 60 秒（${p}）"
  START_AT="$(now)"
  echo "{\"runs\":[{\"run\":\"${p}-64k\",\"kind\":\"log\"},{\"run\":\"${p}-1m\",\"kind\":\"log\"}],\"collectors\":$(collectors_json)}" | probe start "${p}"
  job "${p}-64k" 1 log PROFILE=60:20 LINE_BYTES=65536
  job "${p}-1m" 1 log PROFILE=60:2 LINE_BYTES=1048576
  finish "${p}" 300 "${p}-64k" "${p}-1m"
  # message 有沒有完整（長度對）、有沒有被切成好幾筆
  for r in "${p}-64k:65536" "${p}-1m:1048576"; do
    echo "   ${r%%:*}：message 長度分布（長度 → 筆數）"
    es "/k8s-*/_search" POST "{\"size\":0,\"query\":{\"term\":{\"app.run\":\"${r%%:*}\"}},
      \"runtime_mappings\":{\"mlen\":{\"type\":\"long\",\"script\":\"def m = params._source.message; emit(m == null ? -1 : m.length())\"}},
      \"aggs\":{\"l\":{\"terms\":{\"field\":\"mlen\",\"size\":5}},\"noseq\":{\"missing\":{\"field\":\"app.seq\"}}}}" \
      | python_json 'a=d["aggregations"]; print("     ", {b["key"]: b["doc_count"] for b in a["l"]["buckets"]}, "；沒有 app.seq（沒解析成 JSON）的", a["noseq"]["doc_count"], "筆")'
  done
}
python_json() { MSYS_NO_PATHCONV=1 kubectl -n "${NS}" exec -i probe -- python -c "import json,sys; d=json.load(sys.stdin); $1"; }

do_pods50() {
  local p="p50-$(date +%H%M%S)"
  echo "== $(ts) 50 個 Pod 同時送：每個每秒 40 行 log＋20 個 span，120 秒（${p}）"
  START_AT="$(now)"
  if has trace; then
    echo "{\"runs\":[{\"run\":\"${p}-log\",\"kind\":\"log\"},{\"run\":\"${p}-trace\",\"kind\":\"trace\"}],\"collectors\":$(collectors_json)}" | probe start "${p}"
    job "${p}" 50 log+trace PROFILE_log=120:40 PROFILE_trace=120:20
  else
    echo "{\"runs\":[{\"run\":\"${p}\",\"kind\":\"log\"}],\"collectors\":$(collectors_json)}" | probe start "${p}"
    job "${p}" 50 log PROFILE=120:40
  fi
  finish "${p}" 300 "${p}"
}

do_bigtrace() {
  local p="bigtr-$(date +%H%M%S)"
  echo "== $(ts) 一個 trace 1,000 段，每秒 2 個 trace（2,000 span／秒），120 秒（${p}）"
  START_AT="$(now)"
  echo "{\"runs\":[{\"run\":\"${p}\",\"kind\":\"trace\"}],\"collectors\":$(collectors_json)}" | probe start "${p}"
  job "${p}" 1 trace PROFILE=120:2000 SPANS=1000
  finish "${p}" 300 "${p}"
  echo "   每個 trace 收到幾段（段數 → trace 數）："
  es "/traces-*.otel-*/_search" POST "{\"size\":0,\"query\":{\"term\":{\"attributes.run\":\"${p}\"}},
    \"aggs\":{\"t\":{\"terms\":{\"field\":\"trace_id\",\"size\":1000}}}}" \
    | python_json 'import collections; c=collections.Counter(b["doc_count"] for b in d["aggregations"]["t"]["buckets"]); print("     ", dict(sorted(c.items())))'
}

do_cardinality() {
  local p="card-$(date +%H%M%S)"
  echo "== $(ts) 高基數：20,000 條序列、id 每 30 秒全換，300 秒（${p}）"
  START_AT="$(now)"
  echo "{\"runs\":[{\"run\":\"${p}\",\"kind\":\"prom\"}],\"collectors\":$(collectors_json)}" | probe start "${p}"
  job "${p}" 1 prom SERIES=20000 WINDOW=30 DURATION=300
  finish "${p}" 300 "${p}"
}

# ES 擋寫入（index.blocks.read_only_allow_delete，跟磁碟滿時 ES 自己加的一樣，回 429）
es_block() {  # es_block true|null
  es "/k8s-*,logs-*.otel-*,metrics-*.otel-*,traces-*.otel-*/_settings?expand_wildcards=all&allow_no_indices=true" PUT \
    "{\"index.blocks.read_only_allow_delete\":$1}" >/dev/null
}
do_reject() {
  local p="rej-$(date +%H%M%S)"
  echo "== $(ts) ES 拒收 ${DOWN} 秒（平常量，${p}）"
  trap 'set +e; es_block null' EXIT
  base_load "${p}" "$(( DOWN + 180 )):1"
  sleep 60; es_block true; echo "   $(ts) ES 開始拒收"
  sleep "${DOWN}"; es_block null; echo "   $(ts) ES 恢復"
  trap - EXIT
  finish "${p}" 600 "${RUNS[@]}"
}

# 佇列縮小：common.yaml（log、metrics、trace 送 ES，算筆數）留 20,000 筆
with_config() {  # with_config <sed 運算式> <ES_ENDPOINT>：只在這個情境改 Collector 設定，結束後換回 repo 的
  local tmp; tmp="$(mktemp -d)"
  cp "${repo}"/collector/code/*.yaml "${tmp}/"
  sed -i -E "$1" "${tmp}/common.yaml" "${tmp}/node.yaml" "${tmp}/aiops.yaml"
  CONFIG_DIR="${tmp}" "${repo}/e2e/k8s/deploy-collector.sh" --set "esEndpoint=$2"
  wait_collectors
  rm -rf "${tmp}"
}
restore_config() { "${repo}/e2e/k8s/deploy-collector.sh"; wait_collectors; }

do_queuefull() {
  local p="qf-$(date +%H%M%S)"
  echo "== $(ts) 佇列縮小、ES 拒收 ${DOWN} 秒（平常量，${p}）"
  with_config 's/queue_size: 20000000/queue_size: 20000/; s/queue_size: 10000,/queue_size: 20,/' http://elasticsearch.es-test.svc:9200
  trap 'set +e; es_block null; restore_config' EXIT  # 一步失敗也要做完還原
  base_load "${p}" "$(( DOWN + 180 )):1"
  sleep 60; es_block true; echo "   $(ts) ES 開始拒收"
  sleep "${DOWN}"; es_block null; echo "   $(ts) ES 恢復"
  finish "${p}" 600 "${RUNS[@]}"
  restore_config; trap - EXIT
}

do_slow() {
  local delay="${1:-5}" p
  p="slow${delay}-$(date +%H%M%S)"
  echo "== $(ts) ES 每個回應慢 ${delay} 秒，180 秒（平常量，${p}）"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: es-proxy, namespace: ${NS}}
spec:
  selector: {matchLabels: {app.kubernetes.io/name: es-proxy}}
  template:
    metadata: {labels: {app.kubernetes.io/name: es-proxy}}
    spec:
      containers:
        - name: proxy
          image: ${IMG}
          imagePullPolicy: IfNotPresent
          command: ["python", "-u", "/gen/loadgen.py"]
          env: [{name: MODE, value: proxy}, {name: DELAY, value: "0"}]
          volumeMounts: [{name: gen, mountPath: /gen}]
      volumes: [{name: gen, configMap: {name: loadgen}}]
---
apiVersion: v1
kind: Service
metadata: {name: es-proxy, namespace: ${NS}}
spec:
  selector: {app.kubernetes.io/name: es-proxy}
  ports: [{port: 9200, targetPort: 9200}]
EOF
  kubectl -n "${NS}" rollout status deploy/es-proxy --timeout=120s >/dev/null
  with_config 's/^$//' "http://es-proxy.${NS}.svc:9200"
  trap 'set +e; restore_config' EXIT
  local ctl="http://es-proxy.${NS}.svc:9200/_loadproxy"
  base_load "${p}" 240:1
  sleep 30
  MSYS_NO_PATHCONV=1 kubectl -n "${NS}" exec probe -- python -c "import urllib.request;print(urllib.request.urlopen('${ctl}?delay=${delay}').read().decode())" >/dev/null
  echo "   $(ts) ES 開始慢 ${delay} 秒"
  sleep 180
  MSYS_NO_PATHCONV=1 kubectl -n "${NS}" exec probe -- python -c "import urllib.request;print(urllib.request.urlopen('${ctl}?delay=0').read().decode())" >/dev/null
  echo "   $(ts) ES 恢復正常速度"
  finish "${p}" 600 "${RUNS[@]}"
  restore_config; trap - EXIT
  kubectl -n "${NS}" delete deploy/es-proxy svc/es-proxy >/dev/null
}

case "${1:-}" in
  smoke|log|metric|trace|burst|bigline|pods50|cardinality|bigtrace|reject|slow|queuefull) setup ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
case "$1" in
  log) shift; [[ $# -gt 0 ]] || set -- 5000 10000 20000; ramp log 5000 "$@" ;;
  metric) shift; [[ $# -gt 0 ]] || set -- 5000 20000 50000 100000; ramp metric 20000 "$@" ;;
  trace) shift; [[ $# -gt 0 ]] || set -- 1000 3000 6000 10000; ramp trace 2500 "$@" ;;
  smoke) do_smoke ;;
  burst) do_burst ;;
  bigline) do_bigline ;;
  pods50) do_pods50 ;;
  cardinality) do_cardinality ;;
  bigtrace) do_bigtrace ;;
  reject) do_reject ;;
  slow) do_slow "${2:-5}" ;;
  queuefull) do_queuefull ;;
esac
