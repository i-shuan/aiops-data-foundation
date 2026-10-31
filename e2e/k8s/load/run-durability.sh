#!/usr/bin/env bash
# 收值線故障測試：各種故障下 log、metrics、trace 會不會掉、會不會重複。每個情境：
#   - log：產生器每秒 LOG_RATE 行、每行帶序號（generator.yaml），數遺失、重複
#   - trace：產生器每秒 TRACE_RATE 個 trace、每個 2 段、帶序號（trace-gen.py，重試照 Java SDK 預設），
#     數遺失、不完整（只剩 1 段）、重複；「應用端丟」是 SDK 重試 5 次都失敗、自己丟的（Collector 收不到）
#   - metrics：節點 CPU（kubeletstats，每 30 秒一點；node Collector 抓），數點數、最大空檔、重複
#   - 遺失的時間點：相對於故障那一刻（t=0）的秒數，括號是筆數；相隔 2 秒以內的算同一段
# 每個情境自己拿 cluster 鎖、做完還原（ES 起來、Collector 正常、改過的設定改回去）再放鎖；中途失敗也會還原。
#
# 用法：e2e/k8s/load/run-durability.sh <情境...>
#   node-oom         node Collector 的 memory limit 調成 OOM_LIMIT（預設 150Mi），OOM_BURST 行/秒（預設 5000）衝 OOM_SECONDS 秒
#   node-kill        kill -9 node Collector 的程序，原地重啟
#   node-delete      kubectl delete pod --grace-period=0 --force node Collector
#   drain            單節點模擬 drain：cordon、趕走產生器、正常停 node Collector（關機）、uncordon
#   es-429           ES 寫入 thread pool 調成 1 條、佇列 ES429_QUEUE（預設 1），LOG_RATE_429 行/秒（預設 3000）
#   es-down          ES 停 DOWN_SECONDS 秒（預設 600）
#   hostpath-rm      node Collector 停 30 秒兩次：第一次保留 /var/lib/otelcol/node（對照），第二次停的時候刪掉
#   rolling          三種 Collector 一起換版到 TO_VERSION（預設 0.161.0）、60 秒後換回原版本
# 環境變數：GEN_NODE（多節點：產生器釘在這個節點，node Collector、metrics 也看這個節點；NODE_EXEC 要連到同一個節點）、ES_DOCKER（ES 是主機上的 docker 容器時設容器名，例如 k3d 實測用 es8；ES 起停、es-429 改用 docker／elasticsearch.yml）、PF_PORT（自己開 port-forward 到 ES，預設 31920）或 ES_URL（已經連得到就不開）、
#   NODE_EXEC（在節點上執行指令；Rancher Desktop 預設 wsl -d rancher-desktop --，在節點上跑就設成空字串）、
#   DEPLOY_COLLECTOR=1（每個情境開始前用 e2e/k8s/deploy-collector.sh 重新部署 repo 的 Collector 設定）、
#   LOG_RATE（200）、TRACE_RATE（20）、RESULTS（結果表另存一份 TSV）。
# 前提：run-e2e.sh 跑過（trace 100% 直接寫 ES，沒有 trace-sampler）。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../cluster-lock.sh
source "${here}/../cluster-lock.sh"
A=elastic:changeme-test-only
PF_PORT="${PF_PORT:-31920}"
if [[ -n "${ES_URL:-}" ]]; then PF_PORT=""; else ES_URL="http://127.0.0.1:${PF_PORT}"; fi
if [[ -z "${NODE_EXEC+x}" ]]; then
  if command -v wsl >/dev/null 2>&1; then NODE_EXEC="wsl -d rancher-desktop --"; else NODE_EXEC=""; fi
fi
LOG_RATE="${LOG_RATE:-200}"
TRACE_RATE="${TRACE_RATE:-20}"
RESULTS="${RESULTS:-}"
OC=otel-collector
IMG=docker.io/otel/opentelemetry-collector-contrib
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ts() { date -u +%H:%M:%S; }
epoch() { date +%s.%N | cut -c1-14; }
jq() { command jq "$@" | tr -d '\r'; }   # Windows 的 jq 輸出 CRLF
# 指令從 stdin 給 sh：wsl 會把參數再交給 shell 解析一次，引號、$( ) 會壞掉
on_node() { printf '%s\n' "$1" | MSYS_NO_PATHCONV=1 ${NODE_EXEC} sh -s | tr -d '\r'; }

