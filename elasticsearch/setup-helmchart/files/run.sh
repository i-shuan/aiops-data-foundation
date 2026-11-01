#!/usr/bin/env bash
# es-setup Job 跑的：解開 elasticsearch/code/、跑 elasticsearch-setup.sh（key 不印出來，寫成檔），再把 key 寫進 Secret。
# 環境變數由 templates/job.yaml 給：ES_URL、ES_AUTH、SKIP_KEYS_MODE、SNAPSHOT_*、RELEASE_NAMESPACE、KEYS_SECRET、TARGETS
set -euo pipefail
work="$(mktemp -d)"
keys="$(mktemp -d)"
trap 'rm -rf "${work}" "${keys}"' EXIT
tar xzf /bundle/code.tgz -C "${work}"   # ConfigMap 的 binaryData 掛出來已經是解碼後的 tgz
# 在 Windows 上打包的話檔案是 CRLF 換行，bash 讀 .sh、.conf 會報 $'\r'：去掉（Linux 的 CI checkout 本來就是 LF）
find "${work}" -type f \( -name '*.sh' -o -name '*.conf' \) -exec sed -i 's/\r$//' {} +
setup="${work}/code/scripts/elasticsearch-setup.sh"
[[ -f "${setup}" ]] || { echo "bundle 裡沒有 code/scripts/elasticsearch-setup.sh" >&2; exit 1; }

if [[ -n "${SKIP_KEYS_MODE}" ]]; then
  echo "=== 只更新結構（skipKeys），不發新 key、不碰 Secret ==="
  SKIP_KEYS=1 bash "${setup}"
  exit 0
fi

KEYS_OUT_DIR="${keys}" bash "${setup}"

# server-side apply 只管 key 那個欄位：Secret 的其他欄位（例如 grafana-env 的 ES_URL、webhook）不會被蓋掉。不存 annotation
apply_secret() {  # apply_secret <namespace> <secret> <--from-file=…>…
  local ns="$1" name="$2"; shift 2
  kubectl -n "${ns}" create secret generic "${name}" "$@" --dry-run=client -o yaml \
    | kubectl apply --server-side --force-conflicts --field-manager=es-setup -f - >/dev/null
}

apply_secret "${RELEASE_NAMESPACE}" "${KEYS_SECRET}" \
  --from-file=writer="${keys}/writer" --from-file=reader="${keys}/reader" --from-file=engine="${keys}/engine" \
  --from-file=grafana="${keys}/grafana" --from-file=probe="${keys}/probe"
echo "=== 5 把 key 寫進 ${RELEASE_NAMESPACE}/${KEYS_SECRET}（不印出）==="

while read -r ns secret field from; do
  [[ -n "${ns}" ]] || continue
  apply_secret "${ns}" "${secret}" --from-file="${field}=${keys}/${from}"
  echo "=== ${from} key 寫進 ${ns}/${secret} 的 ${field} ==="
done <<< "${TARGETS}"
