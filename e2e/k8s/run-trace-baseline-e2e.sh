#!/usr/bin/env bash
# trace 每一段的 baseline 端到端驗證（detector trace-*；案例見 docs/rca/README.md）。
#
# 兩個模擬服務（rca-apps.yaml）走真正的路線（Collector → ES）：
#   1. 正常跑 20 分鐘，學出「平常」（測試用 1 分鐘一點；正式是 5 分鐘、4 週）
#   2. 再正常跑 3 分鐘，偵測：不能有誤報
#   3. 依序製造 4 種事故，各 3 分鐘，每次都要在對的那一段、用對的數字抓到：
#        N+1（wip-worker 每個請求查 100 次）→ wip-worker 那段的「次數」
#        重試風暴（mes-api 呼叫 wip-worker 4 次）→ mes-api 呼叫 wip-worker 的「次數」
#        快取穿透（mes-api 每個請求都查 DB，平常 5%）→ mes-api 查 DB 的「次數」
#        簡訊廠商掛了（20ms → 3 秒）→ mes-api 呼叫簡訊廠商的「時間」
#      另外兩條不用學的規則：N+1 同時要報「同一個呼叫太多次」（repeated）；
#        吞掉的例外（wip-worker 查 DB 逾時、catch 掉回 200）→ 那一段的 swallowed（規則）和 swallowed_rate（baseline）
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector、測試 namespace 都在）。約 40 分鐘。
# 用法：ES_URL=http://<ip>:9200 e2e/k8s/run-trace-baseline-e2e.sh      （KEEP=1：跑完不刪模擬服務）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
api() { kubectl -n it-mes-prod exec deploy/rca-mes-api -- sh -c "$1"; }
wip() { kubectl -n imc-wip-prod exec deploy/rca-wip-worker -- sh -c "$1"; }
cfg=tests/trace_k3s.yaml
aiops() { (cd "${repo}/aiops-engine/code" && ES_URL="${ES_URL}" ES_API_KEY="${BASELINE_KEY}" python3 -m detector "$@"); }
# anomalies <開始秒> <結束秒>：這段時間 rca 兩個服務的異常，一行一個「數字|服務|段」
anomalies() {
  es -X POST "${ES_URL}/aiops-anomalies/_refresh" >/dev/null
  es "${ES_URL}/aiops-anomalies/_search" -d "{\"size\":1000,\"query\":{\"bool\":{\"filter\":[
    {\"prefix\":{\"series\":\"trace.segment.\"}},{\"range\":{\"@timestamp\":{\"gte\":\"$(iso "$1")\",\"lt\":\"$(iso "$2")\"}}},
    {\"terms\":{\"keys.entry.service\":[\"rca-mes-api\",\"rca-wip-worker\"]}}]}}}" \
    | jq -r '.hits.hits[]._source | "\(.series | sub("trace.segment.";""))|\(.keys["entry.service"])|\(.keys.segment)"' | sort -u
}

echo "== 1. API key（baseline 的 key：讀 trace、寫 aiops-*）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/elasticsearch/code/scripts/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY="$(key_of '寫入 key：es-writer')" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
BASELINE_KEY="$(key_of 'AIOps baseline key')"

echo "== 2. 模擬服務"
"${here}/deploy-collector.sh"
kubectl -n otel-collector rollout restart daemonset/otel-node-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
for ns in it-mes-prod imc-wip-prod; do
  kubectl -n "${ns}" create configmap rca-lib --from-file=lib.sh="${here}/rca-lib.sh" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
kubectl apply -f "${here}/rca-apps.yaml" >/dev/null
kubectl -n imc-wip-prod rollout restart deployment/rca-wip-worker >/dev/null
kubectl -n it-mes-prod rollout restart deployment/rca-mes-api >/dev/null
kubectl -n imc-wip-prod rollout status deployment/rca-wip-worker --timeout=180s >/dev/null
kubectl -n it-mes-prod rollout status deployment/rca-mes-api --timeout=180s >/dev/null
# 舊的統計、baseline、異常會混進來，先清掉 rca 兩個服務的
for ix in aiops-trace-segments aiops-baseline aiops-anomalies; do
  es -X POST "${ES_URL}/${ix}/_delete_by_query?refresh=true&ignore_unavailable=true" \
    -d '{"query":{"bool":{"should":[{"terms":{"entry.service":["rca-mes-api","rca-wip-worker"]}},
        {"terms":{"keys.entry.service":["rca-mes-api","rca-wip-worker"]}}]}}}' >/dev/null
