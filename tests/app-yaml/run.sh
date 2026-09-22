#!/usr/bin/env bash
# 驗證「label 寫在 app.yaml、由 ArgoCD 貼到 namespace」這條路。
#
#   ./tests/app-yaml/run.sh
#
#   1. CI 驗證：每個案例 validate_observability.py 的結果要跟檔案第一行的 `# expect:` 一致
#   2. template：用 helm 的模板引擎渲染 namespace-metadata.tmpl，看出來的 label 對不對
#      （ArgoCD 的 ApplicationSet 也是 Go template + sprig，函式行為一樣，但這不是 ArgoCD 本身）
#   3. 照 ArgoCD 會做的方式把 label 貼到 namespace，跑 tests/routing 的同一套路由檢查
#
# 前提：tests/routing/run.sh 跑過一次（六個模擬器和三個 tenant 已經在 zk 上）。
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8

cd "$(dirname "$0")/../.."
CASES=tests/app-yaml/cases
TMPL=platform/argocd/namespace-metadata.tmpl
# 照 CI 應有的方式跑：一定要帶 ns-labeler 的 values，才會檢查兩份對照表一致、
# 以及同一個 namespace 沒有被兩邊同時管。
NSL_VALUES=platform/cluster-addons/ns-labeler/values.yaml
validate() { python platform/argocd/validate_observability.py --ns-labeler-values "$NSL_VALUES" "$@"; }

step() { printf '\n=== %s ===\n' "$*"; }

step "1. CI 驗證，結果要跟 # expect 一致"
python - "$CASES" "$NSL_VALUES" <<'PY'
import glob, io, os, subprocess, sys
bad = 0
for f in sorted(glob.glob(os.path.join(sys.argv[1], "*.yaml"))):
    want = io.open(f, encoding="utf-8").readline().split(":", 1)[1].strip()
    rc = subprocess.run([sys.executable, "platform/argocd/validate_observability.py",
                         "--ns-labeler-values", sys.argv[2], f], capture_output=True).returncode
    got = "OK" if rc == 0 else "FAIL"
    mark = "✓" if got == want else "✗ 不符"
    bad += got != want
    print("  %-24s 預期 %-4s 實際 %-4s %s" % (os.path.basename(f), want, got, mark))
sys.exit(1 if bad else 0)
PY

step "2. 渲染 template"
chart=$(mktemp -d)
trap 'rm -rf "$chart"' EXIT
mkdir -p "$chart/templates"
printf 'apiVersion: v2\nname: render\nversion: 0.0.0\n' > "$chart/Chart.yaml"
cp "$TMPL" "$chart/patch.tmpl"
# ArgoCD 給 template 的參數是 map[string]interface{}，helm 的 .Values 不是，
# 先轉一手，不然 dig 會認不得。
echo '{{ tpl (.Files.Get "patch.tmpl") (.Values | toYaml | fromYaml) }}' > "$chart/templates/out.yaml"
# helm 是 Windows 程式時看不懂 Git Bash 的 /tmp/... 路徑，要轉成 C:/... 的寫法。
# （上面設了 MSYS_NO_PATHCONV=1，Git Bash 不會自動轉。）
chart_path=$(cygpath -m "$chart" 2>/dev/null || printf '%s' "$chart")

labels_of() {  # 印出渲染後貼到 namespace 的 label，一行一個 k=v
  helm template r "$chart_path" -f "$1" | python -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d:
        for k, v in d["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"].items():
            print("%s=%s" % (k, v))' | tr -d '\r'   # Windows 的 Python 行尾是 \r\n，\r 會黏在 label 值後面
}

for s in it imc both none typo plat; do
  f="$CASES/sim-$s.yaml"
  if validate "$f" >/dev/null 2>&1; then
    out=$(labels_of "$f")
    # 渲染失敗不能默默往下走，不然後面會拿空的 label 去貼
    [ -n "$out" ] || { echo "  sim-$s 渲染結果是空的，停止"; exit 1; }
    printf '  %-10s %s\n' "sim-$s" "$(printf '%s' "$out" | tr '\n' ' ')"
  else
    printf '  %-10s CI 會擋下，不會部署\n' "sim-$s"
  fi
done

step "3. 把 ns-labeler 的測試對照表拿掉，讓它不會碰到 zk-sim-*"
helm upgrade ns-labeler platform/cluster-addons/ns-labeler -n vm-monitoring \
  --set image.registry=docker.io --wait --timeout 60s >/dev/null
echo "ns-labeler 已還原成預設對照表"

step "4. 照 ArgoCD 會做的方式貼 label"
for s in it imc both none typo plat; do
  ns="zk-sim-$s-prod"
  # ArgoCD 建出來的 namespace 不會有 ns-labeler 的 annotation，一起清掉
  kubectl label ns "$ns" dept- group- system- >/dev/null 2>&1 || true
  kubectl annotate ns "$ns" observability.spil.com.tw/labeled-by- >/dev/null 2>&1 || true
  f="$CASES/sim-$s.yaml"
  if validate "$f" >/dev/null 2>&1; then
    # shellcheck disable=SC2046
    kubectl label ns "$ns" $(labels_of "$f") --overwrite >/dev/null
  fi
done
kubectl get ns -l 'kubernetes.io/metadata.name in (zk-sim-it-prod,zk-sim-imc-prod,zk-sim-both-prod,zk-sim-none-prod,zk-sim-typo-prod,zk-sim-plat-prod)' -L dept,group,system

step "5. 路由檢查（跟 tests/routing 同一套）"
python tests/routing/check.py --wait 150
