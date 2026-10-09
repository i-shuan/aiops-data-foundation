#!/usr/bin/env bash
# 去重複測試：強迫 Fluent Bit 重送已寫入的資料，確認 generate_id 能擋掉重複，而且重試會停下來。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/load/run-dedup.sh [速率]（預設 5000）
# 跑完會換回正式設定。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
rate="${1:-5000}"
src="${here}/../../../platform/cluster-addons/fluent-bit/fluent-bit.yaml"
dst="${here}/../fluent-bit-dedup/fluent-bit.yaml"

# 正式設定 → 回應上限改回 512KB；generate_id 沒開的話補上
sed -e 's/^      buffer_size: false$/      buffer_size: 512KB/' "${src}" > "${dst}"
grep -q 'generate_id: on' "${dst}" || sed -i 's/^\(      write_operation: create.*\)$/\1\n      generate_id: on/' "${dst}"
echo "測試設定：$(grep -c 'buffer_size: 512KB' "${dst}") 條出口 buffer_size 512KB，$(grep -c 'generate_id: on' "${dst}") 條出口 generate_id on"

restore() {
  echo "換回正式設定"
  kubectl apply -k "${here}/../fluent-bit" >/dev/null
  kubectl -n logging rollout status daemonset/fluent-bit --timeout=180s >/dev/null
}
trap restore EXIT

kubectl apply -k "${here}/../fluent-bit-dedup" >/dev/null
kubectl -n logging rollout status daemonset/fluent-bit --timeout=180s >/dev/null
"${here}/run-load.sh" "${rate}"

echo; echo "Fluent Bit 有沒有遇到「讀不完回應」而重送："
grep -h 'cannot increase buffer' /var/log/pods/logging_fluent-bit-*/fluent-bit/*.log 2>/dev/null | wc -l
