#!/usr/bin/env bash
# 測試 ES 改成兩個節點，比較跟單節點的差別。要先跑過 run-e2e.sh、run-metrics-e2e.sh（log、metrics 都在送）。
#
# 用法：ES_URL=http://<ip>:9200 e2e/local-k3s/run-es-2node.sh <動作>
#   up      單節點 → 兩節點（elasticsearch-0 的資料沿用）
#   test    兩個節點輪流卡住 DOWN_SECONDS 秒（預設 180）：先「沒有投票權」的，再「有投票權」的；
#           看卡住期間 log、metrics 有沒有照常寫入、事後有沒有補齊、有沒有重複
#   down    兩節點 → 單節點（先把 elasticsearch-1 移出投票、確認 elasticsearch-0 有每個 shard，再縮）
#
# 前提：主機的 vm.max_map_count 至少 262144（多節點時 ES 會檢查，單節點不會）。
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

wait_health() {  # wait_health <green|yellow> <最多幾秒>
  local s=$SECONDS
  until [[ "$(es -m 5 "${ES_URL}/_cluster/health" 2>/dev/null | jq -r .status 2>/dev/null)" == "$1" ]]; do
    (( SECONDS - s < $2 )) || { echo "  FAIL  $2 秒內沒變 $1"; fail=1; return; }
    sleep 3
  done
  echo "   $(ts) cluster $1（$(( SECONDS - s )) 秒）"
}

make_certs() {
  local d; d="$(mktemp -d)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=es-test-ca -keyout "${d}/ca.key" -out "${d}/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj /CN=elasticsearch -keyout "${d}/node.key" -out "${d}/node.csr" 2>/dev/null
  openssl x509 -req -in "${d}/node.csr" -CA "${d}/ca.crt" -CAkey "${d}/ca.key" -CAcreateserial -days 3650 -out "${d}/node.crt" 2>/dev/null
  k -n es-test create secret generic es-transport-certs \
    --from-file="${d}/ca.crt" --from-file="${d}/node.crt" --from-file="${d}/node.key" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  rm -rf "${d}"
}

do_up() {
  [[ "$(cat /proc/sys/vm/max_map_count)" -ge 262144 ]] || { echo "vm.max_map_count 要至少 262144：sysctl -w vm.max_map_count=262144"; exit 1; }
  echo "== 單節點 → 兩節點"
  k -n es-test get secret es-transport-certs >/dev/null 2>&1 || make_certs
  # 先用多節點的設定重啟 elasticsearch-0（它自己就是 master），再加 elasticsearch-1。
  # 一次做的話，StatefulSet 會先開 elasticsearch-1，它找不到 master、一直不 ready，elasticsearch-0 就不會更新，卡死。
  k apply -f "${here}/elasticsearch-2node.yaml" >/dev/null
  k -n es-test rollout status statefulset/elasticsearch --timeout=600s >/dev/null
  echo "   $(ts) elasticsearch-0 改成多節點設定"
  k -n es-test scale statefulset/elasticsearch --replicas=2 >/dev/null
  k -n es-test rollout status statefulset/elasticsearch --timeout=600s >/dev/null
  echo "   $(ts) elasticsearch-1 加入"
  wait_health green 600
  show
}

show() {
  echo "   節點：$(es "${ES_URL}/_cat/nodes?h=name,master,node.role" | sort | tr '\n' ';')"
  echo "   投票的節點：$(voters | tr '\n' ' ')"
  echo "   $(es "${ES_URL}/_cluster/health" | jq -c '{status, number_of_nodes, active_shards, unassigned_shards}')"
}

# 有投票權的節點名稱。兩個 master 候選時，ES 只讓其中一個投票（投票的要是奇數個）
voters() {
  local ids
  ids="$(es "${ES_URL}/_cluster/state/metadata?filter_path=metadata.cluster_coordination.last_committed_config" | jq -r '.metadata.cluster_coordination.last_committed_config[]')"
  es "${ES_URL}/_nodes/_all/name?filter_path=nodes.*.name" | jq -r --arg ids "${ids}" '.nodes | to_entries[] | select(.key as $k | $ids | split("\n") | index($k)) | .value.name'
}

