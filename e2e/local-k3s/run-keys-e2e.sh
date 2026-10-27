#!/usr/bin/env bash
# 共同鑰匙圈在 K8s 上的驗證：同一個請求的 trace、log（stdout、OTLP）、metrics（Prometheus、OTLP），
# 用 service.name、k8s.namespace.name、k8s.pod.name、dept、group、k8s.cluster.name 交叉查得到，而且值一樣。
# 邊緣情境見 keys-apps.yaml；另外測改 namespace 的 dept、容器重啟、改服務名稱（新 Pod）。
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector 都在）。會用這個 checkout 的 Collector 設定與 ES pipeline 重新部署。約 10 分鐘。
# 用法：ES_URL=http://127.0.0.1:<port> e2e/local-k3s/run-keys-e2e.sh
#       KEEP=1：跑完不刪測試 namespace；ONE_SAMPLER=1：trace-sampler 暫時縮成 1 台（繞過 loadbalancing 的佇列檔問題，見步驟 1）
# 共用 cluster：先 source cluster-lock.sh; cluster_lock <名字>。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
CLUSTER=local
NSS="keys-it keys-imc keys-nodept keys-baddept keys-switch"
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
pod_of() { kubectl -n "$1" get pod -l "case=$2" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'; }

# 五種資料各自在哪、用哪個欄位找回這個情境的資料、鑰匙的欄位名稱
SRCS="trace log_stdout log_otlp met_prom met_otlp"
idx_of() {  # idx_of <來源> [group]：不給 group 就是全部 group
  local g="${2:-*}"
  case $1 in trace) echo "traces-*.otel-${g}";; log_*) echo "k8s-${g}-*";; met_*) echo "metrics-*.otel-${g}";; esac
}
filter_of() {  # filter_of <來源> <情境>
  case $1 in
    trace)      echo "{\"term\":{\"attributes.case\":\"$2\"}}";;
    log_stdout) echo "{\"term\":{\"app.case\":\"$2\"}},{\"term\":{\"app.src\":\"stdout\"}}";;
    log_otlp)   echo "{\"term\":{\"app.case\":\"$2\"}},{\"term\":{\"app.src\":\"otlp\"}}";;
    met_prom)   echo "{\"term\":{\"attributes.case\":\"$2\"}},{\"exists\":{\"field\":\"metrics.keys_requests_total\"}}";;
    met_otlp)   echo "{\"term\":{\"attributes.case\":\"$2\"}},{\"exists\":{\"field\":\"metrics.keys.otlp.requests\"}}";;
  esac
}
R=resource.attributes
fields_of() {  # 鑰匙的欄位：log 的 dept、group 在 labels.*，其他用 alias，跟 metrics、trace 同一條路徑
  case $1 in
    log_*) echo "[\"${R}.service.name\",\"${R}.k8s.namespace.name\",\"${R}.k8s.pod.name\",\"labels.dept\",\"labels.group\",\"${R}.k8s.cluster.name\"]";;
    *)     echo "[\"${R}.service.name\",\"${R}.k8s.namespace.name\",\"${R}.k8s.pod.name\",\"${R}.dept\",\"${R}.group\",\"${R}.k8s.cluster.name\"]";;
  esac
}
# keys <來源> <情境> <從何時起> → 最新一筆的「服務|namespace|Pod|dept|group|cluster|寫進哪個 group 的 index」
keys() {
  es "${ES_URL}/$(idx_of "$1")/_search" -d "{\"size\":1,\"sort\":[{\"@timestamp\":\"desc\"}],\"_source\":false,
    \"fields\":$(fields_of "$1"),\"query\":{\"bool\":{\"filter\":[$(filter_of "$1" "$2"),{\"range\":{\"@timestamp\":{\"gte\":\"$3\"}}}]}}}" \
  | jq -r --argjson f "$(fields_of "$1")" '.hits.hits[0] // empty | . as $h
      | [$f[] | ($h.fields[.] // [""])[0]] + [$h._index | capture("(otel-|k8s-)(?<g>[a-z]+)").g // ""] | join("|")'
}
count_since() {  # count_since <來源> <情境> <從何時起> [額外條件]
  es "${ES_URL}/$(idx_of "$1")/_count" -d "{\"query\":{\"bool\":{\"filter\":[$(filter_of "$1" "$2"),{\"range\":{\"@timestamp\":{\"gte\":\"$3\"}}}${4:+,$4}]}}}" | jq -r '.count // 0'
}
refresh() { es -X POST "${ES_URL}/traces-*.otel-*,metrics-*.otel-*,k8s-*/_refresh" >/dev/null; }
# check_all <說明> <情境> <從何時起> <預期> [只看這些來源]：五種資料各比一次
check_all() {
  local s; for s in ${5:-${SRCS}}; do check "$1：${s}" "$(keys "${s}" "$2" "$3")" "$4"; done
}

echo "== 0. 靜態：groups.yaml 的 dept 跟 common.yaml 的 transform/group 一致"
yaml_depts="$(sed -n 's/^ *depts: *\[\(.*\)\]/\1/p' "${repo}/telemetry/groups.yaml" | tr -d ' ' | tr ',' '\n' | sort | paste -sd, -)"
rule_depts="$(grep -o 'IsMatch(resource.attributes\["dept"\], "\^([^)]*)' "${repo}/telemetry/collectors/common.yaml" | sed 's/.*(\(.*\)/\1/' | tr -d ')' | tr '|' '\n' | sort | paste -sd, -)"
check "dept 清單兩處一樣" "${rule_depts}" "${yaml_depts}"

t_start=$(date +%s)
echo "== 1. API key、ES pipeline、Collector（這個 checkout 的設定）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/telemetry/scripts/elasticsearch-setup.sh")"
key="$(echo "${setup_out}" | grep -A1 '=== 寫入 key：es-writer ===' | tail -1 | jq -r .encoded)"
[[ -n "${key}" && "${key}" != null ]] || { echo "沒拿到寫入 key"; echo "${setup_out}"; exit 1; }
kubectl -n otel-collector create secret generic es-writer-keys --from-literal=ES_WRITER_API_KEY="${key}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -k "${here}/otel-collector" >/dev/null
kubectl -n otel-collector rollout status statefulset/trace-sampler --timeout=180s >/dev/null
kubectl -n otel-collector rollout status deployment/otel-cluster-collector --timeout=180s >/dev/null
# trace-sampler 有 2 台時，node Collector 的 loadbalancing 只建得起一台的子 exporter（兩個子 exporter 共用一個磁碟佇列檔，
# 第二個等檔案鎖逾時），trace 整批被丟（見 troubleshooting）。ONE_SAMPLER=1：測試期間縮成 1 台，結束時恢復
if [[ -n "${ONE_SAMPLER:-}" ]]; then
  kubectl -n otel-collector scale statefulset/trace-sampler --replicas=1 >/dev/null
  kubectl -n otel-collector wait --for=delete pod/trace-sampler-1 --timeout=120s >/dev/null 2>&1 || true
  trap 'kubectl -n otel-collector scale statefulset/trace-sampler --replicas=2 >/dev/null; kubectl -n otel-collector rollout restart daemonset/otel-node-collector >/dev/null' EXIT
