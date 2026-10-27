#!/usr/bin/env bash
# 把主機 docker 的 image 匯進 k3d 節點。單一平台（amd64）匯出：docker 29 的 containerd image store 預設匯出多平台索引，
# 沒拉的平台缺 blob，`k3d image import` 會失敗（content digest … not found）。
# 用法：e2e/local-k3d/load-images.sh <image> [<image>...]
set -euo pipefail
node="k3d-${K3D_NAME:-aiops}-server-0"
tar="$(mktemp)"; trap 'rm -f "${tar}"' EXIT
for img in "$@"; do
  docker pull -q --platform linux/amd64 "${img}" >/dev/null 2>&1 || true
  docker save --platform linux/amd64 "${img}" -o "${tar}"
  docker cp "${tar}" "${node}:/tmp/img.tar"
  docker exec "${node}" ctr --address /run/k3s/containerd/containerd.sock -n k8s.io images import --no-unpack /tmp/img.tar >/dev/null
  docker exec "${node}" rm -f /tmp/img.tar
  echo "loaded ${img}"
done