done
t0=$(( ($(date +%s) / 60 + 1) * 60 ))

echo "== 3. 正常跑 20 分鐘，學出平常"
sleep $(( t0 + 1200 - $(date +%s) + 20 ))
aiops trace-collect --config "${cfg}" --start "$(iso "${t0}")" --end "$(iso $((t0 + 1200)))"
aiops trace-train --config "${cfg}" --history 20min --end "$(iso $((t0 + 1200)))"
n=$(es "${ES_URL}/aiops-baseline/_count" -d '{"query":{"bool":{"filter":[{"term":{"series":"trace.segment.calls"}},{"term":{"keys.entry.service":"rca-mes-api"}}]}}}' | jq .count)
[[ ${n} -gt 0 ]] && echo "  PASS  學出 rca-mes-api 的 baseline（${n} 個時段）" || { echo "  FAIL  沒學出 rca-mes-api 的 baseline"; fail=1; }

# run_case <名稱> <開> <關> <預期的 數字|服務|段>
run_case() {
  local a b
  a=$(( ($(date +%s) / 60 + 1) * 60 ))
  sleep $(( a - $(date +%s) ))
  eval "$2"
  b=$(( a + 180 ))
  sleep $(( b - $(date +%s) + 15 ))
  eval "$3"
  aiops trace-detect --config "${cfg}" --window 3min --at "$(iso "${b}")" >/dev/null
  got="$(anomalies "${a}" "${b}")"
  if [[ -z "$4" ]]; then
    check "$1：沒有誤報" "${got}" ""
  else
    local want
    while IFS= read -r want; do   # 預期的可以有好幾個，用 ; 分開
      if grep -qxF -- "${want}" <<<"${got}"; then echo "  PASS  $1：抓到 ${want}"; else echo "  FAIL  $1：沒抓到 ${want}；抓到的是 [${got}]"; fail=1; fi
    done < <(tr ';' '\n' <<<"$4")
  fi
  sleep 60   # 事故結束後空 1 分鐘，下一個才開始
}

echo "== 4. 正常 3 分鐘：不能有誤報"
run_case "正常" ":" ":" ""
echo "== 5. 事故"
run_case "N+1"         "wip 'touch /tmp/n1'"       "wip 'rm -f /tmp/n1'"       "calls|rca-wip-worker|rca-wip-worker › SELECT LOT_HISTORY → oracle;repeated|rca-wip-worker|rca-wip-worker › SELECT LOT_HISTORY → oracle"
run_case "重試風暴"     "api 'touch /tmp/retry'"    "api 'rm -f /tmp/retry'"    "calls|rca-mes-api|rca-mes-api › GET → rca-wip-worker"
run_case "快取穿透"     "api 'touch /tmp/cold'"     "api 'rm -f /tmp/cold'"     "calls|rca-mes-api|rca-mes-api › SELECT LOT → oracle"
run_case "簡訊廠商掛了" "api 'touch /tmp/sms_hang'" "api 'rm -f /tmp/sms_hang'" "time_ms|rca-mes-api|rca-mes-api › POST → sms-gateway"
run_case "吞掉的例外"   "wip 'touch /tmp/swallow'"  "wip 'rm -f /tmp/swallow'"  "swallowed|rca-wip-worker|rca-wip-worker › SELECT LOT_HISTORY → oracle;swallowed_rate|rca-wip-worker|rca-wip-worker › SELECT LOT_HISTORY → oracle"

if [[ -z "${KEEP:-}" ]]; then
  kubectl delete -f "${here}/rca-apps.yaml" --wait=false >/dev/null
  for ns in it-mes-prod imc-wip-prod; do kubectl -n "${ns}" delete configmap rca-lib --wait=false >/dev/null; done
fi
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
