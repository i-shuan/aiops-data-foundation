#!/usr/bin/env bash
# 租戶分流與兩廠雙寫的 E2E，在本機 k3d 上跑，不走 ArgoCD：
#
#   values.yaml → chart 的 namespace.yaml → Namespace label → vmagent → 兩座 vmauth → 兩座 VictoriaMetrics
#
#   ./tests/tenant-routing/run.sh          分流：每個 namespace 的資料在兩座各進了哪個 AccountID、帶什麼 site
#   ./tests/tenant-routing/run.sh outage   停機：停掉站 1、中間重啟 vmagent、再恢復，資料有沒有補回來（多花約 5 分鐘）
#   ./tests/tenant-routing/run.sh down     刪掉這支測試建的 k3d cluster
#
# 正式環境：每個 cluster 的 vmagent → 中科站、二林站的 vmauth → 各自的 VictoriaMetrics
# 本機：    一套 vmagent（扮演中科的 cluster）→ 本機兩座（backends.yaml）
#
# 被測的設定取自正式檔案，只換位址：
#   - vmagent 的分流、雙寫、暫存取自 platform/vmstack-values.yaml 加 platform/site-ctsp.yaml，只換 remoteWrite 的 url
#   - 測試 app 用 app/eqp-collector/k8s，只換 namespace 和 observability.tenant
#
# 情境（check.py 的 CASES）：四個租戶、沒貼、打錯，再加一個自帶假 label 想冒充別人的 pod。
# 全部對 exit 0，任何一項不對 exit 1。VMSTACK_VALUES=<改壞的複本> 可以確認測試抓得到錯。
#
# 只用自己的 context（k3d-tenant-test），不會切換你目前的 kubectl context，也不碰其他 cluster。
# 版本跟 AP1105 一樣：operator v0.74.0（chart 0.67.2）、VictoriaMetrics v1.148.0。
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/../.."

CLUSTER=tenant-test
CTX=k3d-$CLUSTER
HERE=tests/tenant-routing
VALUES=${VMSTACK_VALUES:-platform/vmstack-values.yaml}
APP_IMAGE=equipment-collector:test
MODE=${1:-routing}

k() { kubectl --context "$CTX" "$@"; }
h() { helm --kube-context "$CTX" "$@"; }
fail=0
check() { python "$HERE/check.py" "$@" || fail=1; }
# Windows 的 python 印的是 CRLF，不去掉的話 read 進來的最後一欄會多一個 CR
cases() { python "$HERE/check.py" cases | tr -d '\r'; }
step() { printf '\n=== %s ===\n' "$*"; }

if [ "$MODE" = down ]; then
  k3d cluster delete "$CLUSTER"
  exit 0
fi
case "$MODE" in routing|outage) ;; *) echo "用法：run.sh [outage|down]"; exit 2 ;; esac

before=$(kubectl config current-context 2>/dev/null || true)

# 等 vmagent 的 pod 就緒。它可能是 Deployment 也可能是 StatefulSet（開了 statefulMode），所以看 pod
wait_agent() {
  for _ in $(seq 60); do
    k -n tr-central get pod -l app.kubernetes.io/name=vmagent -o name 2>/dev/null | grep -q . && break
    sleep 3
  done
  k -n tr-central wait --for=condition=ready pod -l app.kubernetes.io/name=vmagent --timeout=240s >/dev/null
}

step "1. k3d cluster $CLUSTER、operator"
if ! k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  # k3s v1.35 的 kubelet 不在 cgroup v1 的主機上跑，會卡到逾時；釘在 v1.30
  k3d cluster create "$CLUSTER" --image rancher/k3s:v1.30.14-k3s2 --no-lb \
    --k3s-arg "--disable=traefik@server:0" --kubeconfig-switch-context=false --timeout 240s >/dev/null
  # kubeconfig 寫的是 host.docker.internal，在這台會解析到連不上的位址
  port=$(docker port "k3d-$CLUSTER-server-0" 6443/tcp | head -1 | sed 's/.*://')
  kubectl config set-cluster "$CTX" --server="https://127.0.0.1:$port" >/dev/null
