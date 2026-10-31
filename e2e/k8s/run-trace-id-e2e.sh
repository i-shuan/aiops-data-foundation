#!/usr/bin/env bash
# log 的 trace.id／span.id 經過真的 Collector 後對不對得到 trace（docs/pdca/phase1-collect/2026-10-log-trace-bind.md）。
# _simulate 測不到、要 Collector 才測得到的：超過 16KB 的一行被 containerd 切段後接回來、檔案和 OTLP 同時開的兩份、
# 以及 log 的 trace.id 用 trace_id（alias）真的查得到同一個 trace。格式、值的案例在 e2e/pipeline/run-trace-id-cases.sh。
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector、it-mes-prod 都在）；cluster 共用時先 cluster_lock（cluster-lock.sh）。
# 用法：ES_URL=http://127.0.0.1:<port> e2e/k8s/run-trace-id-e2e.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }

echo "== 1. 中央 pipeline（這個分支的）與測試應用"
ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" SKIP_KEYS=1 "${repo}/elasticsearch/code/scripts/elasticsearch-setup.sh" >/dev/null
tr -d '\r' < "${here}/logtrace-app.sh" > "${TMPDIR:-/tmp}/logtrace-app.sh"
kubectl -n it-mes-prod create configmap logtrace-app --from-file=run.sh="${TMPDIR:-/tmp}/logtrace-app.sh" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f - >/dev/null <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: {name: logtrace-app, namespace: it-mes-prod}
spec:
  replicas: 1
  selector: {matchLabels: {app: logtrace-app}}
  template:
    metadata:
      labels: {app: logtrace-app, app.kubernetes.io/name: logtrace-app}
    spec:
      containers:
        - name: app
          image: docker.io/curlimages/curl:8.10.1
          command: ["sh", "/script/run.sh"]
          volumeMounts: [{name: script, mountPath: /script}]
      volumes: [{name: script, configMap: {name: logtrace-app}}]
EOF
kubectl -n it-mes-prod rollout restart deployment/logtrace-app >/dev/null
kubectl -n it-mes-prod rollout status deployment/logtrace-app --timeout=180s >/dev/null
start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "   只看 ${start} 之後的資料，等 60 秒"
sleep 60
es -X POST "${ES_URL}/traces-*.otel-*,k8s-*/_refresh" >/dev/null

# 挑一個 30 秒前的請求（它的 log 一定都到了）
since="{\"range\":{\"@timestamp\":{\"gte\":\"${start}\"}}}"
span="$(es "${ES_URL}/traces-generic.otel-it/_search" -d "{\"size\":1,\"sort\":[{\"@timestamp\":\"desc\"}],
  \"query\":{\"bool\":{\"filter\":[${since},{\"range\":{\"@timestamp\":{\"lte\":\"now-30s\"}}},
    {\"term\":{\"resource.attributes.service.name\":\"logtrace-app\"}}]}}}" | jq '.hits.hits[0]._source')"
tid="$(jq -r .trace_id <<<"${span}")"; sid="$(jq -r .span_id <<<"${span}")"
echo "   trace_id=${tid} span_id=${sid}"
[[ "${tid}" != null ]] || { echo "  FAIL  traces 裡沒有 logtrace-app 的 span"; exit 1; }
logs() {  # logs <case> → 這個請求這個 case 的 log（_source 陣列）
  es "${ES_URL}/k8s-it-local/_search" -d "{\"size\":10,\"query\":{\"bool\":{\"filter\":[
    {\"term\":{\"app.case\":\"$1\"}},{\"term\":{\"app.tid\":\"${tid}\"}}]}}}" | jq '[.hits.hits[]._source]'
}

echo "== 2. 大寫的 trace_id、span_id → 轉小寫，對得到 trace"
d="$(logs upper)"
check "一筆"                     "$(jq length <<<"${d}")" 1
check "trace.id ＝ trace 的 trace_id" "$(jq -r '.[0].trace.id' <<<"${d}")" "${tid}"
check "span.id ＝ span 的 span_id"    "$(jq -r '.[0].span.id' <<<"${d}")" "${sid}"

echo "== 3. 超過 16KB 的一行（containerd 切段）→ Collector 接回一筆，trace 在最後面也拿得到"
d="$(logs long)"
check "一筆（沒被切成好幾筆）"   "$(jq length <<<"${d}")" 1
check "filler 完整 20000 字"     "$(jq -r '.[0].app.filler | length' <<<"${d}")" 20000
check "trace.id"                 "$(jq -r '.[0].trace.id' <<<"${d}")" "${tid}"

echo "== 4. 格式錯的 trace_id → 不設，原值留在 app.trace_id_invalid"
d="$(logs bad)"
check "沒有 trace.id"            "$(jq -r '.[0].trace.id // "none"' <<<"${d}")" none
check "app.trace_id_invalid"     "$(jq -r '.[0].app.trace_id_invalid' <<<"${d}")" "${tid}zz"

echo "== 5. 背景工作（沒有 trace_id）→ 不被塞 trace_id"
d="$(logs background)"
check "一筆、沒有 trace.id"      "$(jq -r '"\(length) \(.[0].trace.id // "none")"' <<<"${d}")" "1 none"

echo "== 6. 檔案和 OTLP 同時開 → 兩份，log.source 分得出來，兩份都對得到 trace"
d="$(logs dual)"
check "log.source"               "$(jq -r '[.[].log.source] | sort | join(",")' <<<"${d}")" file,otlp
check "兩份的 trace.id"          "$(jq -r '[.[].trace.id] | unique | join(",")' <<<"${d}")" "${tid}"
check "兩份的 message 一樣"      "$(jq -r '[.[].message] | unique | join(",")' <<<"${d}")" "lot handled"

echo "== 7. log → trace、trace → log 用同名欄位（alias trace_id、span_id）查"
n_log="$(es "${ES_URL}/k8s-*/_count" -d "{\"query\":{\"term\":{\"trace_id\":\"${tid}\"}}}" | jq -r .count)"
check "log 用 trace_id 查到 4 筆（upper、long、dual×2）" "${n_log}" 4
n_span="$(es "${ES_URL}/k8s-*/_count" -d "{\"query\":{\"term\":{\"span_id\":\"${sid}\"}}}" | jq -r .count)"
check "log 用 span_id 查到 4 筆"  "${n_span}" 4
check "trace 用 trace_id 查到那段 span" \
  "$(es "${ES_URL}/traces-*.otel-*/_count" -d "{\"query\":{\"term\":{\"trace_id\":\"${tid}\"}}}" | jq -r .count)" 1

kubectl -n it-mes-prod delete deployment/logtrace-app configmap/logtrace-app >/dev/null
if [[ "${fail}" != 0 ]]; then echo "有項目失敗"; exit 1; fi
echo "全部通過"
