#!/usr/bin/env bash
# k3d（ES 在主機 docker）跑完整的收值線故障測試：就是 e2e/local-k3s/load/run-durability.sh，只換掉「ES 怎麼起停」和「怎麼進節點」。
# 用法：e2e/local-k3d/run-durability.sh node-kill es-429 …（情境清單見 run-durability.sh 開頭）
# 前提：create-cluster.sh、deploy-collectors.sh 跑過；ES 容器名 ES_CONTAINER（預設 es8）、對外 port ES_URL（預設 localhost:9201）；
#       節點上要有 python:3.12-alpine、busybox:1.37（load-images.sh）；rolling 情境要先 load-images.sh otel/opentelemetry-collector-contrib:<TO_VERSION>。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-${here}/kubeconfig}"
export ES_DOCKER="${ES_CONTAINER:-es8}" ES_URL="${ES_URL:-http://localhost:9201}"
export NODE_EXEC="${NODE_EXEC:-docker exec -i k3d-${K3D_NAME:-aiops}-server-0}"
exec bash "${here}/../local-k3s/load/run-durability.sh" "$@"
