#!/usr/bin/env bash
# 本機 k3d cluster（docs/pdca/2026-10-k3d-e2e.md）：單節點、沒有 loadbalancer、traefik、servicelb。
# 這個環境（Firecracker VM、沒有 CAP_SYS_RESOURCE、有 TLS 檢查的 proxy）要兩個特別處理：
#   1. containerd 設 restrict_oom_score_adj：不然 kubelet 把 Pod 的 oom_score_adj 調成 -998 會被拒（runc: failed to update
#      /proc/self/oom_score_adj: Permission denied），所有 Pod 都起不來
#   2. 節點拉不到 image（proxy 換憑證，節點不信任）：先在主機 docker 拉好、用 load-images.sh 匯進節點
# 用法：e2e/local-k3d/create-cluster.sh        （需要 docker、k3d、kubectl；k3d 下載後請核對 checksums.txt）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
name="${K3D_NAME:-aiops}"
tmp="$(mktemp -d)"; trap 'rm -rf "${tmp}"' EXIT

k3d cluster delete "${name}" >/dev/null 2>&1 || true
# 第一次先起一個預設的，讓 k3s 產生 containerd 設定，複製出來加一行再重建（k3s 的 config.toml.tmpl 重複寫同一個 table 會壞）
k3d cluster create "${name}" --servers 1 --agents 0 --no-lb --wait --timeout 300s \
  --k3s-arg "--disable=traefik@server:0" --k3s-arg "--disable=servicelb@server:0" >/dev/null
docker cp "k3d-${name}-server-0:/var/lib/rancher/k3s/agent/etc/containerd/config.toml" "${tmp}/config.toml"
sed '/^\[plugins."io.containerd.grpc.v1.cri"\]$/a\  restrict_oom_score_adj = true' "${tmp}/config.toml" > "${here}/containerd-config.toml.tmpl"
grep -q restrict_oom_score_adj "${here}/containerd-config.toml.tmpl"
k3d cluster delete "${name}" >/dev/null
k3d cluster create "${name}" --servers 1 --agents 0 --no-lb --wait --timeout 300s \
  --k3s-arg "--disable=traefik@server:0" --k3s-arg "--disable=servicelb@server:0" \
  --volume "${here}/containerd-config.toml.tmpl:/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl@server:0"
k3d kubeconfig get "${name}" > "${KUBECONFIG:-${here}/kubeconfig}"
"${here}/load-images.sh" rancher/mirrored-pause:3.6 rancher/mirrored-coredns-coredns:1.12.0 rancher/local-path-provisioner:v0.0.30 rancher/mirrored-metrics-server:v0.7.2 rancher/mirrored-library-busybox:1.36.1   # 最後一個是 local-path 建 PVC 的 helper
echo "cluster ${name} 好了：export KUBECONFIG=${KUBECONFIG:-${here}/kubeconfig}"