# ---------- ES 連線（ES 重啟後 port-forward 會斷，自動重開） ----------
pf_pid=""
pf_up() {
  [[ -n "${PF_PORT}" ]] || { sleep 5; return 0; }
  [[ -n "${pf_pid}" ]] && kill "${pf_pid}" 2>/dev/null || true
  kubectl -n es-test port-forward --address 127.0.0.1 svc/elasticsearch "${PF_PORT}:9200" >/dev/null 2>&1 &
  pf_pid=$!
  for _ in $(seq 1 20); do curl -s -o /dev/null -m 2 "${ES_URL}" && return 0; sleep 1; done
  return 1
}
es() {
  local out
  for _ in 1 2 3 4 5 6; do
    if out="$(curl -sS -m 60 -u "${A}" -H 'Content-Type: application/json' "$@" 2>/dev/null)"; then printf '%s' "${out}"; return 0; fi
    pf_up || true
  done
  echo "ES 連不上：$*" >&2; return 1
}

# ---------- 還原：每做一個破壞性動作就登記怎麼還原；情境結束或腳本中途結束都會倒著執行 ----------
RESTORE=()
on_restore() { RESTORE+=("$1"); }
run_restores() {
  local i
  for (( i=${#RESTORE[@]}-1; i>=0; i-- )); do echo "   還原：${RESTORE[i]}"; eval "${RESTORE[i]}" || echo "   還原失敗：${RESTORE[i]}"; done
  RESTORE=()
}
on_exit() {
  set +e
  run_restores
  cluster_unlock
  [[ -n "${pf_pid}" ]] && kill "${pf_pid}" 2>/dev/null
}

# ---------- cluster 狀態 ----------
node_pod() { kubectl -n "${OC}" get pod -l app.kubernetes.io/name=otel-node-collector ${GEN_NODE:+--field-selector spec.nodeName=${GEN_NODE}} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }
restarts() { kubectl -n "$1" get pod "$2" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0; }
last_reason() { kubectl -n "$1" get pod "$2" -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}' 2>/dev/null; }
es_wait_green() { until [[ "$(es "${ES_URL}/_cluster/health" | jq -r .status)" =~ green|yellow ]]; do sleep 2; done; }
es_up() {
  if [[ -n "${ES_DOCKER:-}" ]]; then
    docker start "${ES_DOCKER}" >/dev/null 2>&1 || true; docker unpause "${ES_DOCKER}" >/dev/null 2>&1 || true
    es_wait_green; return
  fi
  kubectl -n es-test scale statefulset/elasticsearch --replicas=1 >/dev/null
  until kubectl -n es-test get pod elasticsearch-0 >/dev/null 2>&1; do sleep 2; done
  kubectl -n es-test wait --for=condition=Ready pod/elasticsearch-0 --timeout=600s >/dev/null
  pf_up
  es_wait_green
}
es_down() {
  if [[ -n "${ES_DOCKER:-}" ]]; then docker stop "${ES_DOCKER}" >/dev/null; return; fi
  kubectl -n es-test scale statefulset/elasticsearch --replicas=0 >/dev/null
  kubectl -n es-test wait --for=delete pod/elasticsearch-0 --timeout=180s >/dev/null 2>&1 || true
}
wait_collectors() {
  kubectl -n "${OC}" rollout status daemonset/otel-node-collector --timeout=300s >/dev/null
  kubectl -n "${OC}" rollout status deployment/otel-cluster-collector --timeout=300s >/dev/null
  kubectl -n "${OC}" wait --for=condition=Ready pod -l app.kubernetes.io/name=otel-node-collector --timeout=300s >/dev/null
}
ensure_ready() {
  # 別的工作可能在沒鎖的時候部署了別版設定：每個情境拿到鎖後重新部署要測的那份
  if [[ -n "${DEPLOY_COLLECTOR:-}" ]]; then "${here}/../deploy-collector.sh"; sleep 3; fi
  if [[ -n "${ES_DOCKER:-}" ]]; then es_up
  else
    [[ "$(kubectl -n es-test get statefulset elasticsearch -o jsonpath='{.spec.replicas}')" == 1 ]] || es_up
    kubectl -n es-test wait --for=condition=Ready pod/elasticsearch-0 --timeout=600s >/dev/null
    pf_up
    es_wait_green
  fi
  wait_collectors
}
# 等某個容器原地重啟完（restartCount 變了、Ready）
wait_restarted() {  # wait_restarted <ns> <pod> <原本的 restartCount>
  until [[ "$(restarts "$1" "$2")" != "$3" ]]; do sleep 1; done
  kubectl -n "$1" wait --for=condition=Ready "pod/$2" --timeout=300s >/dev/null
}

# ---------- 產生器 ----------
RUN=""; GEN_START=""; T0=""; MARKS=""
mark() { MARKS+="$(ts) $1；"; echo "   $(ts) $1"; }
start_gens() {  # start_gens <run> <log 行/秒> <trace/秒> <秒數>
  RUN="$1"; MARKS=""
  kubectl apply -f "${here}/generator.yaml" >/dev/null
  jq -Rs '{apiVersion:"v1",kind:"ConfigMap",metadata:{name:"trace-gen",namespace:"load-test"},data:{"trace-gen.py":.}}' \
    <"${here}/trace-gen.py" | kubectl apply -f - >/dev/null
  sed -e "s/\${RUN}/$1/g" -e "s/\${RATE}/$2/g" -e "s/\${DURATION}/$4/g" ${GEN_NODE:+-e "s/^      restartPolicy: Never/      nodeName: ${GEN_NODE}\n      restartPolicy: Never/"} "${here}/job.tpl.yaml" | kubectl apply -f - >/dev/null
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata: {name: tgen-$1, namespace: load-test}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels: {app.kubernetes.io/name: dur-trace, app.kubernetes.io/version: "1.0.0"}
    spec:
${GEN_NODE:+      nodeName: ${GEN_NODE}}
      restartPolicy: Never
      containers:
        - name: tgen
          image: docker.io/library/python:3.12-alpine
          command: ["python", "-u", "/g/trace-gen.py"]
          env: [{name: RATE, value: "$3"}, {name: DURATION, value: "$4"}, {name: RUN, value: "$1"}]
          volumeMounts: [{name: g, mountPath: /g}]
      volumes: [{name: g, configMap: {name: trace-gen}}]
EOF
  GEN_START="$(now)"
  echo "   $(ts) 產生器開始：log ${2} 行/秒、trace ${3} 個/秒、${4} 秒（run=$1）"
}
wait_job() { until kubectl -n load-test get job "$1" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do sleep 5; done; }
# 產生器最後印的那行 JSON：先讀節點上的 log 檔，沒有（Pod 被刪了）就查 ES 裡那一行
gen_summary() {  # gen_summary <job> <container> <欄位>
  local s
  s="$(on_node "cat /var/log/pods/load-test_$1-*/$2/*.log 2>/dev/null" | grep -o '{"run".*}' | tail -1 || true)"
  if [[ -z "${s}" ]]; then
    es -X POST "${ES_URL}/k8s-imc-local/_refresh" >/dev/null
    s="$(es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":1,\"query\":{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"${RUN}\"}},
      {\"exists\":{\"field\":\"app.$3\"}}]}}}" | jq -c '.hits.hits[0]._source.app // empty')"
  fi
  echo "${s}"
}

# ---------- 數 ES ----------
LOG_Q() { echo "{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"${RUN}\"}},{\"exists\":{\"field\":\"app.seq\"}}]}}"; }
TRACE_Q() { echo "{\"term\":{\"attributes.lt.run\":\"${RUN}\"}}"; }
seq_counts() {  # seq_counts <index> <query> <欄位> → 每行「序號 筆數」
  local after="" resp
  while :; do
    resp="$(es "${ES_URL}/$1/_search" -d "{\"size\":0,\"query\":$2,\"aggs\":{\"s\":{\"composite\":{\"size\":10000,
      \"sources\":[{\"q\":{\"terms\":{\"field\":\"$3\"}}}]${after:+,\"after\":${after}}}}}}")"
    jq -r '.aggregations.s.buckets[] | "\(.key.q) \(.doc_count)"' <<<"${resp}"
    after="$(jq -c '.aggregations.s.after_key // empty' <<<"${resp}")"
    [[ -n "${after}" ]] || break
  done
}
# 序號 1..N 每個應有 PER 筆：印出「遺失 不完整 重複｜遺失的時間｜重複的時間」。SKIP 是不算的序號（應用端丟的，a-b a-b）
analyze() {  # analyze <N> <start epoch> <rate> <PER> <SKIP>   （stdin：seq_counts 的輸出）
  awk -v N="$1" -v S="$2" -v R="$3" -v PER="$4" -v SKIP="$5" -v T0="${T0}" '
    function rel(s) { return S + (s - 1) / R - T0 }
    function flush(k) { if (!(k in le)) return; nr[k]++
      if (nr[k] <= 4) out[k] = out[k] sprintf("%s%+.1f～%+.1fs(%d)", out[k] == "" ? "" : " ", rel(ls[k]), rel(le[k]) + 1 / R, cnt[k])
      else if (nr[k] == 5) out[k] = out[k] " …"
      delete le[k]; delete ls[k]; cnt[k] = 0 }
    function add(k, s, w) { if ((k in le) && s - le[k] <= 2 * R) { le[k] = s; cnt[k] += w; return } flush(k); ls[k] = s; le[k] = s; cnt[k] = w }
    BEGIN { n = split(SKIP, sk, " "); for (i = 1; i <= n; i++) { split(sk[i], ab, "-"); for (j = ab[1]; j <= ab[2]; j++) skip[j] = 1 } }
    { c[$1 + 0] += $2 }
    END {
      for (i = 1; i <= N; i++) {
        if (i in skip) continue
        k = (i in c) ? c[i] : 0
        if (k == 0) { lost++; add("L", i, 1) } else if (k < PER) { inc++; add("L", i, 1) } else if (k > PER) { dup += k - PER; add("D", i, k - PER) }
      }
      flush("L"); flush("D")
      printf "%d %d %d|%s|%s\n", lost, inc, dup, out["L"], out["D"]
    }'
}
# 節點 CPU 的點：「點數 預期 重複 最大空檔秒數｜超過 40 秒的空檔（相對 t=0）」
metric_gaps() {  # metric_gaps <from> <to>
  local node; node="$(kubectl -n "${OC}" get pod "$(node_pod)" -o jsonpath='{.spec.nodeName}')"
  es "${ES_URL}/metrics-kubeletstatsreceiver.otel-unassigned/_search" -d "{\"size\":5000,\"_source\":false,
    \"docvalue_fields\":[{\"field\":\"@timestamp\",\"format\":\"epoch_millis\"}],\"sort\":[{\"@timestamp\":\"asc\"}],
    \"query\":{\"bool\":{\"filter\":[{\"exists\":{\"field\":\"metrics.k8s.node.cpu.usage\"}},{\"term\":{\"resource.attributes.k8s.node.name\":\"${node}\"}},
    {\"range\":{\"@timestamp\":{\"gte\":\"$1\",\"lt\":\"$2\"}}}]}}}" \
  | jq -r --argjson t0 "${T0}" --argjson span "$(( $(date -u -d "$2" +%s) - $(date -u -d "$1" +%s) ))" '
      [.hits.hits[].fields["@timestamp"][0] | tonumber / 1000] as $t
      | [range(1; $t | length) | {a: $t[. - 1], d: ($t[.] - $t[. - 1])}] as $g
      | "\($t | length) \($span / 30 | floor) \([$g[] | select(.d == 0)] | length) \([$g[].d] | max // 0 | floor)|"
        + ([$g[] | select(.d > 40) | "\(.a - $t0 | floor)s 起 \(.d | floor)s"] | join(" "))'
}
# 等資料補進 ES：log、trace 都到齊，或 STABLE 秒沒有再增加
catch_up() {  # catch_up <log 預期> <trace 段數預期>
  local last="" cur same=0
  while (( same < ${STABLE:-90} )); do
    es -X POST "${ES_URL}/k8s-imc-local,traces-generic.otel-imc/_refresh" >/dev/null
    cur="$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":$(LOG_Q)}" | jq .count) $(es "${ES_URL}/traces-generic.otel-imc/_count" -d "{\"query\":$(TRACE_Q)}" | jq .count)"
    read -r l t <<<"${cur}"
    (( l >= $1 && t >= $2 )) && { sleep 20; return; }
    if [[ "${cur}" == "${last}" ]]; then same=$(( same + 10 )); else same=0; last="${cur}"; fi
    sleep 10
  done
}

SUMMARY=()
finish() {  # finish <情境名稱>：等產生器結束、等補送，量、印這個情境的結果
  local ls lsum emitted lstart tsum tsent tstart tdrop skip end la ta mg n
  wait_job "gen-${RUN}"; wait_job "tgen-${RUN}"
  end="$(now)"
  lsum="$(gen_summary "gen-${RUN}" gen emitted)"; tsum="$(gen_summary "tgen-${RUN}" tgen trace_sent)"
  emitted="$(jq -r .emitted <<<"${lsum}")"; lstart="$(jq -r .start <<<"${lsum}")"
  tsent="$(jq -r .trace_sent <<<"${tsum}")"; tstart="$(jq -r .start <<<"${tsum}")"; tdrop="$(jq -r .trace_app_dropped <<<"${tsum}")"
  skip="$(jq -r '[.trace_app_dropped_seqs[]? | "\(.[0])-\(.[1])"] | join(" ")' <<<"${tsum}")"
  echo "   $(ts) 產生器結束：log ${emitted} 行、trace ${tsent} 個（應用端丟 ${tdrop}）；等補送"
  catch_up "${emitted}" $(( (tsent - tdrop) * 2 ))
  la="$(seq_counts k8s-imc-local "$(LOG_Q)" app.seq | analyze "${emitted}" "${lstart}" "${LOG_RATE_USED}" 1 "")"
  ta="$(seq_counts traces-generic.otel-imc "$(TRACE_Q)" attributes.lt.seq | analyze "${tsent}" "${tstart}" "${TRACE_RATE_USED}" 2 "${skip}")"
  mg="$(metric_gaps "${GEN_START}" "${end}")"
  read -r ll li ld <<<"${la%%|*}"; read -r tl ti td <<<"${ta%%|*}"; read -r mn me md mx <<<"${mg%%|*}"
  echo "   ── $1（t=0 是 ${T0_LABEL:-故障}）：${MARKS}"
  printf "   log    ：送出 %s、遺失 %s、重複 %s　遺失時間 [%s]　重複時間 [%s]\n" "${emitted}" "${ll}" "${ld}" "$(cut -d'|' -f2 <<<"${la}")" "$(cut -d'|' -f3 <<<"${la}")"
  printf "   trace  ：送出 %s、應用端丟 %s、遺失 %s、不完整 %s、重複段 %s　遺失時間 [%s]\n" "${tsent}" "${tdrop}" "${tl}" "${ti}" "${td}" "$(cut -d'|' -f2 <<<"${ta}")"
  printf "   metrics：%s 點（預期約 %s）、重複 %s、最大間隔 %ss　超過 40 秒的空檔 [%s]\n" "${mn}" "${me}" "${md}" "${mx}" "${mg#*|}"
  SUMMARY+=("$(printf '%s\t%s/%s\t%s/%s/%s/%s\t%ss/%s\t%s' "$1" "${ll}" "${ld}" "${tl}" "${ti}" "${td}" "${tdrop}" "${mx}" "${md}" "${NOTE:-}")")
  [[ -n "${RESULTS}" ]] && printf '%s\t%s\t%s\t%s\t%s\n' "$(now)" "${SUMMARY[-1]}" "${la#*|}" "${ta#*|}" "${mg#*|}" >>"${RESULTS}"
  kubectl -n load-test delete job "gen-${RUN}" "tgen-${RUN}" --ignore-not-found >/dev/null
  NOTE=""
}
begin() {  # begin <名稱> <秒數> [log 行/秒] [trace/秒]
  LOG_RATE_USED="${3:-${LOG_RATE}}"; TRACE_RATE_USED="${4:-${TRACE_RATE}}"; T0_LABEL=""
  start_gens "$1-$(date +%H%M%S)" "${LOG_RATE_USED}" "${TRACE_RATE_USED}" "$2"
}
fault() { T0="$(epoch)"; mark "t=0 $1"; }
proc_pid() {  # proc_pid <設定檔片段> <Pod 名稱>：節點上這個 Pod 的 Collector 程序
  # 不用 pgrep（k3s 的節點沒有）：掃 /proc，指令列對得上、環境變數 HOSTNAME 是這個 Pod
  on_node "for d in /proc/[0-9]*; do { tr '\\0' ' ' <\$d/cmdline | grep -q 'otelcol-contrib --config=/conf/$1' && grep -qa 'HOSTNAME=$2' \$d/environ && echo \${d#/proc/}; } 2>/dev/null; done; true" | head -1
}
queue_du() {  # 節點上 Collector 佇列目錄用多少（KB）
  echo "node=$(on_node "du -sk /var/lib/otelcol/node | cut -f1")KB"
}
# node Collector 自己報的佇列長度（請求數）／上限，每種資料一個
queue_fill() {
  kubectl get --raw "/api/v1/namespaces/${OC}/pods/$(node_pod):8888/proxy/metrics" 2>/dev/null     | awk '/^otelcol_exporter_queue_(size|capacity)\{/ { match($0, /data_type="[a-z]+"/); t = substr($0, RSTART + 11, RLENGTH - 12)
        if ($0 ~ /queue_size/) sz[t] = $NF; else cap[t] = $NF } END { for (t in sz) printf "%s %d/%d ", t, sz[t], cap[t] }'
}

# ---------- 情境 ----------
sc_node_oom() {
  local lim="${OOM_LIMIT:-150Mi}" cur req pod r0 burst
  req="$(kubectl -n "${OC}" get daemonset otel-node-collector -o jsonpath='{.spec.template.spec.containers[0].resources.requests.memory}')"
  cur="$(kubectl -n "${OC}" get daemonset otel-node-collector -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')"
  begin "node-oom" $(( 60 + ${OOM_SECONDS:-180} + 180 ))
  sleep 60
  fault "node Collector memory limit ${cur} → ${lim}（換一次 Pod）"
  kubectl -n "${OC}" set resources daemonset/otel-node-collector -c otelcol --limits=memory="${lim}" --requests=memory=64Mi >/dev/null
  on_restore "kubectl -n ${OC} set resources daemonset/otel-node-collector -c otelcol --limits=memory=${cur} --requests=memory=${req} >/dev/null; kubectl -n ${OC} rollout status daemonset/otel-node-collector --timeout=300s >/dev/null"
  kubectl -n "${OC}" rollout status daemonset/otel-node-collector --timeout=300s >/dev/null
  pod="$(node_pod)"; r0="$(restarts "${OC}" "${pod}")"; mark "新 Pod 起來（limit ${lim}）"
  burst="burst-$(date +%H%M%S)"
  sed -e "s/\${RUN}/${burst}/g" -e "s/\${RATE}/${OOM_BURST:-5000}/g" -e "s/\${DURATION}/${OOM_SECONDS:-180}/g" "${here}/job.tpl.yaml" | kubectl apply -f - >/dev/null
  mark "另一個產生器每秒 ${OOM_BURST:-5000} 行、${OOM_SECONDS:-180} 秒（不計）"
  wait_job "gen-${burst}"
  NOTE="OOMKilled $(( $(restarts "${OC}" "${pod}") - r0 )) 次（最後一次：$(last_reason "${OC}" "${pod}")）"
  mark "${NOTE}"
  kubectl -n load-test delete job "gen-${burst}" --ignore-not-found >/dev/null
  run_restores; mark "limit 改回 ${cur}（換一次 Pod）"
  finish "node-oom（${lim}）"
}

sc_node_kill() {
  local pod r pid
  begin "node-kill" 240
  sleep 90
  pod="$(node_pod)"; r="$(restarts "${OC}" "${pod}")"; pid="$(proc_pid common.yaml "${pod}")"
  [[ -n "${pid}" ]] || { echo "找不到 node Collector 的程序"; exit 1; }
  fault "kill -9 node Collector"; on_node "kill -9 ${pid}"
  wait_restarted "${OC}" "${pod}" "${r}"; mark "原地重啟完成"
  finish "node-kill"
}

sc_node_delete() {
  local pod
  begin "node-delete" 240
  sleep 90
  pod="$(node_pod)"
  fault "kubectl delete pod --grace-period=0 --force（${pod}）"
  kubectl -n "${OC}" delete pod "${pod}" --grace-period=0 --force >/dev/null 2>&1
  sleep 3; kubectl -n "${OC}" wait --for=condition=Ready pod -l app.kubernetes.io/name=otel-node-collector --timeout=300s >/dev/null
  mark "新 Pod $(node_pod) Ready（重啟 $(restarts "${OC}" "$(node_pod)") 次）"
  finish "node-delete"
}

sc_drain() {
  local node sel
  node="$(kubectl -n "${OC}" get pod "$(node_pod)" -o jsonpath='{.spec.nodeName}')"
  begin "drain" 240
  sleep 90
  fault "drain ${node}：cordon，趕走 log、trace 產生器"
  on_restore "kubectl uncordon ${node} >/dev/null"
  for sel in 'app.kubernetes.io/name in (load-gen,dur-trace)'; do
    kubectl drain "${node}" --pod-selector="${sel}" --ignore-daemonsets --delete-emptydir-data --timeout=180s >/dev/null 2>&1 || true
  done
  mark "drain 完"
  sleep 10
  ds_stop; on_restore "ds_start"; mark "正常停 node Collector（模擬關機），停 30 秒"
  sleep 30
  kubectl uncordon "${node}" >/dev/null; ds_start; RESTORE=(); mark "uncordon、node Collector 起來"
  wait_collectors; mark "Collector 都 Ready"
  NOTE="單節點：Pod 被趕走後沒有別的節點可去，uncordon 才回來"
  finish "drain"
}

# ES 寫入 thread pool 到現在拒絕了幾次（ES 剛起來時 _nodes/stats 可能還沒有 nodes，等到有為止）
rejected() {
  local n
  until n="$(es "${ES_URL}/_nodes/stats/thread_pool?filter_path=nodes.*.thread_pool.write.rejected" | jq -e '[.nodes[].thread_pool.write.rejected] | add' 2>/dev/null)"; do sleep 2; done
  echo "${n}"
}
collector_429() { kubectl -n "${OC}" logs "$1" --since=10m 2>/dev/null | grep 'Will retry' | grep -c 'Too Many Requests\|429' || true; }
sc_es_429() {
  local q="${ES429_QUEUE:-1}" r0 r1
  echo "   ES 寫入 thread pool 改成 1 條、佇列 ${q}（重啟 ES）"
  if [[ -n "${ES_DOCKER:-}" ]]; then   # thread_pool 是啟動時才讀的設定：寫進容器裡的 elasticsearch.yml 再重啟（容器沒有 volume，不能重建）
    docker exec "${ES_DOCKER}" sh -c "printf 'thread_pool.write.size: 1\nthread_pool.write.queue_size: ${q}\n' >> config/elasticsearch.yml"
    on_restore "docker exec ${ES_DOCKER} sed -i '/^thread_pool.write/d' config/elasticsearch.yml; docker restart ${ES_DOCKER} >/dev/null; es_wait_green"
    docker restart "${ES_DOCKER}" >/dev/null; es_wait_green
  else
    kubectl -n es-test set env statefulset/elasticsearch thread_pool.write.size=1 thread_pool.write.queue_size="${q}" >/dev/null
    on_restore "kubectl -n es-test set env statefulset/elasticsearch thread_pool.write.size- thread_pool.write.queue_size- >/dev/null; kubectl -n es-test rollout status statefulset/elasticsearch --timeout=600s >/dev/null; es_up"
    kubectl -n es-test rollout status statefulset/elasticsearch --timeout=600s >/dev/null; es_up
  fi
  r0="$(rejected)"
  begin "es-429" 240 "${LOG_RATE_429:-3000}"
  T0="$(epoch)"; T0_LABEL="產生器開始"
  wait_job "gen-${RUN}"
  r1="$(rejected)"
  NOTE="write rejected $(( r1 - r0 )) 次"
  # 429 時 Collector 回頭重試、退避最多 30 秒，限流期間進度很慢：先把 ES 改回來，再等補完、數
  mark "${NOTE}；Collector 收到整批 429：$(collector_429 "$(node_pod)") 次（node）"
  run_restores; mark "ES thread pool 改回預設（重啟 ES）"
  finish "es-429（佇列 ${q}）"
}

sc_es_down() {
  local down="${DOWN_SECONDS:-600}"
  begin "es-down-${down}s" $(( 60 + down + 120 ))
  sleep 60
  fault "ES 停 ${down} 秒"; es_down; on_restore "es_up"
  sleep $(( down - 5 ))
  local du; du="$(queue_du)"; mark "佇列：${du}；佇列裡幾個請求／上限：$(queue_fill)"
  es_up; RESTORE=(); mark "ES 起來、可以用"
  NOTE="停機結束時佇列 ${du}"
  finish "es-down（${down}s）"
}

ds_stop() { kubectl -n "${OC}" patch daemonset otel-node-collector -p '{"spec":{"template":{"spec":{"nodeSelector":{"durability-test":"stopped"}}}}}' >/dev/null
  kubectl -n "${OC}" wait --for=delete pod -l app.kubernetes.io/name=otel-node-collector --timeout=180s >/dev/null 2>&1 || true; }
ds_start() { kubectl -n "${OC}" patch daemonset otel-node-collector --type=json -p '[{"op":"remove","path":"/spec/template/spec/nodeSelector"}]' >/dev/null 2>&1 || true
  sleep 3; kubectl -n "${OC}" rollout status daemonset/otel-node-collector --timeout=300s >/dev/null
  kubectl -n "${OC}" wait --for=condition=Ready pod -l app.kubernetes.io/name=otel-node-collector --timeout=300s >/dev/null; }
sc_hostpath_rm() {
  begin "hostpath-rm" 330
  sleep 60
  mark "對照：node Collector 停 30 秒（保留 /var/lib/otelcol/node）"; ds_stop; on_restore "ds_start"
  sleep 30; ds_start; RESTORE=(); mark "node Collector 起來"
  sleep 60
  fault "node Collector 停 30 秒，停的時候刪 /var/lib/otelcol/node"; ds_stop; on_restore "ds_start"
  on_node "rm -rf /var/lib/otelcol/node"
  sleep 30; ds_start; RESTORE=(); mark "node Collector 起來（目錄重建：$(on_node "ls /var/lib/otelcol/node | tr '\n' ' '")）"
  finish "hostpath-rm"
}

sc_rolling() {
  local to="${TO_VERSION:-0.161.0}" from
  from="$(kubectl -n "${OC}" get daemonset otel-node-collector -o jsonpath='{.spec.template.spec.containers[0].image}')"
  from="${from##*:}"
  echo "   先把 ${IMG}:${to} 拉到節點上（換版時間不算拉 image）"
  kubectl -n load-test delete pod prepull --ignore-not-found >/dev/null 2>&1 || true
  kubectl apply -f "${here}/generator.yaml" >/dev/null
  kubectl -n load-test run prepull --image="${IMG}:${to}" --restart=Never -- --version >/dev/null
  kubectl -n load-test wait --for=jsonpath='{.status.phase}'=Succeeded pod/prepull --timeout=300s >/dev/null
  kubectl -n load-test delete pod prepull >/dev/null
  set_ver() {
    kubectl -n "${OC}" set image daemonset/otel-node-collector otelcol="${IMG}:$1" >/dev/null
    kubectl -n "${OC}" set image deployment/otel-cluster-collector otelcol="${IMG}:$1" >/dev/null
    wait_collectors
  }
  begin "rolling" 300
  sleep 60
  on_restore "set_ver ${from}"
  fault "三種 Collector 換版 ${from} → ${to}"; set_ver "${to}"; mark "換版完成"
  sleep 60
  mark "換回 ${from}"; set_ver "${from}"; RESTORE=(); mark "換回完成"
  NOTE="${from} → ${to} → ${from}"
  finish "rolling"
}

# ---------- 主程式 ----------
[[ $# -gt 0 ]] || { sed -n '2,33p' "$0"; exit 2; }
for s in "$@"; do
  fn="sc_${s//-/_}"
  declare -F "${fn}" >/dev/null || { echo "沒有這個情境：${s}"; exit 2; }
done
for s in "$@"; do
  echo
  echo "== $(ts) ${s}"
  cluster_lock "p1-durability:${s}"
  trap on_exit EXIT
  ensure_ready
  "sc_${s//-/_}"
  run_restores
  ensure_ready
  cluster_unlock
  trap on_exit EXIT
done

echo
echo "== 結果（遺失/重複；trace 是 遺失/不完整/重複段/應用端丟；metrics 是 最大間隔/重複點）"
printf '%s\n' "情境	log	trace	metrics	備註" "${SUMMARY[@]}" | { column -t -s $'\t' 2>/dev/null || cat; }
