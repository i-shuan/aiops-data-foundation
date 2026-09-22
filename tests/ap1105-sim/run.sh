#!/usr/bin/env bash
# 在本機模擬 AP1105，照 README「已經在跑的 vmstack 加上分流」的步驟走一遍。
#
#   ./tests/ap1105-sim/run.sh            從頭跑（會先刪掉 k3d cluster ap1105 重建）
#   ./tests/ap1105-sim/run.sh <步驟>     只跑某一步之後的步驟，例如 run.sh routed
#
# 步驟：
#   cluster   建 k3d cluster ap1105（ghcr.io 故意連不到）
#   ap1105    重現 AP1105 現況：rev1 chart 0.86.2 deployed → rev2 chart 0.91.0 post-upgrade hook failed
#   diagnose  kubectl explain 的假陰性、欄位到底會不會被保留、operator 版本
#   apps      中央 vmauth（假 token）+ 5 個 eqp-collector（imc / it / it.imc / 沒 group / 打錯字）
#   routed    hook 還壞著的狀態下套 overlay，驗證分流
#   rollback  helm rollback 回套 overlay 前，驗證回到單一 insert/0
#   hookfix   關掉 syncJob 修 hook（看 VMRule 會不會被刪）
#   final     hook 修好後再套一次 overlay，release 要是 deployed
#
# 只用 k3d 的 context（k3d-ap1105），不會切換你目前的 kubectl context。
set -euo pipefail
cd "$(dirname "$0")/../.."
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8

CTX=k3d-ap1105
NODE=k3d-ap1105-server-0
SIM=tests/ap1105-sim
CHART=vm/victoria-metrics-k8s-stack
VMA=vmstack-victoria-metrics-k8s-stack
SIM_VMAUTH=http://vmauth.central-sim.svc:8427/prometheus/api/v1/write
WORK="$SIM/.work"
mkdir -p "$WORK"

k() { kubectl --context "$CTX" "$@"; }
h() { helm --kube-context "$CTX" "$@"; }
check() { python "$SIM/check.py" "$@"; }
say() { printf '\n==================== %s\n' "$*"; }
last_rev() { h history vmstack -n vm-monitoring -o json | python -c 'import sys,json; print(json.load(sys.stdin)[-1]["revision"])'; }
history() { h history vmstack -n vm-monitoring | sed 's/^/  /'; }

# 跟 AP1105 一樣：docker save 成 tar → 複製到 node → ctr -n k8s.io images import
import_image() {
  local img=$1 tar="$WORK/import.tar"
  docker save -o "$tar" "$img"
  docker cp "$tar" "$NODE:/tmp/import.tar"
  docker exec "$NODE" ctr -n k8s.io images import /tmp/import.tar >/dev/null
  docker exec "$NODE" rm -f /tmp/import.tar
  rm -f "$tar"
  echo "  匯入 $img"
}

# README 的步驟 1 ~ 4：helm get values → overlay（只換 url）→ dry-run diff → 套用
apply_overlay() {
  local timeout=$1
  h get values vmstack -n vm-monitoring -o yaml > "$WORK/current-values.yaml"
  sed "s#https://vmauth.aillm.spil.com.tw/prometheus/api/v1/write#$SIM_VMAUTH#" \
    platform/vmagent-routing-overlay.yaml > "$WORK/overlay.yaml"
  h get manifest vmstack -n vm-monitoring > "$WORK/manifest-before.yaml"
  echo "  --- dry-run（--dry-run=server）"
  h upgrade vmstack "$CHART" --version 0.91.0 -n vm-monitoring \
    -f "$WORK/current-values.yaml" -f "$WORK/overlay.yaml" --dry-run=server > "$WORK/dry-run.txt"
  check diff "$WORK/manifest-before.yaml" "$WORK/dry-run.txt"
  echo "  --- 套用"
  h upgrade vmstack "$CHART" --version 0.91.0 -n vm-monitoring \
    -f "$WORK/current-values.yaml" -f "$WORK/overlay.yaml" --timeout "$timeout" 2>&1 | grep -E '^(Error|STATUS|REVISION)' | sed 's/^/  /' || true
}

