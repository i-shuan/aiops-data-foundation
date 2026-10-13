#!/usr/bin/env bash
# metrics 的情境測試。要先跑過 run-metrics-e2e.sh（Collector、測試應用、key 都在）。
#
# 用法：ES_URL=http://<ip>:9200 test/local-k3s/run-metrics-scenarios.sh <情境>
#   label    改 legacy 的 dept label（不重啟 Collector），量多久生效，最後改回來
#   outage   ES 停 OUTAGE_SECONDS 秒（預設 180），中途重啟 Collector；確認佇列在重啟後還在、資料補齊、
#            沒有重複、沒有被 ES 丟進 failure store
#   keyfail  作廢 IMC 的寫入 key 3 分鐘；確認 IT 照常寫、IMC 停住、換新 key 後恢復（失效那段不會補回來，已知限制）
#
# 測試應用每 30 秒被抓一次，所以 3 分鐘內每個應該有 6 筆。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
ES_PASSWORD=changeme-test-only
KCTX="${KCTX:-default}"
k() { kubectl --context "${KCTX}" "$@"; }
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ts() { date -u +%H:%M:%S; }

fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
check_ge() { if [[ "$2" -ge "$3" ]]; then echo "  PASS  $1（$2）"; else echo "  FAIL  $1：得到 $2，預期至少 $3"; fail=1; fi; }

refresh() { es -X POST "${ES_URL}/metrics-*.otel-*/_refresh" >/dev/null; }