fi
kubectl -n otel-collector rollout restart daemonset/otel-node-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
sleep 5
if kubectl -n otel-collector logs ds/otel-node-collector | grep -q 'failed to start new exporter'; then
  echo "   注意：node Collector 的 loadbalancing 有子 exporter 建不起來，trace 會被丟，trace 那幾項會 FAIL（ONE_SAMPLER=1 可繞過）"
fi

echo "== 2. 測試應用"
t_apps=$(date +%s)
# apply 會把上一次改過的 keys-switch 的 dept、base 的服務名稱改回來
kubectl apply -f "${here}/keys-apps.yaml" >/dev/null
for ns in ${NSS}; do
  kubectl -n "${ns}" create configmap keys-app --from-literal=run.sh="$(tr -d '\r' < "${here}/keys-app.sh")" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "${ns}" rollout restart deployment >/dev/null
done
for ns in ${NSS}; do kubectl -n "${ns}" rollout status deployment --timeout=180s >/dev/null; done
sleep 5   # 舊 Pod 刪完
t0=$(date +%s); since="$(iso "${t0}")"
echo "   ${since} 起的資料，等 75 秒（Prometheus 30 秒抓一次）"
sleep 75; refresh

it_pod() { pod_of keys-it "$1"; }
echo "== 3. 一般情況：五種資料的鑰匙一樣"
check_all "base" base "${since}" "keys-a|keys-it|$(it_pod base)|D3120|it|${CLUSTER}|it"

echo "== 4. Pod 沒有 app.kubernetes.io/name：退回 Deployment 名稱"
check_all "nolabel" nolabel "${since}" "nolabel-b|keys-it|$(it_pod nolabel)|D3120|it|${CLUSTER}|it"

echo "== 5. 應用自己的 OTEL_SERVICE_NAME（keys-c-own）跟 Pod label（keys-c）不同：一律用 Pod label"
check_all "conflict" conflict "${since}" "keys-c|keys-it|$(it_pod conflict)|D3120|it|${CLUSTER}|it"