verify_routed() {
  echo "  --- vmagent 實際載入的規則"; check relabel routed --wait 180
  echo "  --- 三條 remoteWrite 都送成功";  check remotewrite --wait 120
  echo "  --- target 帶上 namespace 的 label"; check targets set --wait 120
  echo "  --- 每個 namespace 進了哪些 tenant（等舊資料離開 1 分鐘的查詢視窗）"; check routing --wait 300
  echo "  --- it.imc 抓一次、送兩份"; check fanout
  echo "  --- 沒貼 group 的 namespace 只在 tenant 0"; check platform
}

step_cluster() {
  say "cluster：建 k3d cluster ap1105"
  k3d cluster delete ap1105 >/dev/null 2>&1 || true
  k3d cluster create ap1105 --image rancher/k3s:v1.30.14-k3s2 --no-lb --kubeconfig-switch-context=false \
    --k3s-arg "--disable=traefik@server:0" --k3s-arg "--disable=servicelb@server:0" \
    --k3s-arg "--disable=metrics-server@server:0" --k3s-arg "--disable-default-registry-endpoint@server:0" \
    2>&1 | grep -E 'successfully|ERRO' | sed 's/^/  /'
  # Docker Desktop 上 k3d 會寫 host.docker.internal，重啟 node 之後會連不上，改成 127.0.0.1
  kubectl config set-cluster "$CTX" --server="https://127.0.0.1:$(docker port "$NODE" 6443/tcp | head -1 | sed 's/.*://')" >/dev/null
  # rev1 的 sync-job 在 AP1105 是成功的（當時的 image 已經在 node 上）。
  # 先讓 node 自己的 containerd 拉好，再開始擋 ghcr.io
  docker exec "$NODE" crictl pull ghcr.io/victoriametrics/sync-job:v0.0.11 >/dev/null
  echo "  node 上先放好 sync-job:v0.0.11"
  docker cp "$SIM/registries.yaml" "$NODE:/etc/rancher/k3s/registries.yaml"
  docker restart "$NODE" >/dev/null
  local i
  for i in $(seq 1 60); do k get nodes 2>/dev/null | grep -q ' Ready' && break; sleep 3; done
  k get nodes 2>/dev/null | grep -q ' Ready' || { echo "  node 重啟後 3 分鐘還沒 Ready"; exit 1; }
  if docker exec "$NODE" crictl pull ghcr.io/victoriametrics/sync-job:v0.0.14 >/dev/null 2>&1; then
    echo "  ghcr.io 竟然拉得到，模擬不成立"; exit 1
  fi
  echo "  ghcr.io 拉不到了（跟 AP1105 一樣）"
  docker image inspect equipment-collector:test >/dev/null 2>&1 || \
    docker build -q --build-arg BASE_IMAGE=python:3.12-slim -t equipment-collector:test services/eqp-collector >/dev/null
  import_image equipment-collector:test
  import_image victoriametrics/vmauth:v1.148.0
}

step_ap1105() {
  # 重跑要從 cluster 開始：helm uninstall 會卡在 chart 的 pre-delete hook，
  # namespace 也會因為 VM 資源上的 operator finalizer 卡在 Terminating
  if h status vmstack -n vm-monitoring >/dev/null 2>&1; then
    echo "  vmstack 已經存在，請從 cluster 重跑：./tests/ap1105-sim/run.sh"; exit 1
  fi
  say "ap1105：rev1 chart 0.86.2"
  h install vmstack "$CHART" --version 0.86.2 -n vm-monitoring --create-namespace \
    -f "$SIM/vmstack-values.yaml" --wait --timeout 10m 2>&1 | grep -E '^(Error|STATUS)' | sed 's/^/  /'
  say "ap1105：rev2 chart 0.91.0（sync-job v0.0.14 在 ghcr.io，拉不到）"
  h upgrade vmstack "$CHART" --version 0.91.0 -n vm-monitoring \
    -f "$SIM/vmstack-values.yaml" --timeout 2m 2>&1 | grep -E '^(Error|STATUS)' | sed 's/^/  /' || true
  history
  k -n vm-monitoring rollout status deploy/vmagent-$VMA --timeout 5m >/dev/null
  k -n vm-monitoring get pods --no-headers | awk '{printf "  %-70s %s %s\n", $1, $2, $3}'
  echo "  --- sync-job 的 pod"
  k -n vm-monitoring get pods -l job-name=vmstack-victoria-metrics-k8s-stack-sync-job \
    -o jsonpath='{range .items[*]}  {.metadata.name}  {.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}'
  echo "  --- VMAgent 現況"; check state
}

