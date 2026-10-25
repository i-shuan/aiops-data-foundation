#!/usr/bin/env bash
# 跟兄弟比（engine/configs/baseline/peer.yaml）端到端驗證：真的 .NET 應用（dotnet-app/，自動埋點）開 3 個副本，每次只讓其中一個壞掉，
# 看能不能指出是哪一個，而且其他兩個不報。
#
#   正常時：3 個 Pod 都不報
#   錯誤：一個 Pod 的連線池漏光（拿了不還）→ 那個 Pod 的請求等 2 秒後 500 → peer.http_error_rate、peer.http_latency_p95_s
#         同時看服務整體的錯誤率：被另外兩個正常的 Pod 稀釋
#   記憶體：一個 Pod 每個請求 32KB 放進不會清的 list → peer.pod_memory_bytes
#   CPU：一個 Pod 裡有程式卡在無窮迴圈（load 容器裡跑 busy loop）→ peer.pod_cpu
#
# 前提：run-e2e.sh、run-metrics-e2e.sh 跑過。約 40 分鐘。
# 用法：ES_URL=http://<ip>:9200 e2e/local-k3s/run-peer-e2e.sh      （KEEP=1：跑完不刪；ONLY=errors,memory,cpu 只跑某幾個案例）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
fail=0
ns=metrics-cases
ts() { date +%H:%M:%S; }
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
want() { [[ -z "${ONLY:-}" || ",${ONLY}," == *",$1,"* ]]; }
pods() {  # 現在在跑、沒有正在刪的 Pod，依名稱排序
  kubectl -n "${ns}" get pods -l app.kubernetes.io/name=dotnet-app -o json \
    | jq -r '.items[] | select(.metadata.deletionTimestamp == null and .status.phase == "Running") | .metadata.name' | sort
}
in_pod() { kubectl -n "${ns}" exec "$1" -c "$2" -- sh -c "$3"; }
# peer <檢查,…>：跑這幾個，印出觸發的（只看 dotnet-app）
peer() {
  (cd "${repo}/engine" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" python3 -m detector peer \
    --config configs/baseline/peer.yaml --only "$1") | grep -E '^  \[' | grep dotnet-app || true
}
expect() {  # expect <說明> <輸出> <檢查標題> <Pod>
  if grep -F -- "$3" <<<"$2" | grep -qF -- "$4"; then echo "  PASS  $1"; echo "        $(grep -F -- "$3" <<<"$2" | grep -F -- "$4" | head -1 | sed 's/^ *//')"
  else echo "  FAIL  $1：沒有「$3 … $4」"; fail=1; fi
}
none() {  # none <說明> <輸出>：輸出裡除了 <壞的 Pod> 以外不能有別的 Pod
  local others; others="$(grep -vF -- "$3" <<<"$2" || true)"
  if [[ -z "${others}" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：${others}"; fail=1; fi
}
restart_pod() {  # 刪掉壞的 Pod，等補上的新 Pod 起來、累積一點資料
  kubectl -n "${ns}" delete pod "$1" --wait=true >/dev/null
  kubectl -n "${ns}" rollout status deploy/dotnet-app --timeout=900s >/dev/null
  sleep 180
}
ALL=peer.http_error_rate,peer.http_latency_p95_s,peer.pod_cpu,peer.pod_memory_bytes

echo "== 1. API key、Collector"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/telemetry/scripts/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY="$(key_of '寫入 key：es-writer')" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
BASELINE_KEY="$(key_of 'AIOps baseline key')"
kubectl apply -k "${here}/otel-collector" >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null

echo "== 2. .NET 應用 3 個副本（namespace ${ns}，第一次要編譯幾分鐘）"
kubectl delete namespace "${ns}" --ignore-not-found --wait=true >/dev/null
kubectl create namespace "${ns}" >/dev/null
kubectl label namespace "${ns}" dept=D1110 >/dev/null
kubectl -n "${ns}" create configmap dotnet-app-src --from-file="${here}/dotnet-app/app.csproj" --from-file="${here}/dotnet-app/Program.cs" >/dev/null
[[ -f /root/.ccr/ca-bundle.crt ]] && kubectl -n "${ns}" create configmap proxy-ca --from-file=ca-bundle.crt=/root/.ccr/ca-bundle.crt >/dev/null
kubectl apply -f "${here}/dotnet-app.yaml" >/dev/null
kubectl -n "${ns}" scale deploy/dotnet-app --replicas=3 >/dev/null
kubectl -n "${ns}" rollout status deploy/dotnet-app --timeout=1200s >/dev/null
echo "   $(ts) 起來了：$(pods | tr '\n' ' ')；跑 6 分鐘累積資料"
sleep 360

echo "== 3. 正常的時候：3 個 Pod 都不報"
out="$(peer "${ALL}")"
if [[ -z "${out}" ]]; then echo "  PASS  沒有觸發"; else echo "  FAIL  正常時就報了：${out}"; fail=1; fi

if want errors; then
bad="$(pods | sed -n 2p)"
echo "== 4. 案例：只有 ${bad} 的連線池漏光（請求等 2 秒後 500），4 分鐘"
in_pod "${bad}" app "touch /ctl/leakconn"
sleep 240
out="$(peer peer.http_error_rate,peer.http_latency_p95_s)"
expect "錯誤率比兄弟高：${bad}" "${out}" "錯誤率比同服務其他 Pod 高" "${bad}"
expect "比兄弟慢：${bad}"       "${out}" "比同服務其他 Pod 慢" "${bad}"
none "另外兩個 Pod 不報" "${out}" "${bad}"
# 同一段時間、整個服務的錯誤率（跟平常比的 baseline 看的是這個）：被正常的 Pod 稀釋
svc="$(es "${ES_URL}/metrics-*.otel-*/_search?size=0" -d '{"query":{"bool":{"filter":[{"term":{"resource.attributes.service.name":"dotnet-app"}},
  {"exists":{"field":"metrics.http.server.request.duration"}},{"range":{"@timestamp":{"gte":"now-3m"}}}]}},
  "aggs":{"all":{"value_count":{"field":"metrics.http.server.request.duration"}},
  "err":{"filter":{"range":{"attributes.http.response.status_code":{"gte":500}}},"aggs":{"n":{"value_count":{"field":"metrics.http.server.request.duration"}}}}}}' \
  | jq -r '.aggregations | "\(.err.n.value) / \(.all.value)"')"
