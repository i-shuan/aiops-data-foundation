#!/usr/bin/env bash
# 測試用：把 collector/helmchart 用 values-test.yaml 裝進（或更新到）namespace otel-collector。
# 設定檔從 collector/code 帶（--set-file），所以改了 repo 的設定再跑一次就會滾動更新（Pod 有 checksum/config）。
# 用法：e2e/k8s/deploy-collector.sh [helm 的額外參數…]
#   CONFIG_DIR=<目錄>：設定檔改從這個目錄拿（壓測改佇列大小時用；預設 collector/code）
#   KCTX=<kube context>（預設目前的）
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
# Windows 的 helm 看不懂 Git Bash 的 /c/... 路徑，檔案路徑都先轉成 C:/...
p() { cygpath -m "$1" 2>/dev/null || echo "$1"; }
cfg="$(p "${CONFIG_DIR:-${repo}/collector/code}")"
helm upgrade --install collector "$(p "${repo}/collector/helmchart")" -n otel-collector --create-namespace \
  ${KCTX:+--kube-context "${KCTX}"} \
  -f "$(p "${repo}/collector/helmchart/values-test.yaml")" \
  --set-file "config.common=${cfg}/common.yaml,config.node=${cfg}/node.yaml,config.aiops=${cfg}/aiops.yaml,config.cluster=${cfg}/cluster.yaml" \
  "$@" >/dev/null