echo "== 6. 冒充：自帶 keys-imc、fake-pod、D1110、imc、cluster other：一律拿掉、依 Pod 補"
check_all "spoof" spoof "${since}" "keys-d|keys-it|$(it_pod spoof)|D3120|it|${CLUSTER}|it"
check "spoof：IMC 的 index 裡沒有冒充的資料" \
  "$(( $(es "${ES_URL}/traces-*.otel-imc,metrics-*.otel-imc,k8s-imc-*/_count" -d '{"query":{"bool":{"should":[{"term":{"attributes.case":"spoof"}},{"term":{"app.case":"spoof"}}]}}}' | jq .count) ))" 0
check "spoof：stdout 自帶的 service、kubernetes、labels 收進 app，不蓋過身分" \
  "$(es "${ES_URL}/k8s-*/_search" -d '{"size":1,"query":{"bool":{"filter":[{"term":{"app.case":"spoof"}},{"term":{"app.src":"stdout"}}]}}}' | jq -r '.hits.hits[0]._source | "\(.app.labels.dept)|\(.app.service.name)|\(.labels.dept)|\(.service.name)"')" \
  "D1110|fake-svc|D3120|keys-d"

echo "== 7. namespace 沒有 dept → unassigned；dept 不在 groups.yaml → unassigned，dept 照實記"
check_all "nodept"  nodept  "${since}" "keys-e|keys-nodept|$(pod_of keys-nodept nodept)||unassigned|${CLUSTER}|unassigned"
check_all "baddept" baddept "${since}" "keys-f|keys-baddept|$(pod_of keys-baddept baddept)|D9999|unassigned|${CLUSTER}|unassigned"

echo "== 8. 同名服務在兩個 namespace：靠 namespace 分得開，各進各的部門"
check_all "samename" samename "${since}" "keys-a|keys-imc|$(pod_of keys-imc samename)|D1110|imc|${CLUSTER}|imc"
check "samename：keys-a 在 IT 的資料都是 keys-it 的" \
  "$(es "${ES_URL}/traces-*.otel-it,metrics-*.otel-it,k8s-it-*/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"${R}.service.name\":\"keys-a\"}},{\"range\":{\"@timestamp\":{\"gte\":\"${since}\"}}}],\"must_not\":{\"term\":{\"${R}.k8s.namespace.name\":\"keys-it\"}}}}}" | jq .count)" 0

echo "== 9. hostNetwork：stdout log、Prometheus 認得出 Pod；OTLP 推的（連線來源是節點 IP）對不到 → unassigned（已知限制）"
# hostnet、hostnet2 在同一個節點、不同 namespace：兩個 Pod 的 IP 都是節點 IP，看 Prometheus 會不會對錯 Pod
check_all "hostnet"  hostnet  "${since}" "keys-i|keys-it|$(it_pod hostnet)|D3120|it|${CLUSTER}|it" "log_stdout met_prom"
check_all "hostnet2" hostnet2 "${since}" "keys-i2|keys-imc|$(pod_of keys-imc hostnet2)|D1110|imc|${CLUSTER}|imc" "log_stdout met_prom"
check_all "hostnet"  hostnet  "${since}" "keys-i||||unassigned|${CLUSTER}|unassigned" "trace log_otlp met_otlp"
check_all "hostnet2" hostnet2 "${since}" "keys-i2||||unassigned|${CLUSTER}|unassigned" "trace log_otlp met_otlp"

echo "== 10. 改 namespace 的 dept（keys-switch：D3120 → D1110）：多久生效、五種是否同時切"
ts=$(date +%s); kubectl label namespace keys-switch dept=D1110 --overwrite >/dev/null
tsw="$(iso "${ts}")"; echo "   ${tsw} 改 dept"
sleep 90; refresh
first_new() {  # 改了之後第一筆進 IMC 的時間（秒，相對於改的時間）
  local t; t="$(es "${ES_URL}/$(idx_of "$1" imc)/_search" -d "{\"size\":1,\"sort\":[{\"@timestamp\":\"asc\"}],\"_source\":false,\"query\":{\"bool\":{\"filter\":[$(filter_of "$1" switch),{\"range\":{\"@timestamp\":{\"gte\":\"${tsw}\"}}}]}}}" | jq -r '.hits.hits[0].sort[0] // empty')"   # 排序值是毫秒
  [[ -n "${t}" ]] && echo $(( t / 1000 - ts )) || echo none
}
for s in ${SRCS}; do
  d="$(first_new "${s}")"
  echo "   ${s}：改後 ${d} 秒第一筆進 IMC"
  check "switch：${s} 90 秒內切到 IMC" "$([[ "${d}" != none && "${d}" -le 90 ]] && echo yes || echo no)" yes
  if [[ "${d}" != none ]]; then
    check "switch：${s} 切過去之後沒有再進 IT" \
      "$(es "${ES_URL}/$(idx_of "${s}" it)/_count" -d "{\"query\":{\"bool\":{\"filter\":[$(filter_of "${s}" switch),{\"range\":{\"@timestamp\":{\"gt\":\"$(iso $((ts + d + 1)))\"}}}]}}}" | jq .count)" 0
  fi