# 某段時間內，某個 namespace 的測試應用有幾筆 demo_requests_total
app_count() {  # app_count <group> <namespace> <from> <to>
  es "${ES_URL}/metrics-prometheusreceiver.otel-$1/_count" -d "{\"query\":{\"bool\":{\"filter\":[
    {\"exists\":{\"field\":\"metrics.demo_requests_total\"}},
    {\"term\":{\"resource.attributes.k8s.namespace.name\":\"$2\"}},
    {\"range\":{\"@timestamp\":{\"gte\":\"$3\",\"lt\":\"$4\"}}}]}}}" | jq -r '.count // 0'
}

# 同一個 Pod、同一個時間點出現不只一筆的數量（重複寫入）
dup_count() {  # dup_count <from> <to>
  es "${ES_URL}/metrics-prometheusreceiver.otel-*/_search" -d "{\"size\":0,\"query\":{\"bool\":{\"filter\":[
    {\"exists\":{\"field\":\"metrics.demo_requests_total\"}},
    {\"range\":{\"@timestamp\":{\"gte\":\"$1\",\"lt\":\"$2\"}}}]}},
    \"aggs\":{\"d\":{\"multi_terms\":{\"terms\":[{\"field\":\"resource.attributes.k8s.pod.name\"},{\"field\":\"@timestamp\"}],
      \"min_doc_count\":2,\"size\":1000}}}}" | jq -r '[.aggregations.d.buckets[].doc_count - 1] | add // 0'
}

# 等到某段時間的資料補齊（或逾時），回傳花了幾秒
wait_filled() {  # wait_filled <group> <namespace> <from> <to> <預期筆數> <最多等幾秒>
  local start=$SECONDS
  while (( SECONDS - start < $6 )); do
    refresh
    [[ "$(app_count "$1" "$2" "$3" "$4")" -ge "$5" ]] && { echo $(( SECONDS - start )); return; }
    sleep 5
  done
  echo -1
}

scenario_label() {
  echo "== 改 legacy 的 dept label（不重啟 Collector）"
  local t0 s0 first
  t0="$(now)"; s0=$SECONDS
  k label namespace legacy dept=D3120 --overwrite >/dev/null
  echo "   $(ts) 改成 dept=D3120"
  first=""
  while (( SECONDS - s0 < 300 )) && [[ -z "${first}" ]]; do
    refresh
    first="$(es "${ES_URL}/metrics-*.otel-it/_search" -d "{\"size\":1,\"sort\":[{\"@timestamp\":\"asc\"}],\"query\":{\"bool\":{\"filter\":[
      {\"term\":{\"resource.attributes.k8s.namespace.name\":\"legacy\"}},{\"range\":{\"@timestamp\":{\"gte\":\"${t0}\"}}}]}}}" \
      | jq -r '.hits.hits[0]._source["@timestamp"] // empty')"
    [[ -z "${first}" ]] && sleep 5
  done
  k label namespace legacy dept- >/dev/null
  echo "   已改回原狀"
  [[ -n "${first}" ]] || { echo "  FAIL  5 分鐘內沒有生效"; fail=1; return; }
  local lag=$(( first / 1000 - $(date -u -d "${t0}" +%s) ))
  echo "   第一筆進 IT 的資料時間：$(date -u -d "@$(( first / 1000 ))" +%H:%M:%S)，改 label 後 ${lag} 秒"
  if (( lag <= 60 )); then echo "  PASS  1 分鐘內生效（${lag} 秒）"; else echo "  FAIL  超過 1 分鐘（${lag} 秒）"; fail=1; fi
}

scenario_outage() {
  local half=$(( ${OUTAGE_SECONDS:-180} / 2 ))
  echo "== ES 停 ${OUTAGE_SECONDS:-180} 秒，中途重啟 Collector"
  local down restart_at restarted up
  down="$(now)"
  k -n es-test scale statefulset/elasticsearch --replicas=0 >/dev/null
  k -n es-test wait --for=delete pod/elasticsearch-0 --timeout=120s >/dev/null 2>&1 || true
  echo "   $(ts) ES 關掉"
  sleep "${half}"
  restart_at="$(now)"
  k -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
  k -n otel-collector rollout status daemonset/otel-node-collector --timeout=120s >/dev/null
  k -n otel-collector rollout status deployment/otel-cluster-collector --timeout=120s >/dev/null
  restarted="$(now)"
  echo "   $(ts) 停機中重啟了兩個 Collector"
  sleep "${half}"
  echo "   $(ts) 節點上的佇列：$(du -sh /var/lib/otelcol 2>/dev/null | cut -f1)"
  up="$(now)"
  k -n es-test scale statefulset/elasticsearch --replicas=1 >/dev/null
  until [[ "$(es -m 5 "${ES_URL}/_cluster/health" 2>/dev/null | jq -r .status 2>/dev/null)" =~ green|yellow ]]; do sleep 2; done
  echo "   $(ts) ES 可以用了，等三個 group 都補完（最多 5 分鐘）"

  # 每個應用每 30 秒抓一次：停機到重啟前約 90 秒、重啟完到 ES 恢復約 90 秒，各至少 2 筆
  local g ns before after
  for g in it:it-mes-prod imc:imc-wip-prod unassigned:legacy; do
    # 兩段都要等：重啟後，佇列裡較舊的資料可能比新抓的晚幾秒才送到
    wait_filled "${g%%:*}" "${g#*:}" "${restarted}" "${up}" 2 300 >/dev/null
    wait_filled "${g%%:*}" "${g#*:}" "${down}" "${restart_at}" 2 300 >/dev/null
    before="$(app_count "${g%%:*}" "${g#*:}" "${down}" "${restart_at}")"
    after="$(app_count "${g%%:*}" "${g#*:}" "${restarted}" "${up}")"
    check_ge "${g%%:*}：停機後、重啟前收到的（重啟時還在佇列裡）有送進 ES" "${before}" 2
    check_ge "${g%%:*}：重啟後、ES 恢復前收到的有送進 ES" "${after}" 2
    echo "         整段停機 $(app_count "${g%%:*}" "${g#*:}" "${down}" "${up}") 筆（每 30 秒一筆，最多約 $(( ${OUTAGE_SECONDS:-180} / 30 )) 筆；重啟那段可能沒抓到 1～2 筆）"
  done
  check "沒有重複（同一個 Pod 同一個時間點只有一筆）" "$(dup_count "${down}" "$(now)")" 0
  # ES 會把時間不在可寫入範圍的資料丟進 failure store，回給 Collector 的仍是成功，Collector 不會報錯
  check "沒有資料被 ES 丟進 failure store" \
    "$(es "${ES_URL}/.fs-metrics-*/_count?expand_wildcards=all&ignore_unavailable=true" -d "{\"query\":{\"range\":{\"@timestamp\":{\"gte\":\"${down}\"}}}}" | jq -r '.count // 0')" 0
  local dropped=0 f
  for f in /var/log/pods/otel-collector_*/otelcol/*.log; do
    dropped=$(( dropped + $(grep -c "Dropping data" "${f}" || true) ))
  done
  check "Collector 沒有丟資料的紀錄" "${dropped}" 0
}

scenario_keyfail() {
  echo "== 作廢 IMC 的寫入 key 3 分鐘"
  local bad good newkey
  bad="$(now)"
  es -X DELETE "${ES_URL}/_security/api_key" -d '{"name":"es-writer-imc"}' | jq -c '{invalidated: (.invalidated_api_keys | length)}'
  echo "   $(ts) IMC 的 key 作廢"
  sleep 180
  good="$(now)"
  refresh
  check_ge "IMC 的 key 失效期間，IT 照常寫入" "$(app_count it it-mes-prod "${bad}" "${good}")" 5
  check "IMC 的 key 失效期間，IMC 寫不進去" "$(app_count imc imc-wip-prod "${bad}" "${good}")" 0
  newkey="$(es -X POST "${ES_URL}/_security/api_key" -d '{"name":"es-writer-imc","role_descriptors":{"es-writer-imc":{"index":[
    {"names":["k8s-imc-*","k8s-events-imc-*","metrics-*-imc","logs-*.otel-imc","traces-*.otel-imc"],"privileges":["create_doc","create_index","auto_configure"]}]}}}' | jq -r .encoded)"
  k -n otel-collector get secret es-writer-keys -o json \
    | jq --arg v "$(printf %s "${newkey}" | base64 -w0)" '.data.IMC_ES_API_KEY = $v' | k apply -f - >/dev/null
  k -n otel-collector rollout restart daemonset/otel-node-collector deployment/otel-cluster-collector >/dev/null
  k -n otel-collector rollout status daemonset/otel-node-collector --timeout=120s >/dev/null
  echo "   $(ts) 換上新 key、重啟 Collector"
  sleep 90
  refresh
  check_ge "換 key 後，IMC 恢復寫入" "$(app_count imc imc-wip-prod "${good}" "$(now)")" 1
  # 已知限制：OTLP 出口只重試 429/502/503/504，401 當成永久錯誤直接丟，key 失效那段的資料補不回來
  #（log 也一樣）。這裡只記錄筆數，不判失敗。
  echo "   key 失效那段的 IMC 資料：$(app_count imc imc-wip-prod "${bad}" "${good}") 筆（已知會遺失，見 docs/metrics/README.md）"
}

case "${1:-}" in
  label) scenario_label ;;
  outage) scenario_outage ;;
  keyfail) scenario_keyfail ;;
  *) echo "用法：$0 label|outage|keyfail"; exit 2 ;;
esac
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
