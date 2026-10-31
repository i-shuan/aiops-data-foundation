#!/usr/bin/env bash
# 測試用：把 elasticsearch/helmchart（測試用的單節點 ES）裝進（或更新到）namespace es-test。
# 用法：e2e/k8s/deploy-es.sh [helm 的額外參數…]      例如 --set twoNode.enabled=true（run-es-2node.sh）
#   KCTX=<kube context>（預設目前的）
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
helm upgrade --install elasticsearch "${repo}/elasticsearch/helmchart" -n es-test --create-namespace \
  ${KCTX:+--kube-context "${KCTX}"} "$@" >/dev/null
