#!/usr/bin/env bash
# 在每個應用 repo 的 CI 裡跑，不通過就擋下 merge。
# 規範檢查都寫在 app-common 模板裡，所以這裡只要能成功渲染就代表符合規範。
#
# 用法：./validate-charts.sh <chart 目錄> [<chart 目錄> ...]
set -euo pipefail

status=0
for chart in "$@"; do
  helm dependency build "$chart" >/dev/null
  if output=$(helm template ci-check "$chart" 2>&1 >/dev/null); then
    echo "OK    $chart"
  else
    echo "FAIL  $chart"
    echo "      ${output}"
    status=1
  fi
done
exit "$status"
