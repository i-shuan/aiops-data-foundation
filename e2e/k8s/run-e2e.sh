#!/usr/bin/env bash
# log 路線端到端驗證：OpenTelemetry Collector → 分流 → ES pipeline → data stream，外加 API key 權限。
#
# 前提：已有一個可用的 K8s（KUBECONFIG 指向它），可以拉 docker.io 的 image。
# 用法：e2e/k8s/run-e2e.sh
#       已經連得到測試 ES（例如直接打 ClusterIP）時：ES_URL=http://<ip>:9200 e2e/k8s/run-e2e.sh
# 會建立 namespace es-test、otel-collector、it-mes-prod、imc-wip-prod、legacy。只能在測試 cluster 跑。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
ES_PASSWORD=changeme-test-only
ES_LOCAL_PORT="${ES_LOCAL_PORT:-19200}"
WAIT_SECONDS="${WAIT_SECONDS:-60}"

fail=0
check() {  # check <說明> <實際> <預期>
  if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi
}
check_gt0() {  # check_gt0 <說明> <數字>
  if [[ "$2" -gt 0 ]]; then echo "  PASS  $1（$2）"; else echo "  FAIL  $1：得到 $2"; fail=1; fi
}
es() { curl -sS -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
count() {  # count <index> <query json>
  es "${ES_URL}/$1/_count" -d "{\"query\": $2}" | jq -r '.count // 0'
}

echo "== 1. Elasticsearch"
"${here}/deploy-es.sh"
kubectl -n es-test rollout status statefulset/elasticsearch --timeout=300s
if [[ -z "${ES_URL:-}" ]]; then
  ES_URL="http://localhost:${ES_LOCAL_PORT}"
  kubectl -n es-test port-forward svc/elasticsearch "${ES_LOCAL_PORT}:9200" >/dev/null 2>&1 &
  pf=$!
  trap 'kill ${pf} 2>/dev/null || true' EXIT
fi
until es -o /dev/null -m 5 "${ES_URL}" 2>/dev/null; do sleep 1; done
until [[ "$(es -m 5 "${ES_URL}/_cluster/health" | jq -r .status)" =~ green|yellow ]]; do sleep 2; done

echo "== 2. index 結構與 API key（正式腳本）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/elasticsearch/code/scripts/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
WRITER_KEY="$(key_of '寫入 key：es-writer')"
AIOPS_KEY="$(key_of 'AIOps 唯讀 key')"
PROBE_KEY="$(key_of '健康檢查 key')"
for k in "${WRITER_KEY}" "${AIOPS_KEY}" "${PROBE_KEY}"; do
  [[ -n "${k}" && "${k}" != null ]] || { echo "沒拿到 API key，elasticsearch-setup.sh 的輸出："; echo "${setup_out}"; exit 1; }
done

kubectl create namespace otel-collector --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY="${WRITER_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# ES 的 readinessProbe 改用這把 key 打 GET /（elasticsearch.yaml）：ES 剛起來 API key 還不能用的那 1 秒多不會被導流量
kubectl -n es-test create secret generic es-probe-key --from-literal=PROBE_ES_API_KEY="${PROBE_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "== 3. OpenTelemetry Collector 與測試應用"
"${here}/deploy-collector.sh"
# 情境測試會幫 legacy 標上 dept，這裡拿掉，恢復成「沒標 dept」的情境
kubectl label namespace legacy dept- >/dev/null 2>&1 || true
# 每次跑都換新的 key，Collector 要重啟才會讀到
kubectl -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s
kubectl -n otel-collector rollout status deployment/otel-cluster-collector --timeout=180s
# 測試 ES 有掛硬碟，上一次（或其他情境測試）的資料會留著。等新的 Collector 把舊 Pod 留下的堆積送完，
# 再清掉測試用的 data stream，之後進來的都是這次的資料
sleep 15
es -X DELETE "${ES_URL}/_data_stream/k8s-*-local" >/dev/null
kubectl apply -f "${here}/log-generators.yaml" >/dev/null
for ns in it-mes-prod imc-wip-prod legacy; do
  kubectl -n "${ns}" rollout status deployment --timeout=180s >/dev/null
done
echo "   等 ${WAIT_SECONDS} 秒讓資料進來"
sleep "${WAIT_SECONDS}"
es -X POST "${ES_URL}/k8s-*/_refresh" >/dev/null

echo "== 4. 分流"
ns_q() { echo "{\"term\": {\"kubernetes.namespace\": \"$1\"}}"; }
check_gt0 "it-mes-prod 進 k8s-it-local"           "$(count k8s-it-local "$(ns_q it-mes-prod)")"
check_gt0 "imc-wip-prod 進 k8s-imc-local"         "$(count k8s-imc-local "$(ns_q imc-wip-prod)")"
check_gt0 "legacy（沒標 dept）進 k8s-unassigned-local" "$(count k8s-unassigned-local "$(ns_q legacy)")"
check "k8s-it-local 裡沒有別的 namespace"  "$(count k8s-it-local '{"bool":{"must_not":{"term":{"kubernetes.namespace":"it-mes-prod"}}}}')" 0
check "k8s-imc-local 裡沒有別的 namespace" "$(count k8s-imc-local '{"bool":{"must_not":{"term":{"kubernetes.namespace":"imc-wip-prod"}}}}')" 0
check "it、imc 的資料沒有落到 unassigned"  "$(count k8s-unassigned-local '{"terms":{"kubernetes.namespace":["it-mes-prod","imc-wip-prod"]}}')" 0
# 兩邊只數同一段時間（到 upto 為止）：資料一直在進，先後數會差幾筆
upto="$(date -u -d '-30 seconds' +%Y-%m-%dT%H:%M:%SZ)"
until_q="{\"range\":{\"@timestamp\":{\"lte\":\"${upto}\"}}}"
check "每筆只送一份（三個 index 合計 = 全部）" \
  "$(count 'k8s-*' "${until_q}")" \
  "$(( $(count k8s-it-local "${until_q}") + $(count k8s-imc-local "${until_q}") + $(count k8s-unassigned-local "${until_q}") ))"

echo "== 5. 欄位"
doc="$(es "${ES_URL}/k8s-it-local/_search" -d '{"size":1,"query":{"bool":{"filter":[{"term":{"service.name":"mes-api"}},{"exists":{"field":"error.type"}}]}}}' | jq '.hits.hits[0]._source')"
check "service.name（來自 pod label）"  "$(jq -r .service.name <<<"${doc}")" mes-api
check "service.version"                 "$(jq -r .service.version <<<"${doc}")" 2.3.1
check "service.namespace（part-of）"    "$(jq -r .service.namespace <<<"${doc}")" mes
check "labels.group"                    "$(jq -r .labels.group <<<"${doc}")" it
check "labels.dept（來自 namespace label）" "$(jq -r .labels.dept <<<"${doc}")" D3120
check "orchestrator.cluster.name"       "$(jq -r .orchestrator.cluster.name <<<"${doc}")" local
check "log.level 轉小寫"                 "$(jq -r .log.level <<<"${doc}")" error
check "error.type 從 stack trace 拆出"   "$(jq -r .error.type <<<"${doc}")" java.lang.IllegalStateException
check "trace.id"                        "$(jq -r .trace.id <<<"${doc}")" 5bf92f3577b34da6a3ce929d0e0e4736
check "@timestamp 用應用自己的時間（毫秒為 000）" "$(jq -r '."@timestamp" | test("\\.000Z$")' <<<"${doc}")" true
info="$(es "${ES_URL}/k8s-it-local/_search" -d '{"size":1,"query":{"bool":{"filter":[{"term":{"service.name":"mes-api"}},{"term":{"log.level":"info"}}]}}}' | jq '.hits.hits[0]._source')"
check "自訂欄位收進 app（lot_id）"        "$(jq -r '.app.lot_id | startswith("L")' <<<"${info}")" true
check "logstash encoder 的雜訊欄位已移除" "$(jq -r '.app | has("@version") or has("level_value")' <<<"${info}")" false
legacy="$(es "${ES_URL}/k8s-unassigned-local/_search" -d '{"size":1,"query":{"term":{"service.name":"legacy"}}}' | jq '.hits.hits[0]._source')"
check "純文字 log 保留原文"              "$(jq -r '.message | startswith("plain text")' <<<"${legacy}")" true
check "沒套 chart 的服務退回 container 名稱" "$(jq -r .service.name <<<"${legacy}")" legacy
ecs="$(es "${ES_URL}/k8s-imc-local/_search" -d '{"size":1,"sort":[{"@timestamp":"desc"}],"query":{"term":{"service.name":"wip-worker"}}}' | jq '.hits.hits[0]._source')"
check "ECS 巢狀格式：log.level"       "$(jq -r .log.level <<<"${ecs}")" warn
check "ECS 巢狀格式：log.logger"      "$(jq -r .log.logger <<<"${ecs}")" wip.Queue
check "ECS 巢狀格式：thread"          "$(jq -r .process.thread.name <<<"${ecs}")" main
check "ECS 巢狀格式：拿完的欄位不留空殼" "$(jq -c .app <<<"${ecs}")" '{"process":{"pid":1},"ecs":{"version":"8.11"}}'
esdoc="$(es "${ES_URL}/k8s-unassigned-local/_search" -d '{"size":1,"query":{"bool":{"filter":[{"term":{"kubernetes.namespace":"es-test"}},{"exists":{"field":"log.level"}}]}},"sort":[{"@timestamp":"desc"}]}' | jq '.hits.hits[0]._source')"
check_gt0 "ECS 字面點號格式（ES 自己的 log）：log.logger" "$(jq -r '.log.logger // "" | length' <<<"${esdoc}")"
check "收集器標記（agent.type）"         "$(jq -r .agent.type <<<"${doc}")" otelcol
check "Collector 不收自己的 log" "$(count 'k8s-*' '{"bool":{"filter":[{"term":{"kubernetes.namespace":"otel-collector"}},{"range":{"@timestamp":{"gte":"now-'"${WAIT_SECONDS}"'s"}}}]}}')" 0

echo "== 5b. 依格式分派"
svc_doc() { es "${ES_URL}/k8s-it-local/_search" -d "{\"size\":1,\"sort\":[{\"@timestamp\":\"desc\"}],\"query\":{\"term\":{\"service.name\":\"$1\"}}}" | jq '.hits.hits[0]._source'; }
lf="$(svc_doc hold-svc)"
check "logfmt：走 logfmt 規則"          "$(jq -r .log.parser <<<"${lf}")" logfmt
check "logfmt：log.level"               "$(jq -r .log.level <<<"${lf}")" warn
check "logfmt：message 取出 msg"        "$(jq -r .message <<<"${lf}")" "lot hold requested"
check "logfmt：自訂欄位收進 app"         "$(jq -r '.app.lot_id | startswith("L")' <<<"${lf}")" true
check "logfmt：@timestamp 用 ts"        "$(jq -r '."@timestamp" | test("\\.000Z$")' <<<"${lf}")" true
sc="$(svc_doc eap)"
check "還沒有規則的格式：記下宣告的格式" "$(jq -r .log.format <<<"${sc}")" secs
check "還沒有規則的格式：退回 default"   "$(jq -r .log.parser <<<"${sc}")" default
check "還沒有規則的格式：原文完整保留"   "$(jq -r '.message | test("RECV S6F11 W SystemBytes=1234$")' <<<"${sc}")" true
check "沒宣告格式：format 是 default"    "$(jq -r .log.format <<<"${doc}")" default
check "每筆都有 log.parser"              "$(count 'k8s-*' '{"bool":{"must_not":{"exists":{"field":"log.parser"}}}}')" 0


echo "== 5c. 純文字 log 保底抓 level"
text_level() {  # text_level <訊息開頭> → 該行的 log.level 與 log.level_inferred
  es "${ES_URL}/k8s-unassigned-local/_search" -d "{\"size\":1,\"sort\":[{\"@timestamp\":\"desc\"}],\"query\":{\"bool\":{\"filter\":[{\"term\":{\"service.name\":\"text-app\"}},{\"match_phrase_prefix\":{\"message\":\"$1\"}}]}}}" \
    | jq -r '.hits.hits[0]._source.log | "\(.level // "none")/\(.level_inferred // false)"'
}
check "行首附近的 ERROR → error（標記為猜的）" "$(text_level 'lot not found')" error/true
check "[WARNING] → warn"                         "$(text_level 'disk usage')" warn/true
check "INFO: → info"                              "$(text_level 'service started')" info/true
check "小寫的 error 不算"                         "$(text_level 'db connection ok')" none/false
check "ERROR_CODE 連在別的字上不算"               "$(text_level 'processing batch')" none/false
check "[WARN] 前綴 → warn"                        "$(text_level 'cache miss ratio')" warn/true
check "ERROR - 前綴 → error"                      "$(text_level 'payment gateway timeout')" error/true
check "Warning: 行首（非全大寫）→ warn"           "$(text_level 'certificate expires')" warn/true
check "0 errors 不算"                              "$(text_level 'file transfer')" none/false
check "應用自己寫了 level 就不猜（mes-api 沒有猜的）" \
  "$(count k8s-it-local '{"bool":{"filter":[{"term":{"service.name":"mes-api"}},{"term":{"log.level_inferred":true}}]}}')" 0

check "沒有 pipeline 失敗的文件"          "$(count 'k8s-*' '{"exists":{"field":"error.message"}}')" 0

echo "== 6. 權限"
code() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: ApiKey $1" -H 'Content-Type: application/json' "${@:2}"; }
# 寫入 key 只有一把、所有 group 共用（docs/pdca/phase1-collect/2026-10-ingest-simplify.md）：資料屬於哪個 group 由 Collector 決定，
# 上面「冒充」那幾項驗的就是這個；這裡只驗 key 能寫、不能讀
check "寫入 key 可以寫 IT 的 index" \
  "$(code "${WRITER_KEY}" -X POST "${ES_URL}/k8s-it-local/_doc?op_type=create" -d '{"@timestamp":"2026-01-01T00:00:00Z","message":"x"}')" 201
check "寫入 key 可以寫 IMC 的 index" \
  "$(code "${WRITER_KEY}" -X POST "${ES_URL}/k8s-imc-local/_doc?op_type=create" -d '{"@timestamp":"2026-01-01T00:00:00Z","message":"x"}')" 201
check "寫入 key 不能讀" "$(code "${WRITER_KEY}" "${ES_URL}/k8s-it-local/_search")" 403
check "AIOps 唯讀 key 可以讀全部 k8s-*" "$(code "${AIOPS_KEY}" "${ES_URL}/k8s-*/_search")" 200
check "AIOps 唯讀 key 不能寫" \
  "$(code "${AIOPS_KEY}" -X POST "${ES_URL}/k8s-it-local/_doc?op_type=create" -d '{"@timestamp":"2026-01-01T00:00:00Z","message":"x"}')" 403

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
