#!/usr/bin/env bash
# 測試用：把 elasticsearch/helmchart（測試用的單節點 ES）裝進（或更新到）namespace es-test。
# 用法：e2e/k8s/deploy-es.sh [helm 的額外參數…]      例如 --set twoNode.enabled=true（run-es-2node.sh）
#   KCTX=<kube context>（預設目前的）
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
# Windows 的 helm 看不懂 Git Bash 的 /c/... 路徑，先轉成 C:/...
p() { cygpath -m "$1" 2>/dev/null || echo "$1"; }
helm upgrade --install elasticsearch "$(p "${repo}/elasticsearch/helmchart")" -n es-test --create-namespace \
  ${KCTX:+--kube-context "${KCTX}"} "$@" >/dev/null