step_diagnose() {
  say "diagnose：AP1105 上看到的 field does not exist 是真的嗎"
  local f
  for f in vmagent.spec.scrapeClasses vmagent.spec.remoteWrite.inlineUrlRelabelConfig; do
    printf '  kubectl explain %s → ' "$f"
    { k explain "$f" 2>&1 || true; } | grep -E '^(FIELD|error)' | head -1
  done
  echo "  CRD 長什麼樣："
  check crd | sed 's/^/  /'
  echo "  API server 會不會保留欄位（server-side dry-run，不會真的改）："
  printf '    scrapeClasses → '
  k -n vm-monitoring patch vmagent $VMA --type=merge --dry-run=server \
    -p '{"spec":{"scrapeClasses":[{"name":"probe"}]}}' -o jsonpath='{.spec.scrapeClasses[*].name}{"\n"}'
  printf '    inlineUrlRelabelConfig → '
  k -n vm-monitoring patch vmagent $VMA --type=merge --dry-run=server \
    -p '{"spec":{"remoteWrite":[{"url":"http://probe","inlineUrlRelabelConfig":[{"action":"keep","source_labels":["group"],"regex":"probe"}]}]}}' \
    -o jsonpath='{.spec.remoteWrite[0].inlineUrlRelabelConfig[0].regex}{"\n"}'
  echo "  在跑的 operator："
  k get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}  {.spec.template.spec.containers[*].image}{"\n"}{end}' \
    | grep 'victoriametrics/operator' | sed 's/^/    /'
}

step_apps() {
  say "apps：中央 vmauth + eqp-collector"
  k apply -f "$SIM/central-vmauth.yaml" | sed 's/^/  /'
  local chart=applications/eqp-collector/helm
  h upgrade --install eqp-collector $chart -n default --reset-values --wait >/dev/null
  h upgrade --install eqp-it $chart -n default --reset-values --wait \
    --set namespace=eqp-sim-it --set observability.group=it --set observability.dept=D3120 >/dev/null
  h upgrade --install eqp-shared $chart -n default --reset-values --wait \
    --set namespace=eqp-sim-shared --set observability.group=it.imc >/dev/null
  h upgrade --install eqp-none $chart -n default --reset-values --wait \
    --set namespace=eqp-sim-none --set observability=null >/dev/null
  h upgrade --install eqp-typo $chart -n default --reset-values --wait \
    --set namespace=eqp-sim-typo --set observability.group=it.imcc >/dev/null
  k get ns -l 'kubernetes.io/metadata.name in (eqp-collector-dev,eqp-sim-it,eqp-sim-shared,eqp-sim-none,eqp-sim-typo)' \
    -L dept,group,system | sed 's/^/  /'
  k -n central-sim rollout status deploy/vmauth --timeout 2m >/dev/null
  echo "  --- 改之前：target 抓得到，但沒有 dept/group/system（跟 AP1105 一樣）"
  check targets none --wait 120
}

step_routed() {
  say "routed：hook 還壞著的狀態下套 overlay"
  k -n vm-monitoring create secret generic vmauth-token \
    --from-literal=it=sim-token-it --from-literal=imc=sim-token-imc --from-literal=platform=sim-token-platform \
    --dry-run=client -o yaml | k apply -f - | sed 's/^/  /'
  apply_overlay 90s
  history
  echo "  --- VMAgent 現況"; check state
  verify_routed
}

step_rollback() {
  say "rollback：回到套 overlay 之前"
  # 回到套 overlay 前的那一版（rev2：0.91.0、hook failed，就是 AP1105 現在的樣子）
  local rev=$(( $(last_rev) - 1 ))
  echo "  helm rollback vmstack $rev"
  h rollback vmstack "$rev" -n vm-monitoring --wait --timeout 3m 2>&1 | sed 's/^/  /'
  history
  echo "  --- VMAgent 現況"; check state
  echo "  --- vmagent 實際載入的規則"; check relabel single --wait 180
}

