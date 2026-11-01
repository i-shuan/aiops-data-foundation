#!/usr/bin/env bash
# 把 repo 裡的外掛 zip（git lfs）帶進 cluster：切成每段不到 1MB 的 ConfigMap，chart 的 initContainer 接回、核對 SHA-256、解開。
# ConfigMap、Secret、Helm release 單一物件都有 1MB 上限，10MB 的 zip 只能分段；Pod 不用連 Nexus（docs/pdca/phase5-learning-loop/2026-11-grafana-plugin-lfs.md）。
#
# 用法：grafana/helmchart/load-plugin.sh [-n <namespace>] [zip 路徑]
#   zip 路徑預設是這支腳本旁邊的 ../plugins/volkovlabs-form-panel-6.3.1.zip（相對路徑，從哪裡執行都一樣）
#   KCTX=<kube context>（預設目前的）
# 印出 helm 要加的參數，例如：
#   --set plugin.source=configmap --set plugin.parts=11
# 再跑：helm upgrade --install grafana grafana/helmchart -n <namespace> … <上面那串>
#
# zip 還是 git lfs 的指標檔（沒裝 git lfs、或 clone 時沒拉）會停下來，不會把指標檔當外掛送進去。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ns=grafana
if [[ "${1:-}" == "-n" ]]; then ns="$2"; shift 2; fi
zip="${1:-${here}/../plugins/volkovlabs-form-panel-6.3.1.zip}"
part_size=$((900 * 1024))    # 留一點給 ConfigMap 本身的欄位，整個物件不超過 1MB
k() { kubectl ${KCTX:+--context "${KCTX}"} "$@"; }

[[ -f "${zip}" ]] || { echo "找不到 ${zip}" >&2; exit 1; }
if head -c 200 "${zip}" | grep -q "git-lfs.github.com/spec"; then
  echo "${zip} 是 git lfs 的指標檔，不是 zip：先 git lfs install && git lfs pull" >&2; exit 1
fi
sha="$(sha256sum "${zip}" | cut -d' ' -f1)"
want="$(sed -n 's/^  sha256: *\([0-9a-f]\{64\}\).*/\1/p' "${here}/values.yaml" | head -1)"
if [[ -n "${want}" && "${sha}" != "${want}" ]]; then
  echo "注意：zip 的 SHA-256 是 ${sha}，跟 values.yaml 的 plugin.sha256（${want}）不同；initContainer 會照 values 核對、不符就不裝" >&2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
split -b "${part_size}" -d -a 3 "${zip}" "${tmp}/part-"
parts=("${tmp}"/part-*)
n=${#parts[@]}
k create namespace "${ns}" --dry-run=client -o yaml | k apply -f - >/dev/null
for i in $(seq 0 $((n - 1))); do
  f="${parts[$i]}"
  # Windows 的 kubectl 看不懂 Git Bash 的 /tmp/…
  src="$(cygpath -m "${f}" 2>/dev/null || echo "${f}")"
  k -n "${ns}" create configmap "grafana-plugin-zip-${i}" --from-file="part=${src}" --dry-run=client -o yaml \
    | k label --local -f - app.kubernetes.io/part-of=grafana-plugin-zip --dry-run=client -o yaml \
    | k apply --server-side --force-conflicts --field-manager=load-plugin -f - >/dev/null   # client-side apply 會把整份存進 annotation（上限 256KB）
done
# 上一次段數比較多的話，多出來的刪掉（不然 parts 設錯時會接到舊的）
for cm in $(k -n "${ns}" get configmap -l app.kubernetes.io/part-of=grafana-plugin-zip -o name); do
  idx="${cm##*-}"
  (( idx >= n )) && k -n "${ns}" delete "${cm}" >/dev/null
done
echo "已建 ${n} 個 ConfigMap（grafana-plugin-zip-0…$((n - 1))，namespace ${ns}），zip SHA-256 ${sha}" >&2
echo "--set plugin.source=configmap --set plugin.parts=${n}"
