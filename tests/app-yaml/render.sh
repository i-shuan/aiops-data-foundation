#!/usr/bin/env bash
# 把一份 app.yaml 渲染成 namespace 會得到的 label，一行一個 k=v。
#
#   ./tests/app-yaml/render.sh tests/app-yaml/cases/ok-it.yaml
#
# 用的是正式的 platform/argocd/namespace-metadata.tmpl，渲染引擎是 helm。
# helm 和 ArgoCD ApplicationSet 都是 Go template + sprig，這裡用到的 dict / dig / get / default
# 行為一樣，但這不是 ArgoCD 本身。
#
# tests/routing 也用這支，所以 E2E 貼到 namespace 上的 label 跟這裡驗的是同一份結果。
set -euo pipefail
# helm 的輸出有中文註解。Windows 的 Python 預設用系統編碼讀 stdin，不設的話會解成亂碼後炸掉。
export PYTHONIOENCODING=utf-8

here=$(cd "$(dirname "$0")" && pwd)
# 可以用 NAMESPACE_METADATA_TMPL 指到改壞的複本，驗證測試抓得到錯
tmpl="${NAMESPACE_METADATA_TMPL:-$here/../../platform/argocd/namespace-metadata.tmpl}"
app_yaml=$1

chart=$(mktemp -d)
trap 'rm -rf "$chart"' EXIT
mkdir -p "$chart/templates"
printf 'apiVersion: v2\nname: render\nversion: 0.0.0\n' > "$chart/Chart.yaml"
cp "$tmpl" "$chart/patch.tmpl"
# ArgoCD 給 template 的參數是 map[string]interface{}，helm 的 .Values 不是，
# 先轉一手，不然 dig 會認不得。
echo '{{ tpl (.Files.Get "patch.tmpl") (.Values | toYaml | fromYaml) }}' > "$chart/templates/out.yaml"

# helm 是 Windows 程式時看不懂 Git Bash 的 /tmp/... 路徑，要轉成 C:/... 的寫法
to_native() { cygpath -m "$1" 2>/dev/null || printf '%s' "$1"; }

helm template r "$(to_native "$chart")" -f "$(to_native "$app_yaml")" | python -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d:
        for k, v in d["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"].items():
            print("%s=%s" % (k, v))' | tr -d '\r'   # Windows 的 Python 行尾是 \r\n，\r 會黏在 label 值後面
