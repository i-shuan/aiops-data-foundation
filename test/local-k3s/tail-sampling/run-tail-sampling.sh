#!/usr/bin/env bash
# 尾端取樣（tail sampling）實測：兩層 Collector（node → loadbalancing 依 trace_id → trace-sampler × 2 → ES），
# 用 loadgen.py 模擬 89% 正常、11% 有問題（錯誤 4%、慢 3%、很慢 12 秒 1%、自己之後又做 12 秒 1%、吞掉例外 2%）的請求，
# 每一輪換 decision_wait、流量，看：有問題的留下多少、正常的留多少、trace 完不完整、trace-sampler 用多少 CPU 記憶體。
#
# 前提：run-e2e.sh、run-metrics-e2e.sh 跑過（ES、Collector、es-writer-keys）。每輪約 7 分鐘。
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/tail-sampling/run-tail-sampling.sh
#       ROUNDS="dw5:5s:200 dw10:10s:200"（名字:decision_wait:每秒請求數）、DURATION=300
#       跑完會把 Collector 換回原本的（不經 trace-sampler）；KEEP=1 不換回
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
export ES_AUTH="elastic:changeme-test-only"
ns=imc-wip-prod
DURATION="${DURATION:-300}"
ROUNDS="${ROUNDS:-dw5:5s:200 dw10:10s:200 dw10-500:10s:500}"
ts() { date +%H:%M:%S; }
metric() {  # 兩台 trace-sampler 某個內部計數的加總
  local sum=0 v
  for p in trace-sampler-0 trace-sampler-1; do
    v="$(kubectl get --raw "/api/v1/namespaces/otel-collector/pods/${p}:8888/proxy/metrics" | grep -E "^$1( |\{)" \
      | { [[ -n "${2:-}" ]] && grep -F "$2" || cat; } | awk '{s+=$NF} END {print s+0}')"
    sum=$(python3 -c "print(${sum}+${v})")
  done
  echo "${sum}"
}

echo "== 換成兩層：node Collector → trace-sampler × 2"
kubectl apply -k "${here}" >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
kubectl -n "${ns}" create configmap ts-loadgen --from-file="${here}/loadgen.py" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

for round in ${ROUNDS}; do
  IFS=: read -r name dw rate <<<"${round}"
  run="${name}-$(date +%H%M)"
  echo
  echo "== ${name}：decision_wait ${dw}、每秒 ${rate} 個請求、${DURATION} 秒（${run}）"
  kubectl -n otel-collector set env statefulset/trace-sampler DECISION_WAIT="${dw}" EXPECTED_TPS="${rate}" >/dev/null
  kubectl -n otel-collector rollout restart statefulset/trace-sampler >/dev/null   # 內部計數歸零
  kubectl -n otel-collector rollout status statefulset/trace-sampler --timeout=180s >/dev/null
  sleep 20   # node Collector 的 DNS 解析到新的兩台
  kubectl -n "${ns}" delete job ts-load --ignore-not-found --wait=true >/dev/null
  start=$(date +%s)
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata: {name: ts-load, namespace: ${ns}}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: loadgen
          image: docker.io/library/python:3.12-alpine
          command: ["python3", "-u", "/lg/loadgen.py"]
          env: [{name: RATE, value: "${rate}"}, {name: DURATION, value: "${DURATION}"}, {name: RUN, value: "${run}"}]
          resources: {requests: {cpu: 500m}}
          volumeMounts: [{name: lg, mountPath: /lg}]
      volumes: [{name: lg, configMap: {name: ts-loadgen}}]
EOF
  kubectl -n "${ns}" wait --for=condition=complete job/ts-load --timeout=$((DURATION + 300))s >/dev/null
  end=$(date +%s)
  sent="$(kubectl -n "${ns}" logs job/ts-load 2>/dev/null | grep '^SENT ' | sed 's/^SENT //' || true)"
  if [[ -z "${sent}" ]]; then   # kubectl logs 在這台測試機上不可靠，直接讀節點上的檔
    sent="$(cat /var/log/pods/${ns}_ts-load-*/loadgen/*.log | grep -o 'SENT .*' | tail -1 | sed 's/^SENT //')"
  fi
  echo "   $(ts) 送完，等 ${dw} ＋ 40 秒讓最後的 trace 決定、寫進 ES"
  sleep $(( ${dw%s} + 40 ))
  got=$(metric otelcol_processor_tail_sampling_new_trace_id_received)
  kept=$(metric otelcol_processor_tail_sampling_global_count_traces_sampled 'sampled="true"')
  early=$(metric otelcol_processor_tail_sampling_sampling_trace_dropped_too_early)
  late=$(metric otelcol_processor_tail_sampling_sampling_late_span_age_count)
  internal="{\"收到的 trace\": ${got}, \"留下\": ${kept}, \"來不及決定就被擠掉\": ${early}, \"晚到的段\": ${late}}"
  curl -sS -u "${ES_AUTH}" -X POST "${ES_URL}/traces-*,metrics-*/_refresh" >/dev/null
  python3 "${here}/measure.py" "${run}" "${start}" "${end}" "${sent}" "${internal}"
done

kubectl -n "${ns}" delete job ts-load --ignore-not-found >/dev/null
if [[ -z "${KEEP:-}" ]]; then
  echo
  echo "== 換回原本的 Collector（不經 trace-sampler）"
  kubectl -n otel-collector delete statefulset trace-sampler --ignore-not-found >/dev/null
  kubectl -n otel-collector delete service trace-sampler-headless --ignore-not-found >/dev/null
  kubectl apply -k "${here}/../otel-collector" >/dev/null
  kubectl -n otel-collector rollout restart daemonset/otel-node-collector >/dev/null
  kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
fi
