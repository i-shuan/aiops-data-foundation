#!/usr/bin/env bash
# 把平台的 Collector（telemetry/collectors）部署到 k3d：ES 在主機的 docker，接到 k3d 的網路後用它的 IP。
# 前提：create-cluster.sh 跑過；主機上有測試 ES（容器名 ES_CONTAINER，預設 es-t）；寫入 key 檔（elasticsearch-setup.sh 發的 es-writer，一行 key）。
# 用法：ES_WRITER_KEY_FILE=<檔> e2e/local-k3d/deploy-collectors.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
es="${ES_CONTAINER:-es-t}"; net="k3d-${K3D_NAME:-aiops}"
: "${ES_WRITER_KEY_FILE:?請設定 ES_WRITER_KEY_FILE}"
docker network connect "${net}" "${es}" 2>/dev/null || true
ip="$(docker inspect "${es}" --format "{{(index .NetworkSettings.Networks \"${net}\").IPAddress}}")"
"${here}/load-images.sh" otel/opentelemetry-collector-contrib:0.162.0 >/dev/null
rm -rf "${here}/.run"; cp -r "${here}/otel-collector" "${here}/.run"
sed -i "s/ES_HOST/${ip}/" "${here}/.run/kustomization.yaml"
kubectl create namespace otel-collector --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n otel-collector create secret generic es-writer-keys --from-file=ES_WRITER_API_KEY="${ES_WRITER_KEY_FILE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -k "${here}/.run"
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s
echo "Collector 好了（ES ${ip}）。trace-sampler 的 PVC 要 local-path 的 helper image：create-cluster.sh 已經匯入"
