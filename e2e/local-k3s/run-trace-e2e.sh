#!/usr/bin/env bash
# trace 端到端驗證：應用 → 同節點的 OpenTelemetry Collector → 依 Pod 的 namespace 分 group → ES 的 traces-*.otel-<group>。
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector、測試 namespace 都在）。
# 用法：ES_URL=http://<ip>:9200 e2e/local-k3s/run-trace-e2e.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
check_gt0() { if [[ "$2" -gt 0 ]]; then echo "  PASS  $1（$2）"; else echo "  FAIL  $1：得到 $2"; fail=1; fi; }

echo "== 1. API key（正式腳本，含 trace 權限）"
setup_out="$(ES_URL="${ES_URL}" ES_AUTH="elastic:${ES_PASSWORD}" "${repo}/telemetry/scripts/elasticsearch-setup.sh")"
key_of() { echo "${setup_out}" | grep -A1 "=== $1 ===" | tail -1 | jq -r .encoded; }
WRITER_KEY="$(key_of '寫入 key：es-writer')"; AIOPS_KEY="$(key_of 'AIOps 唯讀 key')"
kubectl -n otel-collector create secret generic es-writer-keys --from-literal=ES_WRITER_API_KEY="${WRITER_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "== 2. Collector 與測試應用"
kubectl apply -k "${here}/otel-collector" >/dev/null
kubectl -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=180s >/dev/null
for ns in it-mes-prod imc-wip-prod legacy; do
  kubectl -n "${ns}" create configmap trace-app --from-file=run.sh="${here}/trace-app.sh" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
kubectl apply -f "${here}/trace-apps.yaml" >/dev/null
for ns in it-mes-prod imc-wip-prod legacy; do
  kubectl -n "${ns}" rollout restart deployment/trace-app >/dev/null
  kubectl -n "${ns}" rollout status deployment/trace-app --timeout=180s >/dev/null
done
start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "   只看 ${start} 之後的資料，等 60 秒"
sleep 60
es -X POST "${ES_URL}/traces-*.otel-*,k8s-*/_refresh" >/dev/null

since="{\"range\":{\"@timestamp\":{\"gte\":\"${start}\"}}}"
count() { es "${ES_URL}/$1/_count" -d "{\"query\":{\"bool\":{\"filter\":[${since}${2:+,$2}]}}}" | jq -r '.count // 0'; }
ns_f() { echo "{\"term\":{\"resource.attributes.k8s.namespace.name\":\"$1\"}}"; }
app_f='{"term":{"resource.attributes.service.name":"trace-app"}}'

echo "== 3. 分流"
check_gt0 "it-mes-prod 的 trace 進 IT"                 "$(count traces-generic.otel-it "${app_f},$(ns_f it-mes-prod)")"
check_gt0 "imc-wip-prod 的 trace 進 IMC"               "$(count traces-generic.otel-imc "${app_f},$(ns_f imc-wip-prod)")"
check_gt0 "legacy（沒標 dept）的 trace 進 unassigned"  "$(count traces-generic.otel-unassigned "${app_f},$(ns_f legacy)")"
check "IT 裡沒有別的 namespace"  "$(count traces-generic.otel-it "{\"bool\":{\"must_not\":$(ns_f it-mes-prod)}}")" 0
check "IMC 裡沒有別的 namespace" "$(count traces-generic.otel-imc "{\"bool\":{\"must_not\":$(ns_f imc-wip-prod)}}")" 0

echo "== 4. 擋冒充（it-mes-prod 的應用自稱 imc-wip-prod、dept D1110、group imc、假的 Pod UID）"
doc="$(es "${ES_URL}/traces-generic.otel-it/_search" -d "{\"size\":1,\"sort\":[{\"@timestamp\":\"desc\"}],\"query\":{\"bool\":{\"filter\":[${since},${app_f}]}}}" | jq '.hits.hits[0]._source')"
check "namespace 是真的（it-mes-prod）"   "$(jq -r '.resource.attributes["k8s.namespace.name"]' <<<"${doc}")" it-mes-prod
check "dept 是真的（D3120）"              "$(jq -r '.resource.attributes.dept' <<<"${doc}")" D3120
check "group 是 it"                       "$(jq -r '.resource.attributes.group' <<<"${doc}")" it
check "Pod UID 是真的，不是假的"          "$(jq -r '.resource.attributes["k8s.pod.uid"] != "00000000-0000-0000-0000-000000000000" and .resource.attributes["k8s.pod.uid"] != null' <<<"${doc}")" true
check "IMC 裡沒有 it-mes-prod 冒充的資料" "$(count traces-generic.otel-imc "$(ns_f it-mes-prod)")" 0