fi
k get nodes >/dev/null
docker image inspect "$APP_IMAGE" >/dev/null 2>&1 \
  || docker build --build-arg BASE_IMAGE=python:3.12-slim -t "$APP_IMAGE" app/eqp-collector >/dev/null
k3d image import "$APP_IMAGE" busybox:1.36 -c "$CLUSTER" >/dev/null 2>&1
h upgrade --install vmo vm/victoria-metrics-operator --version 0.67.2 -n vm --create-namespace --wait --timeout 240s >/dev/null
echo "  operator $(k -n vm get deploy -o jsonpath='{.items[0].spec.template.spec.containers[0].image}')"

step "2. 本機的兩座：VictoriaMetrics cluster、vmauth，加偽造用的 pod"
# apply 會把 vmauth 的 replicas 設回 1：上一次停機測試如果被中斷，入口在這裡開回來
k apply -f "$HERE/backends.yaml" >/dev/null
for ns in tr-central tr-site2; do
  # vmstorage / vmselect 是 OnDelete 的 StatefulSet，rollout status 不能用，看 operator 回報的狀態
  for _ in $(seq 80); do
    [ "$(k -n "$ns" get vmcluster central -o jsonpath='{.status.updateStatus}' 2>/dev/null)" = operational ] && break
    sleep 3
  done
  k -n "$ns" wait --for=condition=ready pod/vmselect-central-0 pod/vmstorage-central-0 --timeout=180s >/dev/null
  k -n "$ns" rollout status deploy/vminsert-central deploy/vmauth --timeout=240s >/dev/null
done
k -n tr-spoof rollout status deploy/spoof --timeout=120s >/dev/null
echo "  ready"

step "3. vmagent：設定取自 $VALUES 加 platform/site-ctsp.yaml"
python "$HERE/check.py" agent "$VALUES" | k apply -f - >/dev/null
check pruned "$VALUES"
sleep 10    # 讓 operator 先把 Deployment / StatefulSet 換好，不然會等到舊的那個 pod
wait_agent
check loaded "$VALUES"

step "4. 測試 app：eqp-collector 的 chart，每個情境一個 namespace"
cases | while read -r ns tenant; do
  # tenant 是 - 表示不寫。寫成 null，helm 才會把 chart 預設的 tenant 拿掉
  if [ "$tenant" = - ]; then set_tenant=observability.tenant=null; else set_tenant=observability.tenant=$tenant; fi
  h upgrade --install "$ns" app/eqp-collector/k8s -n default --reset-values \
    --set namespace="$ns" --set "$set_tenant" >/dev/null
done
cases | while read -r ns _; do k -n "$ns" rollout status deploy/eqp-collector --timeout=180s >/dev/null; done
check labels

if [ "$MODE" = outage ]; then
  step "5. 停機：關掉站 1 的入口、中間重啟 vmagent、再恢復"
  # 不管成功、失敗還是被中斷，結束時都把站 1 的入口開回來
  trap 'k -n tr-central scale deploy/vmauth --replicas=1 >/dev/null 2>&1 || true' EXIT
  sleep 60    # 先讓新設定穩定送一陣子
  check outage
else
  step "5. 每個 namespace 的資料在兩座各進了哪個 AccountID"
  # 等舊設定送出的樣本離開檢查的時間窗，不然上一輪的結果會被算進來。
  # 要等的是 check.py 的 WINDOW（30s）加上 vmselect 預設往回看的 30s，再留一點餘裕。
  # 等太短的症狀：剛跑完改壞的複本再跑正常的，會看到資料同時在新舊兩個 AccountID 而 FAIL
  sleep 90
  check routing 180
fi

after=$(kubectl config current-context 2>/dev/null || true)
[ "$before" = "$after" ] || { echo "kubectl 的 current-context 被改了：$before → $after"; exit 1; }
if [ "$fail" = 0 ]; then printf '\nPASS\n'; else printf '\nFAIL\n'; exit 1; fi
