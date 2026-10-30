#!/usr/bin/env bash
# 多節點實測：給 create-cluster.sh 建的單節點 cluster 加 agent 節點（預設 2 個，容器名 k3d-agent-0、k3d-agent-1，節點名同容器名）。
# 跟 server 一樣的兩個特別處理：containerd 的 restrict_oom_score_adj、匯入 image；另外 kubelet 的磁碟回收門檻放寬到 1Gi
# （主機磁碟用量接近 90% 時，預設門檻會觸發 DiskPressure，Pod 被驅逐）。
# 用法：e2e/local-k3d/add-nodes.sh [節點數]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
name="${K3D_NAME:-aiops}"; n="${1:-2}"
export KUBECONFIG="${KUBECONFIG:-${here}/kubeconfig}"
tmp="$(mktemp -d)"; trap 'rm -rf "${tmp}"' EXIT
printf 'kubelet-arg:\n  - "eviction-hard=nodefs.available<1Gi,imagefs.available<1Gi"\n' > "${tmp}/config.yaml"
k3d node create agent --cluster "${name}" --role agent --replicas "${n}" --wait --timeout 300s \
  --volume "${here}/containerd-config.toml.tmpl:/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl" \
  --volume "${tmp}/config.yaml:/etc/rancher/k3s/config.yaml"
"${here}/load-images.sh" rancher/mirrored-pause:3.6 rancher/mirrored-library-busybox:1.36.1 docker.io/library/python:3.12-alpine docker.io/library/busybox:1.37 \
  otel/opentelemetry-collector-contrib:0.162.0
kubectl get nodes