done
check_all "switch（改後）" switch "$(iso $((ts + 60)))" "keys-g|keys-switch|$(pod_of keys-switch switch)|D1110|imc|${CLUSTER}|imc"

echo "== 11. 容器重啟（同一個 Pod）：pod 欄位不變"
p_before="$(it_pod base)"
kubectl -n keys-it exec "${p_before}" -- kill 1
until [[ "$(kubectl -n keys-it get pod "${p_before}" -o jsonpath='{.status.containerStatuses[0].restartCount}')" -ge 1 ]]; do sleep 2; done
kubectl -n keys-it wait --for=condition=Ready "pod/${p_before}" --timeout=60s >/dev/null
tr1=$(date +%s); echo "   $(iso "${tr1}") 重啟完，等 70 秒"
sleep 70; refresh
check_all "restart" base "$(iso "${tr1}")" "keys-a|keys-it|${p_before}|D3120|it|${CLUSTER}|it"

echo "== 12. 服務改名（label keys-a → keys-a2，換新 Pod）：五種都換成新名字、新 Pod，舊 Pod 名字不再出現"
t_patch=$(date +%s)
kubectl -n keys-it patch deployment base --type=json -p '[{"op":"replace","path":"/spec/template/metadata/labels/app.kubernetes.io~1name","value":"keys-a2"}]' >/dev/null
kubectl -n keys-it rollout status deployment/base --timeout=120s >/dev/null
until [[ -z "$(kubectl -n keys-it get pod "${p_before}" --ignore-not-found -o name)" ]]; do sleep 1; done
tr2=$(date +%s); p_after="$(it_pod base)"; echo "   $(iso "${tr2}") 新 Pod ${p_after}，等 70 秒"
sleep 70; refresh
check_all "rename" base "$(iso "${tr2}")" "keys-a2|keys-it|${p_after}|D3120|it|${CLUSTER}|it"
for s in ${SRCS}; do
  check "rename：${s} 舊 Pod 刪掉之後沒有資料再掛舊 Pod 名字" \
    "$(count_since "${s}" base "$(iso "${tr2}")" "{\"term\":{\"${R}.k8s.pod.name\":\"${p_before}\"}}")" 0
done

echo "== 13. 有 dept 的情境落到 unassigned 的：只有新 Pod 剛起來時的 OTLP（Pod IP 還沒進 Collector 的清單）"
# 新 Pod 起來的時段：步驟 2 換 Pod、步驟 12 改名換 Pod。其他時段（含步驟 1 Collector 重啟）一筆都不該有
for s in ${SRCS}; do
  # filter_of 的「case = 一個情境」換成「case 是這幾個之一」
  f="$(filter_of "${s}" @ | sed 's/{"term":{"\([a-z.]*case\)":"@"}}/{"terms":{"\1":["base","nolabel","conflict","spoof","samename"]}}/')"
  leaked="$(es "${ES_URL}/$(idx_of "${s}" unassigned)/_search" -d "{\"size\":100,\"_source\":false,\"sort\":[{\"@timestamp\":\"asc\"}],\"query\":{\"bool\":{\"filter\":[${f},{\"range\":{\"@timestamp\":{\"gte\":\"$(iso "${t_start}")\"}}}]}}}" | jq -r '.hits.hits[].sort[0] / 1000 | floor')"
  n=0; outside=0
  for t in ${leaked}; do
    n=$((n + 1))
    (( (t >= t_apps && t <= t0) || (t >= t_patch && t <= tr2) )) || outside=$((outside + 1))
  done
  echo "   ${s}：落到 unassigned ${n} 筆"
  case ${s} in
    log_otlp|met_otlp|trace) check "unassigned：${s} 只在新 Pod 剛起來時" "${outside}" 0;;
    *)                       check "unassigned：${s} 一筆都沒有" "${n}" 0;;
  esac
done

if [[ -z "${KEEP:-}" ]]; then
  for ns in ${NSS}; do kubectl delete namespace "${ns}" --wait=false >/dev/null; done
fi
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
