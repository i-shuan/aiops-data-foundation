#!/usr/bin/env bash
# metrics 異常 → 用「服務＋路由＋時間窗」撈 trace：各種邊緣情況撈不撈得到對的 trace。
# 模擬服務 mt-edge（metrics-trace-apps.yaml）7 條路由各測一種情況，檢查在 metrics_trace_check.py（每項印 PASS／FAIL 和數字）。
#
#   1. 部署 mt-edge，正常跑到下一個整 5 分鐘
#   2. 事故 11 分鐘：/edge/partial 10% 慢 2 秒、/edge/sub1s 200ms → 800ms（中間一定有一個完整的 5 分鐘）
#   3. 事故結束 2 分鐘後：容器重啟（同一個 Pod）；再 3 分鐘：Pod 刪掉重建；再 3 分鐘
#   4. 等 30 秒讓資料進 ES，跑檢查
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector、測試 namespace、rca-lib 用的 Collector 都在），ES 連得到（kubectl port-forward）。
# 部署、重啟、刪除前拿 cluster 鎖（cluster-lock.sh），等待時放掉。約 25 分鐘。
# 用法：ES_URL=http://127.0.0.1:31200 e2e/k8s/run-metrics-trace-e2e.sh      （KEEP=1：跑完不刪 mt-edge）
# 只重跑檢查（資料還在 ES）：CHECK_ONLY="--normal … --end …" 同一支腳本（參數照上次印出來的）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
PY="${PYTHON:-python3}"
check() { ES_URL="${ES_URL}" ES_AUTH=elastic:changeme-test-only "${PY}" -X utf8 "${here}/metrics_trace_check.py" "$@"; }
if [[ -n "${CHECK_ONLY:-}" ]]; then
  # shellcheck disable=SC2086
  check ${CHECK_ONLY}; exit
fi
source "${here}/cluster-lock.sh"
# HOLD_LOCK=1：整段都拿著鎖（別人不會在中途換 Collector、加壓）；預設只在部署、重啟、刪除時拿
if [[ -n "${HOLD_LOCK:-}" ]]; then cluster_lock "metrics-trace（整段）"; lk() { :; }; ulk() { :; }; else lk() { cluster_lock "metrics-trace"; }; ulk() { cluster_unlock; }; fi
app() { MSYS_NO_PATHCONV=1 kubectl -n it-mes-prod exec deploy/mt-edge -- sh -c "$1"; }   # Git Bash 會把 /tmp 改寫成 Windows 路徑
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
wait_until() { local t; t=$(date -d "$1" +%s); while (( $(date +%s) < t )); do sleep 5; done; }

echo "== 1. 部署 mt-edge"
lk
tmp="$(mktemp -d)"
tr -d '\r' < "${here}/rca-lib.sh" > "${tmp}/rca-lib.sh"
kubectl -n it-mes-prod create configmap rca-lib --from-file=lib.sh="${tmp}/rca-lib.sh" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
tr -d '\r' < "${here}/metrics-trace-apps.yaml" | kubectl apply -f - >/dev/null
kubectl -n it-mes-prod rollout restart deployment/mt-edge >/dev/null   # ConfigMap 改了、/tmp 留著開關的話，換一個乾淨的 Pod
kubectl -n it-mes-prod rollout status deployment/mt-edge --timeout=180s >/dev/null
collectors() { kubectl -n otel-collector get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}'; }
before="$(collectors)"
ulk
normal="$(now)"
# 事故從下一個整 5 分鐘再過 1 分鐘開始（不對齊邊界，才看得到「開始」落在時間窗中間）
inc_at=$(( ( $(date +%s) / 300 + 1 ) * 300 + 60 ))
echo "   正常跑到 $(date -u -d "@${inc_at}" +%H:%M:%SZ)"
wait_until "@${inc_at}"

echo "== 2. 事故：/edge/partial 10% 慢 2 秒、/edge/sub1s 變 800ms（11 分鐘）"
app 'touch /tmp/partial /tmp/sub1s'; incident="$(now)"
wait_until "@$(( inc_at + 660 ))"
app 'rm -f /tmp/partial /tmp/sub1s'; heal="$(now)"
sleep 120

echo "== 3. 容器重啟、Pod 重建"
lk
app 'cat /www/metrics' | grep -F 'partial' | grep _count || true
app 'touch /tmp/crash'; crash="$(now)"
ulk
sleep 180
lk
kubectl -n it-mes-prod delete pod -l app.kubernetes.io/name=mt-edge --wait=false >/dev/null; delete="$(now)"
kubectl -n it-mes-prod rollout status deployment/mt-edge --timeout=180s >/dev/null
ulk
sleep 180
end="$(now)"
sleep 30

# 別的任務在中途重啟 Collector、trace-sampler 的話，那段 trace、metrics 會掉，數字不可信
if [[ "$(collectors)" != "${before}" ]]; then echo "   ⚠ 測試期間 Collector 重啟過（${before} → $(collectors)），結果不可信，請重跑"; fi
args="--normal ${normal} --incident ${incident} --heal ${heal} --crash ${crash} --delete ${delete} --end ${end}"
echo "== 4. 檢查（重跑：CHECK_ONLY=\"${args}\"）"
kubectl -n it-mes-prod get pod -l app.kubernetes.io/name=mt-edge -o custom-columns=POD:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount
rc=0
# shellcheck disable=SC2086
check ${args} || rc=$?
if [[ -z "${KEEP:-}" ]]; then
  lk
  kubectl -n it-mes-prod delete deployment/mt-edge configmap/mt-edge --wait=false >/dev/null
  ulk
fi
exit "${rc}"
