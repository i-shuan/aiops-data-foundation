#!/usr/bin/env bash
# Pod 同儕橫向比對（aiops-engine/code/configs/baseline/peer.yaml）的邊緣情境，在真的 cluster 上用真的 .NET 應用（dotnet-app/）確認。
# 其他情境用合成資料測（aiops-engine/code/tests/peer_edge_e2e.py）；這裡只測合成資料測不準的三個：
#
#   A. 3 個副本都正常：都不報
#   B. Pod 被換掉（名字變）＋新 Pod 暖機：一個 Pod 連線池漏光 → 抓到 → 刪掉它 → 之後 10 分鐘每分鐘跑一次：
#      舊名字最多在 fresh（2 分鐘）內還報、新 Pod 一次都不報。同一份資料也用「沒有 warmup」的設定跑，看暖機篩選有沒有作用
#   C. 只剩 2 個 Pod、其中一個壞：peer 不判斷（min_peers 3）
#
# 不動 Collector、不跑 elasticsearch-setup.sh；只建 namespace peer-edge（dept=D1110），跑完刪掉。
# 前提：cluster 上 ES（es-test）、Collector 在跑，ES 連得到。整段拿 cluster 鎖。約 40 分鐘。
# 用法：ES_URL=http://127.0.0.1:31513 e2e/k8s/run-peer-edge-e2e.sh      （KEEP=1：跑完不刪）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_AUTH="${ES_AUTH:-elastic:changeme-test-only}"
ns=peer-edge
fail=0
kubectl() { command kubectl --context "${KUBE_CONTEXT:-rancher-desktop}" "$@"; }
ts() { date +%H:%M:%S; }
pass() { echo "  PASS  $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
es() { curl -sS -m 30 -u "${ES_AUTH}" -H 'Content-Type: application/json' "$@"; }
pods() {  # 現在在跑、沒有正在刪的 Pod，依名稱排序
  kubectl -n "${ns}" get pods -l app.kubernetes.io/name=dotnet-app -o json \
    | jq -r '.items[] | select(.metadata.deletionTimestamp == null and .status.phase == "Running") | .metadata.name' | sort
}
tmp="$(mktemp -d)"
cleanup() {  # 中途失敗也要刪掉測試應用
  [[ -z "${KEEP:-}" ]] && kubectl delete namespace "${ns}" --ignore-not-found --wait=false >/dev/null 2>&1
  rm -rf "${tmp}"
}
trap cleanup EXIT
# 沒有 warmup、max_peer_median 的設定（改之前的行為），跟現在的設定跑同一份資料比較
sed '/^warmup:/d; /max_peer_median:/d' "${repo}/aiops-engine/code/configs/baseline/peer.yaml" > "${tmp}/peer-nowarmup.yaml"
# peer <設定檔>：跑全部檢查，印出觸發的（只看這個 namespace）
peer() {
  (cd "${repo}/aiops-engine/code" && ES_URL="${ES_URL}" ES_AUTH="${ES_AUTH}" python3 -m detector peer --config "$1") \
    | grep -E '^  \[' | grep -F "${ns}、" || true
}
cpu_of() {  # cpu_of <Pod>：最近 10 分鐘 k8s.pod.cpu.usage 平均（核）、第一筆資料時間
  es "${ES_URL}/metrics-kubeletstatsreceiver.otel-*/_search?size=0" -d "{\"query\":{\"bool\":{\"filter\":[
    {\"term\":{\"resource.attributes.k8s.pod.name\":\"$1\"}},{\"exists\":{\"field\":\"metrics.k8s.pod.cpu.usage\"}},
    {\"range\":{\"@timestamp\":{\"gte\":\"now-10m\"}}}]}},\"aggs\":{\"avg\":{\"avg\":{\"field\":\"metrics.k8s.pod.cpu.usage\"}},
    \"max\":{\"max\":{\"field\":\"metrics.k8s.pod.cpu.usage\"}},\"first\":{\"min\":{\"field\":\"@timestamp\"}}}}" \
    | jq -r '.aggregations | "avg \(.avg.value // 0 | .*100|round/100) 核、max \(.max.value // 0 | .*100|round/100) 核、第一筆 \(.first.value_as_string // "無")"'
}

echo "== 0. 前提：ES、Collector"
es "${ES_URL}/_cluster/health" | jq -r '"   ES \(.status)"'
kubectl -n otel-collector get pods --no-headers | awk '{print "   " $1, $3}'

echo "== 1. .NET 應用 3 個副本（namespace ${ns}，第一次要編譯幾分鐘）"
kubectl delete namespace "${ns}" --ignore-not-found --wait=true >/dev/null
kubectl create namespace "${ns}" >/dev/null
kubectl label namespace "${ns}" dept=D1110 >/dev/null
kubectl -n "${ns}" create configmap dotnet-app-src --from-file="${here}/dotnet-app/app.csproj" --from-file="${here}/dotnet-app/Program.cs" >/dev/null
tr -d '\r' < "${here}/dotnet-app.yaml" | sed "s/namespace: metrics-cases/namespace: ${ns}/" | kubectl apply -f - >/dev/null
# 共用的單節點 cluster 記憶體排程不下：編譯的 initContainer 只設 limit 2Gi（request 就等於 2Gi）、db 256Mi，
# 這裡把 request 設小（limit 不變），一次只加一個副本（同時編譯 3 份很吃記憶體），忙的機器上 progressDeadline 放寬
kubectl -n "${ns}" patch deploy/dotnet-app --type=json -p '[
  {"op":"add","path":"/spec/progressDeadlineSeconds","value":1800},
  {"op":"add","path":"/spec/template/spec/initContainers/0/resources/requests","value":{"memory":"512Mi"}},
  {"op":"add","path":"/spec/template/spec/containers/1/resources/requests","value":{"memory":"64Mi"}}]' >/dev/null
for r in 1 2 3; do
  kubectl -n "${ns}" scale deploy/dotnet-app --replicas=${r} >/dev/null
  if ! kubectl -n "${ns}" rollout status deploy/dotnet-app --timeout=1800s >/dev/null; then
    kubectl -n "${ns}" get pods -o wide; echo "應用起不來，停止"; exit 2
  fi
done
echo "   $(ts) 起來了：$(pods | tr '\n' ' ')；跑 6 分鐘累積資料"
sleep 360
n=$(es "${ES_URL}/metrics-*.otel-*/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"resource.attributes.k8s.namespace.name\":\"${ns}\"}},
  {\"exists\":{\"field\":\"metrics.http.server.request.duration\"}},{\"range\":{\"@timestamp\":{\"gte\":\"now-2m\"}}}]}}}" | jq -r .count)
k=$(es "${ES_URL}/metrics-kubeletstatsreceiver.otel-*/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"resource.attributes.k8s.namespace.name\":\"${ns}\"}},
  {\"range\":{\"@timestamp\":{\"gte\":\"now-2m\"}}}]}}}" | jq -r .count)
echo "   最近 2 分鐘：HTTP 指標 ${n} 筆、kubeletstats ${k} 筆"
if [[ "${n}" == 0 || "${k}" == 0 ]]; then echo "資料沒有進來，停止"; exit 2; fi

echo "== 2. A. 3 個都正常"
out="$(peer configs/baseline/peer.yaml)"
if [[ -z "${out}" ]]; then pass "3 個 Pod 都不報"; else bad "正常時就報了：${out}"; fi

echo "== 3. B. 一個 Pod 連線池漏光 → 抓到 → 換掉"
old="$(pods | sed -n 2p)"
kubectl -n "${ns}" exec "${old}" -c app -- sh -c "touch /ctl/leakconn"
sleep 240
out="$(peer configs/baseline/peer.yaml)"
if grep -qF "${old}" <<<"${out}"; then pass "換掉前抓到 ${old}"; echo "        $(grep -F "${old}" <<<"${out}" | head -1 | sed 's/^ *//')"
else bad "換掉前沒抓到 ${old}：${out:-無}"; fi
others="$(grep -vF "${old}" <<<"${out}" || true)"
if [[ -z "${others}" ]]; then pass "另外兩個不報"; else bad "另外兩個也報了：${others}"; fi
before="$(pods)"
kubectl -n "${ns}" delete pod "${old}" --wait=false >/dev/null
deleted=$(date +%s)
echo "   $(ts) 刪掉 ${old}"
new=""
old_last=-1; new_hits=0; new_hits_nowarm=0
for m in 1 2 3 4 5 6 7 8 9 10 11 12; do
  s=$(( deleted + m * 60 - $(date +%s) )); if (( s > 0 )); then sleep "${s}"; fi
  [[ -z "${new}" ]] && new="$(comm -13 <(echo "${before}") <(pods) | head -1)"
  out="$(peer configs/baseline/peer.yaml)"; out0="$(peer "${tmp}/peer-nowarmup.yaml")"
  o=$(grep -cF "${old}" <<<"${out}" || true)
  w=0; w0=0
  if [[ -n "${new}" ]]; then w=$(grep -cF "${new}" <<<"${out}" || true); w0=$(grep -cF "${new}" <<<"${out0}" || true); fi
  (( o > 0 )) && old_last=${m}
  new_hits=$(( new_hits + w )); new_hits_nowarm=$(( new_hits_nowarm + w0 ))
  echo "   $(ts) 刪掉後第 ${m} 分鐘：舊 ${old} 報 ${o} 項；新 ${new:-（還沒 Running）} 報 ${w} 項（沒有 warmup 的設定：${w0} 項）$( [[ -n "${new}" ]] && echo "；新 Pod CPU $(cpu_of "${new}")")"
  grep -F "${new:-^$}" <<<"${out0}" | sed 's/^ */        沒有 warmup：/' || true
done
# 舊 Pod 收到 SIGTERM 後最多 30 秒停掉，加上 fresh 2 分鐘：第 3 分鐘以後不該再報
if (( old_last <= 3 )); then pass "舊名字 ${old} 第 ${old_last} 分鐘後不再報（-1＝刪掉後一次都沒報）"; else bad "舊名字 ${old} 到第 ${old_last} 分鐘還在報"; fi
if (( new_hits == 0 )); then pass "新 Pod ${new} 12 分鐘內一次都不報"; else bad "新 Pod ${new} 被報 ${new_hits} 次"; fi
echo "        （參考）沒有 warmup 的設定：新 Pod 被報 ${new_hits_nowarm} 次"

echo "== 4. C. 只剩 2 個 Pod、其中一個壞"
kubectl -n "${ns}" scale deploy/dotnet-app --replicas=2 >/dev/null
sleep 30
two="$(pods)"
echo "   $(ts) 剩 $(tr '\n' ' ' <<<"${two}")；等 3 分鐘讓縮掉的 Pod 超過 fresh"
sleep 180
b2="$(sed -n 1p <<<"${two}")"
kubectl -n "${ns}" exec "${b2}" -c app -- sh -c "touch /ctl/leakconn"
sleep 240
out="$(peer configs/baseline/peer.yaml)"
if [[ -z "${out}" ]]; then pass "2 個 Pod（${b2} 壞）：peer 不判斷"; else bad "2 個 Pod 卻報了：${out}"; fi
(cd "${repo}/aiops-engine/code" && ES_URL="${ES_URL}" ES_AUTH="${ES_AUTH}" python3 -m detector peer --config configs/baseline/peer.yaml) \
  | grep -E 'peer\.http_error_rate：' | sed 's/^ */        （參考）/'
svc="$(es "${ES_URL}/metrics-*.otel-*/_search?size=0" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"resource.attributes.k8s.namespace.name\":\"${ns}\"}},
  {\"exists\":{\"field\":\"metrics.http.server.request.duration\"}},{\"range\":{\"@timestamp\":{\"gte\":\"now-3m\"}}}]}},
  \"aggs\":{\"all\":{\"value_count\":{\"field\":\"metrics.http.server.request.duration\"}},
  \"err\":{\"filter\":{\"range\":{\"attributes.http.response.status_code\":{\"gte\":500}}},\"aggs\":{\"n\":{\"value_count\":{\"field\":\"metrics.http.server.request.duration\"}}}}}}" \
  | jq -r '.aggregations | "\(.err.n.value) / \(.all.value)"')"
echo "        （參考）整個服務最近 3 分鐘 5xx／全部：${svc}（規則 http.error_rate_floor 看這個）"

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