echo "        （參考）整個服務最近 3 分鐘的錯誤率：${svc} = $(python3 -c "a,b='${svc}'.split(' / ');print(f'{float(a)/float(b):.0%}')")"
restart_pod "${bad}"
fi

if want memory; then
bad="$(pods | sed -n 3p)"
echo "== 5. 案例：只有 ${bad} 記憶體洩漏（每個請求 32KB），最多 8 分鐘"
in_pod "${bad}" app "echo 32 > /ctl/leak"
start=$(date +%s); seen=""
while (( $(date +%s) - start < 480 )); do
  sleep 60
  out="$(peer peer.pod_memory_bytes)"
  m=$(( ($(date +%s) - start) / 60 ))
  if grep -qF "${bad}" <<<"${out}"; then seen="${m}"; break; fi
  echo "   $(ts) 第 ${m} 分鐘：還沒"
done
expect "記憶體比兄弟高很多：${bad}（第 ${seen:-?} 分鐘）" "${out}" "記憶體比同 Deployment 其他 Pod 高很多" "${bad}"
none "另外兩個 Pod 不報" "${out}" "${bad}"
restart_pod "${bad}"
fi

if want cpu; then
bad="$(pods | sed -n 1p)"
echo "== 6. 案例：${bad} 裡有程式卡在無窮迴圈（佔滿 1 核），最多 8 分鐘"
kubectl -n "${ns}" exec "${bad}" -c load -- sh -c 'timeout 600 sh -c "while :; do :; done" >/dev/null 2>&1 &' || true
start=$(date +%s); seen=""
while (( $(date +%s) - start < 480 )); do
  sleep 60
  out="$(peer peer.pod_cpu)"
  m=$(( ($(date +%s) - start) / 60 ))
  if grep -qF "${bad}" <<<"${out}"; then seen="${m}"; break; fi
  echo "   $(ts) 第 ${m} 分鐘：還沒"
done
expect "CPU 比兄弟高很多：${bad}（第 ${seen:-?} 分鐘）" "${out}" "CPU 比同 Deployment 其他 Pod 高很多" "${bad}"
none "另外兩個 Pod 不報" "${out}" "${bad}"
fi

[[ -z "${KEEP:-}" ]] && kubectl delete namespace "${ns}" --wait=false >/dev/null
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
