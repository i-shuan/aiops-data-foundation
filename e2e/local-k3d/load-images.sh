#!/usr/bin/env bash
# 把主機 docker 的 image 匯進 k3d 的所有節點。單一平台（amd64）匯出：docker 29 的 containerd image store 預設匯出多平台索引，
# 沒拉的平台缺 blob，`k3d image import` 會失敗（content digest … not found）。
# 用法：e2e/local-k3d/load-images.sh <image> [<image>...]
set -euo pipefail
# 匯進 cluster 的每個節點（server 和 add-nodes.sh 加的 agent）
mapfile -t nodes < <(docker ps --filter "label=k3d.cluster=${K3D_NAME:-aiops}" --format '{{.Label "k3d.role"}} {{.Names}}' | awk '$1=="server"||$1=="agent"{print $2}')
tar="$(mktemp)"; trap 'rm -f "${tar}"' EXIT
for img in "$@"; do
  docker pull -q --platform linux/amd64 "${img}" >/dev/null 2>&1 || true
  docker save --platform linux/amd64 "${img}" -o "${tar}"
  for node in "${nodes[@]}"; do
    docker cp "${tar}" "${node}:/tmp/img.tar"
    docker exec "${node}" ctr --address /run/k3s/containerd/containerd.sock -n k8s.io images import --no-unpack /tmp/img.tar >/dev/null
    docker exec "${node}" rm -f /tmp/img.tar
  done
  echo "loaded ${img}（${#nodes[@]} 個節點）"
done
