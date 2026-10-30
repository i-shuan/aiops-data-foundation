#!/usr/bin/env bash
# k3d（主機 docker 的 ES）：ES 停機（docker stop）、沒回應（docker pause）時 Collector 會不會掉資料（docs/pdca/meta/2026-10-collector-es-exporter.md）。
# 沿用 e2e/local-k3s/load/run-durability.sh 的產生器、計數、分析函式；只換掉 ES 起停的方法。
# 用法：ES_CONTAINER=es8 ES_URL=http://localhost:9201 e2e/local-k3d/run-es-outage.sh normal stop pause
# 前提：create-cluster.sh、deploy-collectors.sh 跑過；節點上要有 python:3.12-alpine、busybox:1.37（load-images.sh），
#       而且 kubelet 會回收沒在用的 image，先起一個佔住 image 的 Pod（見 PDCA）。
set -euo pipefail
here_k3d="$(cd "$(dirname "$0")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-${here_k3d}/kubeconfig}"
here="${here_k3d}/../local-k3s/load"
export ES_URL="${ES_URL:-http://localhost:9201}" PF_PORT="" NODE_EXEC="docker exec -i k3d-aiops-server-0" LOG_RATE=200 TRACE_RATE=20 STABLE=60 RESULTS=""
# 借函式：從 OC= 到 sc_es_down 之前
eval "$(sed -n '/^OC=otel-collector/,/^sc_es_down()/p' "${here}/run-durability.sh" | sed '$d')"
A=elastic:changeme-test-only
es_down() { docker stop "${ES_CONTAINER:-es8}" >/dev/null; }
es_pause() { docker pause "${ES_CONTAINER:-es8}" >/dev/null; }
es_up() { docker start "${ES_CONTAINER:-es8}" >/dev/null 2>&1 || true; docker unpause "${ES_CONTAINER:-es8}" >/dev/null 2>&1 || true
  until [[ "$(curl -s -u "$A" "${ES_URL}/_cluster/health" | jq -r .status 2>/dev/null)" =~ green|yellow ]]; do sleep 2; done; }
SUMMARY=(); RESTORE=()
sc_normal() { begin normal 120; sleep 5; T0="$(epoch)"; T0_LABEL="開始"; finish "正常 120 秒"; }
sc_stop() { local d="${1:-180}"; begin "es-stop" $(( 60 + d + 120 )); sleep 60; fault "ES 停 ${d} 秒（docker stop）"; es_down; sleep "${d}"; es_up; mark "ES 起來"; finish "ES 停機 ${d} 秒"; }
sc_pause() { local d="${1:-90}"; begin "es-pause" $(( 60 + d + 120 )); sleep 60; fault "ES 沒回應 ${d} 秒（docker pause）"; es_pause; sleep "${d}"; es_up; mark "ES 恢復回應"; finish "ES 沒回應 ${d} 秒"; }
kubectl apply -f "${here}/generator.yaml" >/dev/null
for sc in "$@"; do case "$sc" in normal) sc_normal;; stop) sc_stop 180;; pause) sc_pause 90;; esac; done
echo; echo "情境	log 遺失/重複	trace 遺失/不完整/重複/應用端丟	metrics 最大間隔/重複	備註"; printf '%s\n' "${SUMMARY[@]}"
