#!/usr/bin/env bash
# 整條主線的 E2E，在本機 k3d 上跑，真的經過 ArgoCD：
#
#   values.yaml → ArgoCD ApplicationSet → Namespace labels → vmagent → vmauth → tenant
#
#   ./tests/routing/run.sh
#
# 正式環境：vmagent → 中央 vmauth → IT / IMC / platform tenant
# 本機：    vmagent → 本機 vmauth → VMSingle it / imc / platform
#
# 所有設定都取自 platform/ 的正式檔案，只換位址：
#   - vmagent 取自 platform/victoriametrics/vmstack-values.yaml，只換 remoteWrite 的 URL
#   - ApplicationSet 取自 platform/argocd/applicationset.yaml，只換 repoURL（指向本機 git server）
#   - 測試 app 的 values.yaml 取自 app/metrics-sim/k8s/values.yaml，只換 namespace、映像、observability
#
# 全部對 exit 0，任何一項不對 exit 1。VMSTACK_VALUES=<改壞的複本> 可以確認測試抓得到錯。
#
# 前提（見 README）：ArgoCD v3.5.3 core、VictoriaMetrics operator、
#                    metrics-sim:test 和 vmauth 映像已經 import 進 k3d
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")"

step() { printf '\n=== %s ===\n' "$*"; }
# kubectl、helm 是 Windows 程式時看不懂 Git Bash 的 /tmp/...，要轉成 C:/...
native() { cygpath -m "$1" 2>/dev/null || printf '%s' "$1"; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
CASES="it imc shared none bad"

kubectl -n argocd get deploy argocd-applicationset-controller >/dev/null 2>&1 \
  || { echo "找不到 ArgoCD，先照 README 裝好"; exit 1; }

step "1. 本機 vmauth、三個 tenant、git server"
kubectl apply -f backends.yaml >/dev/null
for t in it imc platform; do
  until kubectl -n route-test get deploy "vmsingle-$t" >/dev/null 2>&1; do sleep 2; done
done
kubectl -n route-test rollout status deploy/vmsingle-it deploy/vmsingle-imc deploy/vmsingle-platform \
  deploy/vmauth-test deploy/git-server --timeout=120s >/dev/null
echo "OK"

step "2. vmagent（取自 platform/victoriametrics/vmstack-values.yaml）"
python gen.py vmagent > "$tmp/vmagent.yaml"
kubectl apply -f "$(native "$tmp/vmagent.yaml")" >/dev/null
until kubectl -n route-test get deploy vmagent-under-test >/dev/null 2>&1; do sleep 2; done
kubectl -n route-test rollout status deploy/vmagent-under-test --timeout=120s >/dev/null
python check.py config "$(native "$tmp/vmagent.yaml")"

step "3. 清空三個 tenant 和上一輪的 app"
for t in it imc platform; do kubectl -n route-test rollout restart "deploy/vmsingle-$t" >/dev/null; done
kubectl -n argocd delete applicationset under-test --ignore-not-found --wait=true >/dev/null
for c in $CASES; do kubectl delete ns "zk-sim-$c-prod" --ignore-not-found --wait=false >/dev/null; done
for c in $CASES; do kubectl wait --for=delete "ns/zk-sim-$c-prod" --timeout=120s >/dev/null 2>&1 || true; done
kubectl -n route-test rollout status deploy/vmsingle-it deploy/vmsingle-imc deploy/vmsingle-platform --timeout=120s >/dev/null
# 故意先建一個沒有 label 的 namespace：驗證 ArgoCD 會把 label 補到已經存在的 namespace 上
kubectl create ns zk-sim-imc-prod >/dev/null
echo "OK（zk-sim-imc-prod 事先建好，沒有 label）"

step "4. 測試 app 灌進本機 git server"
# 選還活著的 pod：重啟後舊 pod 在結束前狀態仍然是 Running
pod=$(kubectl -n route-test get pods -l app=git-server \
  -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' | head -1)
python gen.py repo | kubectl -n route-test exec -i "$pod" -- sh -c '
  set -e; rm -rf /srv/work /srv/git/repo.git; mkdir -p /srv/work /srv/git; tar -x -C /srv/work
  cd /srv/work && git init -q -b main && git add -A && git -c user.email=t@t -c user.name=test commit -qm test
  git clone -q --bare /srv/work /srv/git/repo.git
  echo "commit $(git rev-parse --short HEAD)：$(ls app | tr "\n" " ")"'

step "5. ArgoCD ApplicationSet（取自 platform/argocd/applicationset.yaml）"
python gen.py appset > "$tmp/appset.yaml"
kubectl apply -f "$(native "$tmp/appset.yaml")" >/dev/null
deadline=$((SECONDS + 300))
until [ "$(kubectl -n argocd get applications -o go-template='{{range .items}}{{if and (eq .status.sync.status "Synced") (eq .status.health.status "Healthy")}}x{{end}}{{end}}' 2>/dev/null)" = "xxxxx" ]; do
  [ $SECONDS -lt $deadline ] || { kubectl -n argocd get applications; echo "等了 300 秒，Application 還沒全部 Synced / Healthy"; exit 1; }
  sleep 5
done
kubectl -n argocd get applications -o custom-columns=APP:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,NAMESPACE:.spec.destination.namespace

step "6. ArgoCD 貼到 namespace 上的 label"
python check.py labels

step "7. namespace label → tenant（最多等 150 秒）"
python check.py routing --wait 150