app_count() {  # app_count <group> <from> <to>
  es "${Q_URL:-${ES_URL}}/metrics-prometheusreceiver.otel-$1/_count" -d "{\"query\":{\"bool\":{\"filter\":[
    {\"exists\":{\"field\":\"metrics.demo_requests_total\"}},{\"range\":{\"@timestamp\":{\"gte\":\"$2\",\"lt\":\"$3\"}}}]}}}" 2>/dev/null | jq -r '.count // 0' 2>/dev/null || echo "叫不到"
}
log_count() {  # log_count <group> <from> <to>
  es "${Q_URL:-${ES_URL}}/k8s-$1-*/_count" -d "{\"query\":{\"range\":{\"@timestamp\":{\"gte\":\"$2\",\"lt\":\"$3\"}}}}" 2>/dev/null | jq -r '.count // 0' 2>/dev/null || echo "叫不到"
}
dup_count() {  # dup_count <from> <to>：同一個 Pod 同一個時間點的 metrics 不只一筆
  es "${ES_URL}/metrics-prometheusreceiver.otel-*/_search" -d "{\"size\":0,\"query\":{\"bool\":{\"filter\":[
    {\"exists\":{\"field\":\"metrics.demo_requests_total\"}},{\"range\":{\"@timestamp\":{\"gte\":\"$1\",\"lt\":\"$2\"}}}]}},
    \"aggs\":{\"d\":{\"multi_terms\":{\"terms\":[{\"field\":\"resource.attributes.k8s.pod.name\"},{\"field\":\"@timestamp\"}],
      \"min_doc_count\":2,\"size\":1000}}}}" | jq -r '[.aggregations.d.buckets[].doc_count - 1] | add // 0'
}
refresh() { es -X POST "${ES_URL}/metrics-*.otel-*,k8s-*/_refresh" >/dev/null 2>&1 || true; }

# 讓一個節點卡死 DOWN_SECONDS 秒（暫停 ES 程序，像主機當掉、網路斷掉：連線還在但不回應），記錄：
# 剩下的節點有沒有 master、經過 Service 叫不叫得到 ES、卡住期間即時寫進去幾筆、事後補到幾筆
freeze_node() {  # freeze_node <pod> <說明>
  local pod=$1 down back survivor ip t s no_master=0 api_down=0 secs=${DOWN_SECONDS:-180}
  survivor="$([[ ${pod} == elasticsearch-0 ]] && echo elasticsearch-1 || echo elasticsearch-0)"
  ip="$(k -n es-test get pod "${survivor}" -o jsonpath='{.status.podIP}')"
  echo
  echo "== ${pod}（$2）卡住 ${secs} 秒"
  down="$(now)"; s=$SECONDS
  k -n es-test exec "${pod}" -c elasticsearch -- sh -c 'kill -STOP $(pgrep java)'
  echo "   $(ts) 卡住"
  # 中途失敗也一定要讓它恢復
  trap "k -n es-test exec ${pod} -c elasticsearch -- sh -c 'kill -CONT \$(pgrep java)'" EXIT
  local last=$SECONDS m a
  while (( SECONDS - s < secs )); do
    m="$(curl -s -m 4 -o /dev/null -w '%{http_code}' -u "elastic:${ES_PASSWORD}" "http://${ip}:9200/_cluster/health" || true)"
    a="$(curl -s -m 4 -o /dev/null -w '%{http_code}' -u "elastic:${ES_PASSWORD}" "${ES_URL}/_cluster/health" || true)"
    sleep 3
    t=$(( SECONDS - last )); last=$SECONDS
    [[ "${m}" == 200 ]] || no_master=$(( no_master + t ))
    [[ "${a}" == 200 ]] || api_down=$(( api_down + t ))
  done
  echo "   卡住期間：剩下的 ${survivor} 沒有 master 約 ${no_master} 秒；經過 Service 叫不到 ES 約 ${api_down} 秒"
  # 先在恢復前數一次：這是「卡住期間即時寫進去的」（沒有 master 時 Service 沒有可用的 Pod，數不到就是 0）
  local g live_m=() live_l=() cut; cut="$(now)"
  # 直接問剩下的節點（沒有 master 時還是能讀）
  curl -s -m 10 -u "elastic:${ES_PASSWORD}" -X POST "http://${ip}:9200/metrics-*.otel-*,k8s-*/_refresh" >/dev/null || true
  for g in it imc unassigned; do
    live_m+=("$(Q_URL="http://${ip}:9200" app_count "${g}" "${down}" "${cut}")")
    live_l+=("$(Q_URL="http://${ip}:9200" log_count "${g}" "${down}" "${cut}")")
  done
  k -n es-test exec "${pod}" -c elasticsearch -- sh -c 'kill -CONT $(pgrep java)'
  trap - EXIT
  back="$(now)"
  echo "   $(ts) ${pod} 恢復"
  wait_health green 600
  sleep 60; refresh
  local i=0 final_m final_l expect=$(( secs / 30 - 1 ))
  for g in it imc unassigned; do
    final_m="$(app_count "${g}" "${down}" "${cut}")"; final_l="$(log_count "${g}" "${down}" "${cut}")"
    echo "   ${g}：metrics 卡住期間即時寫入 ${live_m[$i]} 筆、最後 ${final_m} 筆；log 卡住期間即時寫入 ${live_l[$i]} 筆、最後 ${final_l} 筆"
    check_ge "${g}：卡住那段的應用 metrics 都有進 ES" "${final_m}" "${expect}"
    check_ge "${g}：卡住那段的 log 有進 ES" "${final_l}" 1
    i=$(( i + 1 ))
  done
  check "沒有重複的 metrics" "$(dup_count "${down}" "$(now)")" 0
  check "沒有資料被丟進 failure store" \
    "$(es "${ES_URL}/.fs-*/_count?expand_wildcards=all&ignore_unavailable=true" -d "{\"query\":{\"range\":{\"@timestamp\":{\"gte\":\"${down}\"}}}}" | jq -r '.count // 0')" 0
  local dropped=0 f
  for f in /var/log/pods/otel-collector_*/otelcol/*.log; do dropped=$(( dropped + $(grep -c "Dropping data" "${f}" || true) )); done
  check "Collector 沒有丟資料的紀錄" "${dropped}" 0
  show
}

do_test() {
  local voter other
  show
  voter="$(voters | head -1)"
  other="$([[ ${voter} == elasticsearch-0 ]] && echo elasticsearch-1 || echo elasticsearch-0)"
  freeze_node "${other}" "沒有投票權的那個"
  voter="$(voters | head -1)"
  freeze_node "${voter}" "有投票權的那個"
}

do_down() {
  echo "== 兩節點 → 單節點"
  # elasticsearch-1 移出投票、不再放新的 shard，確認每個 shard 在 elasticsearch-0 都有一份，再縮成一個
  es -X POST "${ES_URL}/_cluster/voting_config_exclusions?node_names=elasticsearch-1" >/dev/null
  es -X PUT "${ES_URL}/_cluster/settings" -d '{"persistent":{"cluster.routing.allocation.exclude._name":"elasticsearch-1"}}' >/dev/null
  # 主 shard 在 elasticsearch-1、副本在 elasticsearch-0 的，ES 不會對調；縮掉 elasticsearch-1 時副本會自動升成主的。
  # 所以只要求每個 shard 在 elasticsearch-0 有一份已啟動的（主或副本都可以）
  missing() {
    es "${ES_URL}/_cat/shards?h=index,shard,state,node&format=json" \
      | jq '[group_by(.index + "/" + .shard)[] | select(map(select(.node == "elasticsearch-0" and .state == "STARTED")) | length == 0)] | length'
  }
  until [[ "$(missing)" == 0 ]]; do sleep 5; done
  echo "   $(ts) 每個 shard 在 elasticsearch-0 都有一份"
  k -n es-test scale statefulset/elasticsearch --replicas=1 >/dev/null
  k -n es-test wait --for=delete pod/elasticsearch-1 --timeout=300s >/dev/null 2>&1 || true
  es -X DELETE "${ES_URL}/_cluster/voting_config_exclusions?wait_for_removal=false" >/dev/null
  es -X PUT "${ES_URL}/_cluster/settings" -d '{"persistent":{"cluster.routing.allocation.exclude._name":null}}' >/dev/null
  k apply -f "${here}/elasticsearch.yaml" >/dev/null
  k -n es-test rollout status statefulset/elasticsearch --timeout=600s >/dev/null
  k -n es-test delete service es-node-0 es-node-1 --ignore-not-found >/dev/null
  k -n es-test delete pvc data-elasticsearch-1 --ignore-not-found --wait=false >/dev/null
  wait_health yellow 600
  show
}

case "${1:-}" in
  up) do_up ;;
  test) do_test ;;
  down) do_down ;;
  *) echo "用法：$0 up|test|down"; exit 2 ;;
esac
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