step_hookfix() {
  say "hookfix：關掉 syncJob，但保住它之前建的告警規則"
  echo "  --- 關之前"
  # 不要用 tee /dev/stderr：Git Bash 上它會重新打開 log 檔，把前面的輸出整個蓋掉
  local n
  check synced > "$WORK/synced.txt"
  cat "$WORK/synced.txt"
  n=$(sed -n 's/^  COUNT //p' "$WORK/synced.txt")
  # sync-job 建的 VMRule / dashboard 的 owner 是它的 ServiceAccount。直接關 syncJob 的話，
  # Helm 會刪掉這個 ServiceAccount，garbage collector 接著把所有 VMRule 一起刪掉（第一次模擬：37 → 0）。
  # 先用 orphan 的方式刪 ServiceAccount：規則留下來，只是拿掉 owner。
  echo "  --- 先 orphan 掉 owner"
  k -n vm-monitoring delete serviceaccount vmstack-victoria-metrics-k8s-stack-sync-job --cascade=orphan 2>&1 | sed 's/^/  /'
  h get values vmstack -n vm-monitoring -o yaml > "$WORK/current-values.yaml"
  h upgrade vmstack "$CHART" --version 0.91.0 -n vm-monitoring -f "$WORK/current-values.yaml" \
    --set syncJob.enabled=false --wait --timeout 3m 2>&1 | grep -E '^(Error|STATUS)' | sed 's/^/  /'
  sleep 20
  echo "  --- 關之後（等 20 秒讓 garbage collector 跑完）"; check synced --expect "$n"
  history
  # hook 的 Job 不算 release 的資源，syncJob 關掉之後舊的那個還在，會一直重試拉 image
  echo "  --- 留下來的 sync-job（手動刪掉）"
  k -n vm-monitoring get job vmstack-victoria-metrics-k8s-stack-sync-job --no-headers 2>&1 | sed 's/^/  /' || true
  k -n vm-monitoring delete job vmstack-victoria-metrics-k8s-stack-sync-job --ignore-not-found 2>&1 | sed 's/^/  /'
}

step_final() {
  say "final：hook 修好後再套一次 overlay"
  apply_overlay 3m
  history
  verify_routed
}

