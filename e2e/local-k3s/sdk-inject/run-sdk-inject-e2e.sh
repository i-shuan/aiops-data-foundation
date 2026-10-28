#!/usr/bin/env bash
# 「不改程式」自動埋點驗證：Java agent 2.20.0、.NET 自動埋點 1.12.0（.NET 8）各兩個服務＋Kafka，程式裡沒有任何 OpenTelemetry。
# 每個情境發一個帶 rid 的請求，進 ES 後確認 log 的 trace_id／span_id 跟 span 對得上（docs/pdca/phase1-collect/2026-10-sdk-inject.md）：
#   基本、上游 traceparent（正常／格式錯／未取樣 00）、兩跳 A→B→C（跨語言）、執行緒池等子工作、背景排程、Kafka
#
# 前提：run-e2e.sh 跑過（ES、Collector；trace-sampler 是測試用的全部留）。部署約 6 分鐘（第一次要下載 image、NuGet 套件）。
# 用法：e2e/local-k3s/sdk-inject/run-sdk-inject-e2e.sh（拿到鎖後自己 port-forward es-test 到 127.0.0.1:${ES_PORT:-31877}，斷了自動重連）
#   已經有通的 ES：ES_URL=http://127.0.0.1:<port>
#   KEEP=1：跑完不刪；SKIP_DEPLOY=1：用已經部署好的（重跑檢查）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../../.." && pwd)"
ES_PASSWORD=changeme-test-only
ns=sdk-cases
fail=0
# es：連不上（port-forward 正在重連、ES 剛重啟）就每 5 秒重試，最多 2 分鐘
es() {
  local i
  for i in $(seq 24); do
    curl -sf -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@" && return 0
    sleep 5
  done
  echo "  FAIL  ES 2 分鐘連不上：$*" >&2; return 1
}
check() { if [[ "$3" == "$4" ]]; then echo "  PASS  $1 $2"; else echo "  FAIL  $1 $2：得到 [$3]，預期 [$4]"; fail=1; fi; }
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
# shellcheck source=../cluster-lock.sh
source "${here}/../cluster-lock.sh"

# 部署到檢查完整段拿著鎖：中間放掉的話，別的任務停 ES、重啟 Collector，這段時間的 log、trace 會不見
cluster_lock "phase1-sdk-inject"
if [[ -z "${SKIP_DEPLOY:-}" ]]; then
  echo "== 1. 部署（namespace ${ns}）"
  kubectl -n otel-collector get statefulset trace-sampler -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value} {end}' | grep -q 'RANDOM_PCT=100 ' \
    || echo "   注意：trace-sampler 不是全部留（RANDOM_PCT≠100），span 找不到的項目會 FAIL"
  kubectl delete namespace "${ns}" --ignore-not-found --wait=true >/dev/null
  kubectl create namespace "${ns}" >/dev/null
  kubectl label namespace "${ns}" dept=D1110 >/dev/null
  kubectl -n "${ns}" create configmap sdk-java-src --from-file="${here}/java/SdkApp.java" \
    --from-file="${repo}/sdk/standards/java/logback-spring.xml" >/dev/null
  kubectl -n "${ns}" create configmap sdk-dotnet-src --from-file="${here}/dotnet/Program.cs" --from-file="${here}/dotnet/app.csproj" >/dev/null
  [[ -f /root/.ccr/ca-bundle.crt ]] && kubectl -n "${ns}" create configmap proxy-ca --from-file=ca-bundle.crt=/root/.ccr/ca-bundle.crt >/dev/null
  kubectl apply -f "${here}/base.yaml" >/dev/null
  render() { sed -e "s/__NAME__/$2/g" -e "s/__CONSUME__/$3/g" -e "s#__WAIT__#${4:-}#" "${here}/$1.yaml"; }
  # 兩台 .NET 一台一台編譯（同時編譯記憶體不夠，每台 SDK 約 1GB）：b 的編譯等 a 起來才開始
  { render java sdk-java-a 1; echo ---; render java sdk-java-b 0; echo ---; render dotnet sdk-dotnet-a 1 ""; echo ---
    render dotnet sdk-dotnet-b 0 "until curl -sf -o /dev/null http://sdk-dotnet-a:8080/leaf?rid=wait; do sleep 5; done; "; } | kubectl apply -f - >/dev/null
  kubectl -n "${ns}" rollout status deploy/kafka --timeout=300s >/dev/null
  for t in sdk-java sdk-dotnet; do   # 先建好 topic：消費者訂閱不存在的 topic，要等 metadata 更新（最多 5 分鐘）才收得到
    MSYS_NO_PATHCONV=1 kubectl -n "${ns}" exec deploy/kafka -- /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --if-not-exists --topic "${t}" --partitions 1 >/dev/null
  done
  for d in sdk-java-a sdk-java-b sdk-dotnet-a sdk-dotnet-b client; do kubectl -n "${ns}" rollout status "deploy/${d}" --timeout=900s >/dev/null; done
  echo "   起來了，等 40 秒（背景排程第一次在 20 秒後）"
  sleep 40
