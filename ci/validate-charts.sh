#!/usr/bin/env bash
# chart 規範檢查。不通過就擋下 merge。
#
# 規範本身寫在 app-common 的模板裡（tag 不准用 latest、要填 resources、
# 要有 probe、component 只能是那幾個值），所以這裡只要能成功渲染就代表符合規範。
#
# 用法：
#   ./ci/validate-charts.sh              檢查 charts/ 底下全部
#   ./ci/validate-charts.sh charts/yms-sim   只檢查指定的
set -euo pipefail

charts=("$@")
if [ ${#charts[@]} -eq 0 ]; then
  for d in charts/*/; do charts+=("${d%/}"); done
fi

status=0
for chart in "${charts[@]}"; do
  [ -f "$chart/Chart.yaml" ] || continue

  # library chart 不能單獨渲染，跳過。它的規範是透過引用它的 chart 被驗到的。
  if grep -q '^type:[[:space:]]*library' "$chart/Chart.yaml"; then
    echo "SKIP  $chart（library chart）"
    continue
  fi

  # --skip-refresh 很重要：相依是本地的 file:// 路徑，不需要去打外部 chart repo。
  # 少了它，每個 chart 都會卡在重新整理 repo 索引，CI 會變得很慢甚至逾時。
  helm dependency build "$chart" --skip-refresh >/dev/null 2>&1 || true
  if output=$(helm template ci-check "$chart" 2>&1 >/dev/null); then
    echo "OK    $chart"
  else
    echo "FAIL  $chart"
    echo "      ${output}"
    status=1
  fi
done
exit "$status"