# 真正的 blocker 長什麼樣：CRD 有完整 schema、但是舊版，沒有 scrapeClasses / inlineUrlRelabelConfig。
# 做法：把現在的 CRD 拿掉這兩個欄位再放回去，看 API server 和 vmagent 怎麼反應，最後還原。
step_oldcrd() {
  say "oldcrd：如果 AP1105 的 CRD 是沒有這兩個欄位的舊版"
  local crd=vmagents.operator.victoriametrics.com
  k get crd $crd -o json > "$WORK/crd-backup.json"
  python - "$WORK/crd-backup.json" "$WORK/crd-old.json" "$WORK/crd-restore.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
    c["metadata"].pop(k, None)
c.pop("status", None)
json.dump(c, open(sys.argv[3], "w", encoding="utf-8"))
for v in c["spec"]["versions"]:
    p = v["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    p.pop("scrapeClasses")
    p["remoteWrite"]["items"]["properties"].pop("inlineUrlRelabelConfig")
json.dump(c, open(sys.argv[2], "w", encoding="utf-8"))
PY
  k replace -f "$WORK/crd-old.json" | sed 's/^/  /'
  echo "  --- 診斷（kubectl explain 有快取，剛換完 CRD 的結果不能信，看下面兩個）"
  check crd | sed 's/^/  /'
  printf '    server-side dry-run 加 scrapeClasses → '
  k -n vm-monitoring patch vmagent $VMA --type=merge --dry-run=server \
    -p '{"spec":{"scrapeClasses":[{"name":"probe"}]}}' -o jsonpath='[{.spec.scrapeClasses[*].name}]{"\n"}' 2>&1 | tr '\n' ' '; echo
  echo "  --- 已經存進去的 VMAgent 讀出來"; check state
  printf '  remoteWrite[1].inlineUrlRelabelConfig → [%s]\n' \
    "$(k -n vm-monitoring get vmagent $VMA -o jsonpath='{.spec.remoteWrite[1].inlineUrlRelabelConfig}')"
  # 正式環境是 helm upgrade 觸發 operator reconcile，這裡碰一下 annotation 達到一樣的效果
  k -n vm-monitoring annotate vmagent $VMA sim/touch="$(date +%s)" --overwrite >/dev/null
  echo "  --- 等 operator 重建 vmagent，看資料有沒有外洩到每個 tenant"
  local i
  for i in $(seq 1 18); do check leak >/dev/null 2>&1 && break; sleep 10; done
  check leak || true
  echo "  --- vmagent 的狀況"
  k -n vm-monitoring get pods -l app.kubernetes.io/name=vmagent --no-headers | sed 's/^/  /'
  echo "  --- 還原 CRD"
  k replace -f "$WORK/crd-restore.json" | sed 's/^/  /'
  # 舊 CRD 期間只要 VMAgent 被寫過一次（這裡的 annotate、operator 更新），存進去的就是裁掉之後的版本，
  # CRD 換回來也不會自己長回來
  echo "  --- 只換回 CRD：欄位回來了嗎"; check state
  echo "  --- 再 helm upgrade 一次（同一份 values）把欄位補回去"
  apply_overlay 3m
  check state
  check relabel routed --wait 240
}

# 情況 C 怎麼修：換成跟在跑的 operator（v0.74.0）同一份 chart（0.91.0）帶的 CRD。
# helm upgrade 不會更新 chart crds/ 資料夾裡的 CRD，所以要自己套。
step_crdfix() {
  say "crdfix：把 CRD 換成 chart 0.91.0（operator v0.74.0）帶的版本"
  echo "  --- 先做出情況 C"
  k replace -f "$WORK/crd-old.json" | sed 's/^/  /'
  check crd | sed 's/^/  /'
  echo "  --- 1. 備份 CR（換 CRD 前後比對用，看有沒有欄位被裁掉）"
  local kinds=vmagents,vmclusters,vmservicescrapes,vmrules
  k get $kinds -A -o json > "$WORK/cr-before.json"
  echo "  --- 2. 拿 chart 0.91.0 的 CRD"
  h show crds "$CHART" --version 0.91.0 > "$WORK/vm-crds.yaml"
  echo "  $(grep -c '^kind: CustomResourceDefinition' "$WORK/vm-crds.yaml") 個 CRD"
  echo "  --- 3. server-side dry-run，再套用"
  k apply --server-side --force-conflicts --dry-run=server -f "$WORK/vm-crds.yaml" >/dev/null
  k apply --server-side --force-conflicts -f "$WORK/vm-crds.yaml" | awk '{print $NF}' | sort | uniq -c | sed 's/^/  /'
  echo "  --- 4. 確認"
  check crd | sed 's/^/  /'
  printf '    dry-run 加 scrapeClasses → [%s]\n' "$(k -n vm-monitoring patch vmagent $VMA --type=merge --dry-run=server \
    -p '{"spec":{"scrapeClasses":[{"name":"probe"}]}}' -o jsonpath='{.spec.scrapeClasses[*].name}' 2>&1 | tr '\n' ' ')"
  k get $kinds -A -o json > "$WORK/cr-after.json"
  python - "$WORK/cr-before.json" "$WORK/cr-after.json" <<'PY'
import json, sys
def load(p):
    out = {}
    for i in json.load(open(p, encoding="utf-8"))["items"]:
        out[(i["kind"], i["metadata"].get("namespace"), i["metadata"]["name"])] = i.get("spec")
    return out
def kept(x, y):
    """x 的每個欄位在 y 裡都還在、值一樣（y 可以多東西）。"""
    if isinstance(x, dict):
        return isinstance(y, dict) and all(k in y and kept(v, y[k]) for k, v in x.items())
    if isinstance(x, list):
        return isinstance(y, list) and len(x) == len(y) and all(kept(i, j) for i, j in zip(x, y))
    return x == y
a, b = load(sys.argv[1]), load(sys.argv[2])
# 舊 CRD 下讀出來的是裁掉之後的樣子，換成新 CRD 後會多出欄位（存著的 scrapeClasses 又看得到了），這是正常的。
# 要抓的是反方向：原本有的欄位不見了。
lost = [k for k in a if not kept(a[k], b.get(k))]
more = [k for k in a if a[k] != b.get(k) and k not in lost]
print("  CR 的 spec：%d 個，欄位不見的 %s；多出欄位的 %s" % (
    len(a), lost[:5] or "無", ["%s/%s" % (k[0], k[2]) for k in more] or "無"))
print("  %s" % ("PASS" if not lost else "FAIL"))
sys.exit(1 if lost else 0)
PY
  check relabel routed --wait 120
}

STEPS=(cluster ap1105 diagnose apps routed rollback hookfix final oldcrd crdfix)
start=${1:-cluster}
run=false
for s in "${STEPS[@]}"; do
  [ "$s" = "$start" ] && run=true
  $run && "step_$s"
  # ONLY=1 ./run.sh <步驟>：只跑那一步
  $run && [ -n "${ONLY:-}" ] && break
done
$run || { echo "不認得的步驟：$start（可以用 ${STEPS[*]}）"; exit 1; }
say "完成"