echo "== 5. 欄位"
check "service.name（應用自己的）"        "$(jq -r '.resource.attributes["service.name"]' <<<"${doc}")" trace-app
check "service.version（Pod label）"     "$(jq -r '.resource.attributes["service.version"]' <<<"${doc}")" 3.0.0
check "k8s.cluster.name"                 "$(jq -r '.resource.attributes["k8s.cluster.name"]' <<<"${doc}")" local
check "k8s.pod.name"                     "$(jq -r '.resource.attributes["k8s.pod.name"] | startswith("trace-app-")' <<<"${doc}")" true
check "沒有夾帶 pod label、container 欄位" "$(jq -r '[.resource.attributes | keys[] | select(startswith("k8s.pod.labels.") or startswith("container."))] | length' <<<"${doc}")" 0
check "span 名稱"                         "$(jq -r .name <<<"${doc}")" "GET /orders"

echo "== 6. log 和 trace 串得起來"
tid="$(jq -r .trace_id <<<"${doc}")"
check "這個 trace 的 log 在 IT 的 log 裡找得到（trace.id = ${tid}）" \
  "$(es "${ES_URL}/k8s-it-local/_count" -d "{\"query\":{\"term\":{\"trace.id\":\"${tid}\"}}}" | jq .count)" 1
check "反過來，從 log 的 trace.id 找得到 trace" \
  "$(es "${ES_URL}/traces-*.otel-*/_count" -d "{\"query\":{\"term\":{\"trace_id\":\"${tid}\"}}}" | jq .count)" 1

echo "== 7. 重送同一個 span 不會重複（_id = trace_id-span_id）"
tid2="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"; sid2="$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"; t="$(date +%s)000000000"
span="{\"resourceSpans\":[{\"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"dup-test\"}}]},
  \"scopeSpans\":[{\"spans\":[{\"traceId\":\"${tid2}\",\"spanId\":\"${sid2}\",\"name\":\"dup\",\"kind\":2,\"startTimeUnixNano\":\"${t}\",\"endTimeUnixNano\":\"${t}\"}]}]}]}"
kubectl -n legacy run "dup-test-$$" --rm -i --restart=Never --image=docker.io/curlimages/curl:8.10.1 --command -- \
  sh -c "for i in 1 2 3; do curl -s -o /dev/null -X POST -H 'Content-Type: application/json' http://otel-node-collector.otel-collector.svc:4318/v1/traces -d '${span}'; sleep 2; done" >/dev/null 2>&1
sleep 10; es -X POST "${ES_URL}/traces-*.otel-*/_refresh" >/dev/null
check "同一個 span 送 3 次，ES 只有 1 筆" "$(es "${ES_URL}/traces-*.otel-*/_count" -d "{\"query\":{\"term\":{\"span_id\":\"${sid2}\"}}}" | jq .count)" 1

echo "== 8. 權限"
can() { curl -sS -m 30 -H "Authorization: ApiKey $1" -H 'Content-Type: application/json' "${ES_URL}/_security/user/_has_privileges" \
  -d "{\"index\":[{\"names\":[\"$2\"],\"privileges\":[\"$3\"]}]}" | jq -r ".index[\"$2\"][\"$3\"]"; }
check "寫入 key 可以寫 IT 的 trace"        "$(can "${WRITER_KEY}" traces-x.otel-it create_doc)" true
check "寫入 key 可以寫 IMC 的 trace"       "$(can "${WRITER_KEY}" traces-x.otel-imc create_doc)" true
check "寫入 key 不能讀 trace"              "$(can "${WRITER_KEY}" traces-x.otel-it read)" false
check "AIOps 唯讀 key 可以讀全部 trace"   "$(can "${AIOPS_KEY}" traces-x.otel-imc read)" true
check "IT 的唯讀角色看得到 IT 的 trace"   "$(es "${ES_URL}/_security/role/logs-reader-it" | jq -r '.["logs-reader-it"].indices[0].names | index("traces-*.otel-it") != null')" true

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