fi

kubectl -n otel-collector rollout status daemonset/otel-node-collector --timeout=120s >/dev/null
kubectl -n otel-collector rollout status statefulset/trace-sampler --timeout=120s >/dev/null
# ES 在鎖裡才 port-forward。ES Pod 重建時 port-forward 會結束（curl: (52) Empty reply），迴圈重開；es() 會等它接上
pfdir=
pf_stop() { [[ -n ${pfdir} ]] && { touch "${pfdir}/stop"; kill "$(cat "${pfdir}/pid" 2>/dev/null)" 2>/dev/null; true; }; }
if [[ -z "${ES_URL:-}" ]]; then
  pfdir="$(mktemp -d)"
  ( while [[ ! -f ${pfdir}/stop ]]; do
      kubectl -n es-test port-forward --address 127.0.0.1 svc/elasticsearch "${ES_PORT:-31877}:9200" >/dev/null 2>&1 &
      echo $! > "${pfdir}/pid"; wait $! || true; sleep 2
    done ) &
  trap 'pf_stop; cluster_unlock' EXIT
  ES_URL="http://127.0.0.1:${ES_PORT:-31877}"
fi
es "${ES_URL}/_cluster/health?wait_for_status=yellow&timeout=30s" >/dev/null
R="$(date +%s)"
req() {  # req <服務> <路徑> [traceparent]
  local h=(); [[ -n "${3:-}" ]] && h=(-H "traceparent: $3")
  kubectl -n "${ns}" exec deploy/client -- curl -s -o /dev/null -m 30 "${h[@]}" "http://$1:8080$2"
}
# log <片語>：最新一筆訊息含這個片語的 log，印「trace_id span_id 訊息」（沒有的欄位印 -）
log() {
  es "${ES_URL}/k8s-*/_search?size=1&sort=@timestamp:desc" -d "{\"query\":{\"bool\":{\"filter\":[
    {\"match_phrase\":{\"message\":\"$1\"}},{\"range\":{\"@timestamp\":{\"gte\":\"now-30m\"}}}]}}}" \
    | jq -r '.hits.hits[0]._source // {} | "\(.trace.id // "-") \(.span.id // "-") \(.message // "-")"'
}
# span <span_id>：印「trace_id parent_span_id kind name」
span() {
  [[ "$1" == - ]] && { echo "- - - -"; return; }
  es "${ES_URL}/traces-*/_search?size=1" -d "{\"query\":{\"term\":{\"span_id\":\"$1\"}}}" \
    | jq -r '.hits.hits[0]._source // {} | "\(.trace_id // "-") \(.parent_span_id // "-") \(.kind // "-") \(.name // "-" | gsub(" "; "_"))"'
}
spans_of() { es "${ES_URL}/traces-*/_count" -d "{\"query\":{\"term\":{\"trace_id\":\"$1\"}}}" | jq .count; }
# root_of <trace_id>：這個 trace 最上層（沒有 parent）的那一段，印「kind name」
root_of() {
  es "${ES_URL}/traces-*/_search?size=1" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"trace_id\":\"$1\"}}],\"must_not\":[{\"exists\":{\"field\":\"parent_span_id\"}}]}}}" \
    | jq -r '.hits.hits[0]._source // {} | "\(.kind // "-") \(.name // "-" | gsub(" "; "_"))"'
}

echo "== 2. 先確認平台的 trace 路徑通"
# 不動 cluster：不通就停下來回報（master 版 node.yaml 的 loadbalancing 佇列檔問題會讓 trace 全斷，docs/troubleshooting.md「trace」）
es -X POST "${ES_URL}/traces-*/_refresh" >/dev/null
recent="$(es "${ES_URL}/traces-*/_count" -d '{"query":{"range":{"@timestamp":{"gte":"now-1m"}}}}' | jq .count)"
echo "   最近 1 分鐘進 ES 的 span：${recent}"
if [[ ${recent} -eq 0 ]]; then
  echo "  FAIL  最近 1 分鐘沒有任何 span 進 ES：trace 路徑斷了，回報平台，不要自己重啟 Collector"
  exit 1
fi
# 6 個不同 trace：node Collector 用 trace_id 分給 trace-sampler，只通一台的話會缺一半
n=0
for k in 1 2 3 4 5 6; do req sdk-java-a "/leaf?rid=probe${R}x${k}"; done
for i in $(seq 24); do
  sleep 5
  es -X POST "${ES_URL}/k8s-*,traces-*/_refresh" >/dev/null
  n=0
  for k in 1 2 3 4 5 6; do
    read -r _ ps _ <<<"$(log "leaf rid probe${R}x${k}")"
    [[ ${ps} != - && $(span "${ps}" | cut -d' ' -f1) != - ]] && n=$((n + 1))
  done
  [[ ${n} -eq 6 ]] && break
done
echo "   探測 ${n}/6 個 span 2 分鐘內進 ES"
if [[ ${n} -ne 6 ]]; then
  echo "  FAIL  探測請求的 span 沒全部進 ES：trace 路徑（全部或一台 trace-sampler）斷了，回報平台，不要自己重啟 Collector"
  exit 1
fi
# log 從印出到進 ES 花多久：共用 cluster 有壓測時 Collector 會積壓，延遲大的那輪結果要註明
delay="$(es "${ES_URL}/k8s-*/_search?size=1" -d "{\"query\":{\"match_phrase\":{\"message\":\"leaf rid probe${R}x6\"}}}"   | jq -r '.hits.hits[0]._source | [."@timestamp", .event.ingested] | map(.[0:19] + "Z" | fromdate) | .[1] - .[0]')"
echo "   log 進 ES 延遲約 ${delay} 秒$([[ ${delay} -gt 10 ]] && echo "（偏慢：cluster 可能有別的負載）")"

echo "== 3. 發請求（rid 後綴 ${R}）"
declare -A TP_T TP_P
for L in java dotnet; do
  a="sdk-${L}-a"; b="sdk-${L}-b"; other="$([[ ${L} == java ]] && echo sdk-dotnet-a || echo sdk-java-a)"
  req "${a}" "/hop?rid=${L}base${R}"
  TP_T[${L}ok]="$(hex 16)"; TP_P[${L}ok]="$(hex 8)"
  req "${a}" "/hop?rid=${L}tpok${R}" "00-${TP_T[${L}ok]}-${TP_P[${L}ok]}-01"
  req "${a}" "/hop?rid=${L}tpbad${R}" "00-xyz-123-01"
  TP_T[${L}off]="$(hex 16)"; TP_P[${L}off]="$(hex 8)"
  req "${a}" "/hop?rid=${L}tpoff${R}&next=http://${b}:8080" "00-${TP_T[${L}off]}-${TP_P[${L}off]}-00"
  req "${a}" "/hop?rid=${L}chain${R}&next=http://${b}:8080,http://${other}:8080"
  for w in $([[ ${L} == java ]] && echo "pool cf thread" || echo "async taskrun pool thread"); do req "${a}" "/${w}?rid=${L}${w}${R}"; done
  req "${a}" "/kafka?rid=${L}kafka${R}"
done
# 等最後送的（兩種語言 Kafka 消費後的呼叫）log 和 span 都進 ES：log 延遲會變（實測 1～60 秒），不能固定等
echo "   等 log、span 進 ES（最多 5 分鐘）"
for i in $(seq 60); do
  sleep 5
  es -X POST "${ES_URL}/k8s-*,traces-*/_refresh" >/dev/null
  n=0
  for L in java dotnet; do
    read -r _ ps _ <<<"$(log "leaf rid ${L}kafka${R}")"
    [[ ${ps} != - && $(span "${ps}" | cut -d' ' -f1) != - ]] && n=$((n + 1))
  done
  [[ ${n} -eq 2 ]] && break
done
echo "   $((i * 5)) 秒到齊（${n}/2）；再等 10 秒"
sleep 10
es -X POST "${ES_URL}/k8s-*,traces-*/_refresh" >/dev/null

for L in java dotnet; do
  a="sdk-${L}-a"; b="sdk-${L}-b"; other="$([[ ${L} == java ]] && echo sdk-dotnet-a || echo sdk-java-a)"
  echo "== 4. ${L}"

  read -r t s _ <<<"$(log "hop rid ${L}base${R}")"; read -r st sp sk _ <<<"$(span "${s}")"
  check "${L}" "基本：log 有 trace_id、span_id，對得到 Server span（最上層）" "$([[ ${t} != - ]] && echo y) ${st} ${sp} ${sk}" "y ${t} - Server"

  read -r t s _ <<<"$(log "hop rid ${L}tpok${R}")"; read -r st sp sk _ <<<"$(span "${s}")"
  check "${L}" "上游 traceparent 正常：沿用上游 trace_id，Server span 的 parent 是上游" "${t} ${st} ${sp}" "${TP_T[${L}ok]} ${TP_T[${L}ok]} ${TP_P[${L}ok]}"

  read -r t s _ <<<"$(log "hop rid ${L}tpbad${R}")"; read -r st sp sk _ <<<"$(span "${s}")"
  check "${L}" "上游 traceparent 格式錯：當作沒有，自己開新 trace" "$([[ ${t} =~ ^[0-9a-f]{32}$ ]] && echo y) ${st} ${sp} ${sk}" "y ${t} - Server"

  read -r t1 _ <<<"$(log "hop rid ${L}tpoff${R} svc ${a}")"; read -r t2 _ m2 <<<"$(log "hop rid ${L}tpoff${R} svc ${b}")"
  T="${TP_T[${L}off]}"
  check "${L}" "上游未取樣（00）：log 仍帶上游 trace_id、往下傳 00，ES 沒有 span" \
    "${t1} ${t2} $([[ ${m2} == *"-${T}-"*"-00"* ]] && echo 00) $(spans_of "${T}")" "${T} ${T} 00 0"

  prev="" ids="" bad=""
  for svc in "${a}" "${b}" "${other}"; do
    read -r t s m <<<"$(log "hop rid ${L}chain${R} svc ${svc}")"; ids+="${t} "
    read -r st sp sk _ <<<"$(span "${s}")"
    [[ "${sk}" == Server && "${st}" == "${t}" ]] || bad+="${svc}:Server span 對不上 "
    if [[ -n "${prev}" ]]; then
      read -r ct cp ck _ <<<"$(span "${sp}")"
      [[ "${ck}" == Client && "${cp}" == "${prev}" ]] || bad+="${svc}:parent 不是上一跳的 Client span "
      [[ "${m}" == *"-${sp}-"* ]] || bad+="${svc}:收到的 traceparent 不是上一跳的 Client span "
    fi
    prev="${s}"
  done
  read -r -a idarr <<<"${ids}"
  check "${L}" "兩跳 ${a}→${b}→${other}：trace_id 一致、每一跳的 parent 是上一跳的 Client span" \
    "$([[ ${idarr[0]} != - && ${idarr[0]} == "${idarr[1]}" && ${idarr[1]} == "${idarr[2]}" ]] && echo 一致) ${bad:-parent 正確}" "一致 parent 正確"

  for w in $([[ ${L} == java ]] && echo "pool cf thread" || echo "async taskrun pool thread"); do
    read -r pt ps _ <<<"$(log "parent rid ${L}${w}${R}")"; read -r ct cs _ <<<"$(log "child rid ${L}${w}${R}")"
    read -r lt ls _ <<<"$(log "leaf rid ${L}${w}${R}")"; read -r _ lp _ <<<"$(span "${ls}")"; read -r _ cp ck _ <<<"$(span "${lp}")"
    got="$([[ ${ct} == "${pt}" && ${cs} == "${ps}" ]] && echo 子工作log同trace || echo "子工作log=${ct}")"
    got+=" $([[ ${lt} == "${pt}" && ${ck} == Client && ${cp} == "${ps}" ]] && echo 呼叫掛在請求底下 || echo "呼叫的trace=${lt},Client的parent=${cp}")"
    if [[ ${L} == java && ${w} == thread ]]; then
      # 自己 new Thread：agent 不幫忙帶 context（sdk/standards/tracing.md 2-4 要自己 wrap）
      check "${L}" "子工作 ${w}：log 沒有 trace_id，呼叫自成新 trace" \
        "${ct} $([[ ${lt} != - && ${lt} != "${pt}" && $(root_of "${lt}") == Client* ]] && echo 新trace)" "- 新trace"
    else
      check "${L}" "子工作 ${w}：log 與呼叫都在同一個 trace" "${got}" "子工作log同trace 呼叫掛在請求底下"
    fi
  done

  read -r tt _ tm <<<"$(es "${ES_URL}/k8s-*/_search?size=1&sort=@timestamp:desc" -d "{\"query\":{\"bool\":{\"filter\":[
    {\"match_phrase\":{\"message\":\"tick rid\"}},{\"term\":{\"service.name\":\"${a}\"}},{\"range\":{\"@timestamp\":{\"gte\":\"now-10m\",\"lte\":\"now-30s\"}}}]}}}" \
    | jq -r '.hits.hits[0]._source // {} | "\(.trace.id // "-") \(.span.id // "-") \(.message // "-")"')"
  trid="$(grep -oE 'tick[a-z0-9]+' <<<"${tm}" || echo none)"
  read -r lt _ <<<"$(log "leaf rid ${trid}")"
  check "${L}" "背景排程：log 沒有 trace_id，呼叫自成 trace（最上層是 Client）" "${tt} $([[ ${lt} != - ]] && root_of "${lt}" | cut -d' ' -f1)" "- Client"

  read -r pt ps _ <<<"$(log "produce rid ${L}kafka${R}")"; read -r ct cs _ <<<"$(log "consumed rid ${L}kafka${R}")"
  read -r lt _ <<<"$(log "leaf rid ${L}kafka${R}")"
  if [[ ${L} == java ]]; then
    read -r _ _ ck cn <<<"$(span "${cs}")"
    check "${L}" "Kafka：消費端 log 帶生產者的 trace_id（process span 接在 publish 底下）" \
      "$([[ ${pt} != - && ${ct} == "${pt}" && ${lt} == "${pt}" ]] && echo 同trace) ${ck}" "同trace Consumer"
  else
    check "${L}" "Kafka：消費端 log 沒有 trace_id，處理時的呼叫自成 trace" \
      "$([[ ${pt} != - ]] && echo 生產端有) ${ct} $([[ ${lt} != - && ${lt} != "${pt}" ]] && root_of "${lt}" | cut -d' ' -f1)" "生產端有 - Client"
  fi
done

[[ -z "${KEEP:-}" ]] && kubectl delete namespace "${ns}" --wait=false >/dev/null
pf_stop
cluster_unlock
echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
