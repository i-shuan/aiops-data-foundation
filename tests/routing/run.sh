#!/usr/bin/env bash
# C. namespace label → tenant 分流的端到端測試。在本機 k3d 上跑，會一直留著模擬器產生資料。
#
#   ./tests/routing/run.sh
#
# 流程：
#   1. 建本機的 vmauth + 三個 VMSingle（代表 IT / IMC / platform 三個 tenant）
#   2. 從正式的 vmstack-values.yaml 產生測試用的 VMAgent
#   3. namespace 的 label 走主線：app.yaml → CI 驗證 → 渲染 template → 貼到 namespace
#      （最後一步正式環境是 ArgoCD 做的，這裡用 kubectl label 代替）
#   4. 每個 namespace 部署一份 metrics-sim，持續產生資料
#   5. 查三個 tenant 各收到什麼、共用的那份是不是同一次抓取
#
# 前提：k3d 的 zk cluster 在跑、VictoriaMetrics operator 已裝、
#       metrics-sim:test 和 vmauth 映像已經 import 進 k3d（見 README）。
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8

cd "$(dirname "$0")/../.."
HERE=tests/routing
CASES=tests/app-yaml/cases

step() { printf '\n=== %s ===\n' "$*"; }

step "1. 本機的 vmauth 與三個 tenant"
kubectl apply -f "$HERE/backends.yaml" >/dev/null
for t in it imc platform; do
  until kubectl -n route-test get deploy "vmsingle-$t" >/dev/null 2>&1; do sleep 2; done
done
kubectl -n route-test rollout status deploy/vmsingle-it deploy/vmsingle-imc deploy/vmsingle-platform deploy/vmauth-test --timeout=120s

step "2. 從正式設定產生測試用 VMAgent"
generated=$(mktemp)
trap 'rm -f "$generated"' EXIT
# kubectl、helm 是 Windows 程式時看不懂 Git Bash 的 /tmp/... 路徑
generated_native=$(cygpath -m "$generated" 2>/dev/null || printf '%s' "$generated")
python "$HERE/gen_vmagent.py" > "$generated"
kubectl apply -f "$generated_native" >/dev/null
until kubectl -n route-test get deploy vmagent-under-test >/dev/null 2>&1; do sleep 2; done
kubectl -n route-test rollout status deploy/vmagent-under-test --timeout=120s
# 設定改了不會馬上生效。不等的話會測到舊設定，舊設定剛好是對的時候測試會假裝通過。
python "$HERE/await_config.py" "$generated_native" --timeout 180

step "2b. 清空三個 tenant，只看新設定送進來的資料"
for t in it imc platform; do kubectl -n route-test rollout restart "deploy/vmsingle-$t" >/dev/null; done
kubectl -n route-test rollout status deploy/vmsingle-it deploy/vmsingle-imc deploy/vmsingle-platform --timeout=120s

step "3. namespace 與 label"
fresh_ns() {  # 建 namespace，清掉舊 label，每次都從乾淨的狀態開始
  kubectl create ns "$1" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label ns "$1" dept- group- system- >/dev/null 2>&1 || true
}

# 走主線的三個：label 來自 app.yaml，跟正式環境同一條路
for s in it imc shared; do
  f="$CASES/ok-$s.yaml"
  ns=$(sed -n 's/^namespace: *//p' "$f")
  fresh_ns "$ns"
  python platform/argocd/validate_observability.py "$f" >/dev/null   # CI 沒過就不往下走
  # shellcheck disable=SC2046
  kubectl label ns "$ns" $(bash tests/app-yaml/render.sh "$f") --overwrite >/dev/null
done

# 沒有 label：沒有 app.yaml 的 namespace
fresh_ns zk-sim-none-prod

# 有 label 但值不合法：正式環境會被 CI 擋下，這裡直接貼上去，驗證 vmagent 自己也不會送錯
fresh_ns zk-sim-bad-prod
kubectl label ns zk-sim-bad-prod dept=D9999 group=nope --overwrite >/dev/null

kubectl get ns zk-sim-it-prod zk-sim-imc-prod zk-sim-shared-prod zk-sim-none-prod zk-sim-bad-prod -L dept,group,system

step "4. 部署模擬器"
helm dependency build applications/metrics-sim/helm --skip-refresh >/dev/null
for ns in zk-sim-it-prod zk-sim-imc-prod zk-sim-shared-prod zk-sim-none-prod zk-sim-bad-prod; do
  helm upgrade --install metrics-sim applications/metrics-sim/helm -n "$ns" \
    --set image.repository=metrics-sim --set image.tag=test >/dev/null
done
for ns in zk-sim-it-prod zk-sim-imc-prod zk-sim-shared-prod zk-sim-none-prod zk-sim-bad-prod; do
  kubectl -n "$ns" rollout status deploy/metrics-sim --timeout=90s >/dev/null
done
echo "5 個模擬器都在跑"

step "5. 查三個 tenant 收到什麼（最多等 150 秒）"
python "$HERE/check.py" --wait 150
