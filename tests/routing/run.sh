#!/usr/bin/env bash
# metrics 分流的端到端測試。在本機 k3d 上跑，會一直留著模擬器產生資料。
#
#   ./tests/routing/run.sh
#
# 流程：
#   1. 建本機的 vmauth + 三個 VMSingle（代表三個 tenant）
#   2. 從正式的 vmstack-values.yaml 產生測試用的 VMAgent
#   3. 建六個 namespace，不手動標 label，交給 ns-labeler 依測試對照表標
#   4. 每個 namespace 部署一份 metrics-sim，持續產生資料
#   5. 查三個 tenant 各收到了什麼，跟預期比對
#
# 前提：k3d 的 zk cluster 在跑、VictoriaMetrics operator 已裝、
#       ns-labeler 已經用 helm 裝在 vm-monitoring、
#       metrics-sim:test 和 vmauth 映像已經 import 進 k3d（見 README）。
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8

cd "$(dirname "$0")/../.."
HERE=tests/routing
SCENARIOS="it imc both none typo plat"

step() { printf '\n=== %s ===\n' "$*"; }

step "1. 本機的 vmauth 與三個 tenant"
kubectl apply -f "$HERE/backends.yaml" >/dev/null
for t in it imc platform; do
  until kubectl -n route-test get deploy "vmsingle-$t" >/dev/null 2>&1; do sleep 2; done
done
kubectl -n route-test rollout status deploy/vmsingle-it deploy/vmsingle-imc deploy/vmsingle-platform deploy/vmauth-test --timeout=120s

step "2. 從正式設定產生測試用 VMAgent"
python "$HERE/gen_vmagent.py" | kubectl apply -f - >/dev/null
until kubectl -n route-test get deploy vmagent-under-test >/dev/null 2>&1; do sleep 2; done
kubectl -n route-test rollout status deploy/vmagent-under-test --timeout=120s

step "3. 建 namespace，交給 ns-labeler 標"
for s in $SCENARIOS; do
  ns="zk-sim-$s-prod"
  kubectl create ns "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # 每次跑都從乾淨的狀態開始，才驗得出 ns-labeler 真的有標
  kubectl label ns "$ns" dept- group- system- >/dev/null 2>&1 || true
done
helm upgrade ns-labeler platform/cluster-addons/ns-labeler -n vm-monitoring \
  --set image.registry=docker.io -f "$HERE/ns-labeler-values.yaml" --wait --timeout 60s >/dev/null
job="ns-labeler-route-test-$(date +%s)"
kubectl -n vm-monitoring create job --from=cronjob/ns-labeler "$job" >/dev/null
kubectl -n vm-monitoring wait --for=condition=complete "job/$job" --timeout=90s >/dev/null
kubectl -n vm-monitoring logs "job/$job" | grep -E "zk-sim-|^已正確|^對不到" || true
kubectl -n vm-monitoring delete job "$job" >/dev/null
echo
kubectl get ns -l 'kubernetes.io/metadata.name in (zk-sim-it-prod,zk-sim-imc-prod,zk-sim-both-prod,zk-sim-none-prod,zk-sim-typo-prod,zk-sim-plat-prod)' -L dept,group,system

step "4. 部署模擬器"
helm dependency build charts/metrics-sim --skip-refresh >/dev/null
for s in $SCENARIOS; do
  helm upgrade --install metrics-sim charts/metrics-sim -n "zk-sim-$s-prod" \
    --set image.repository=metrics-sim --set image.tag=test >/dev/null
done
for s in $SCENARIOS; do
  kubectl -n "zk-sim-$s-prod" rollout status deploy/metrics-sim --timeout=90s >/dev/null
done
echo "6 個模擬器都在跑"

step "5. 查三個 tenant 收到什麼（最多等 120 秒）"
python "$HERE/check.py" --wait 120
