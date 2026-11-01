#!/usr/bin/env bash
# ⑧⑨ 串起來實跑：真資料走完整條，量事故到通知的延遲，再對這件真告警送老師傅回饋。
#
#   A. run-rca-demo.sh up：發 key、模擬服務（rca-mes-api → rca-wip-worker → Oracle）、Grafana、notify-sink
#   B. 確認 Grafana 載入了儀表板、告警規則、contact point
#   C. 正常跑 3 分鐘（每分鐘一輪 tick：trace-merge → rules → investigator alert，模擬排程器）
#   D. 製造事故（wip-worker 查 Oracle 1.5 秒、connection pool timeout）：記第一筆異常、第一件 open 的事故；根因報告要指到 Oracle
#   E. Grafana 通知到 notify-sink；印四段延遲（事故 → 偵測 → 開件 → 通知），> 10 分鐘算 FAIL
#   F. 回饋網頁（feedback_web）對這件告警送「真的」：aiops-incidents 變 confirmed、aiops-feedback 有紀錄
#   G. heal
#
# 前提：run-e2e.sh 跑過（測試 ES、Collector、測試 namespace 都在）。約 20 分鐘。
# 用法：ES_URL=http://127.0.0.1:29200 e2e/k8s/rca-demo/run-live-chain.sh      （ES 連不到時自己 port-forward es-test）
# 跑完 Grafana（localhost:3300）和模擬服務留著，pipeline/run-alert-flap.py 接著要用；清掉：kubectl delete namespace aiops-demo
set -u
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../../.." && pwd)"
export PYTHONUTF8=1 ES_URL="${ES_URL:-http://127.0.0.1:29200}"
ES_PASSWORD=changeme-test-only
D="${here}/run-rca-demo.sh"
fail=0
pass() { echo "  PASS  $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
keeper=""; web=""
# ES 已經連得到（例如 run-seq.sh 開著 port-forward）就沿用；沒有才自己開，自己開的才在結束時關
if ! curl -s -o /dev/null -m 3 -u "elastic:${ES_PASSWORD}" "${ES_URL}"; then
  port="$(sed -E 's#^https?://[^:/]+:([0-9]+).*#\1#' <<<"${ES_URL}")"
  ( while true; do kubectl -n es-test port-forward svc/elasticsearch "${port}:9200" >/dev/null 2>&1; sleep 2; done ) &
  keeper=$!
  sleep 4
fi
trap 'kill ${keeper} ${web} 2>/dev/null; [[ -n "${keeper}" ]] && pkill -f "port-forward svc/elasticsearch ${port}:9200" 2>/dev/null' EXIT
STATE="${RCA_DEMO_STATE:-${HOME}/.rca-demo}"
start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== A. up（key、模擬服務、Grafana、notify-sink）"
bash "$D" up || { echo "up 失敗"; exit 1; }
for _ in $(seq 60); do curl -s -o /dev/null -m 3 http://localhost:3300/api/health && break; sleep 3; done
echo "== B. Grafana 載入了什麼"
G="curl -sS -m 10 -u admin:${ES_PASSWORD} http://localhost:3300"
dash="$($G/api/search?type=dash-db | jq -r '[.[].title] | join(" / ")')"
[[ -n "${dash}" ]] && pass "儀表板：${dash}" || bad "沒有儀表板"
rules="$($G/api/v1/provisioning/alert-rules | jq -r '[.[].title] | join(" / ")')"
[[ "${rules}" == *heartbeat* || "${rules}" == *AIOps* ]] && pass "告警規則：${rules}" || bad "告警規則：[${rules}]"
cps="$($G/api/v1/provisioning/contact-points | jq -r '[.[].name] | unique | join(" / ")')"
[[ -n "${cps}" ]] && pass "contact point：${cps}" || bad "沒有 contact point"
echo "   data source：$($G/api/datasources | jq -r '[.[].name] | join(" / ")')"

echo "== C. 正常 3 分鐘（每分鐘 tick）"
bash "$D" loop 3 2>&1 | grep -vE '^\s*$' | tail -12
echo "== D. 事故：wip-worker 查 Oracle 1.5 秒、connection pool timeout"
bash "$D" incident
t_inc=$(date +%s); t_inc_iso="$(date -u -d "@${t_inc}" +%Y-%m-%dT%H:%M:%SZ)"
t_anom=""; t_open=""
# 每分鐘一輪（模擬排程器：trace-merge → rules → alert），每輪後看一次：第一筆異常、第一件 open 的事故
for m in $(seq 8); do
  bash "$D" tick 2>&1 | grep -E 'tick|觸發|件|錯誤|Error' | tail -4
  es -X POST "$ES_URL/aiops-anomalies,aiops-alerts/_refresh?ignore_unavailable=true" >/dev/null
  if [[ -z "${t_anom}" ]]; then
    first="$(es "$ES_URL/aiops-anomalies/_search" -d "{\"size\":200,\"query\":{\"range\":{\"detected_at\":{\"gte\":\"${t_inc_iso}\"}}}}" \
      | jq -r '[.hits.hits[]._source | select(((.keys["resource.attributes.service.name"] // .keys.service // "") | tostring | startswith("rca-"))) | .detected_at] | min // empty')"
    [[ -n "${first}" ]] && t_anom="$(date -u -d "${first}" +%s)"
  fi
  if [[ -z "${t_open}" ]]; then
    n="$(es "$ES_URL/aiops-alerts/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"status\":\"open\"}},{\"prefix\":{\"service\":\"rca-\"}}]}}}" | jq -r '.count // 0')"
    (( n > 0 )) && t_open=$(date +%s)
  fi
  [[ -n "${t_anom}" && -n "${t_open}" && ${m} -ge 3 ]] && break
  sleep 45
done
es -X POST "$ES_URL/aiops-alerts/_refresh" >/dev/null
al="$(es "$ES_URL/aiops-alerts/_search" -d "{\"size\":5,\"sort\":[{\"started_at\":\"desc\"}],\"query\":{\"bool\":{\"filter\":[{\"term\":{\"status\":\"open\"}},{\"range\":{\"started_at\":{\"gte\":\"${start}\"}}}]}}}")"
n_open="$(jq -r '.hits.total.value' <<<"${al}")"
(( n_open > 0 )) && pass "⑤⑥ aiops-alerts 有 ${n_open} 件 open：$(jq -r '[.hits.hits[]._source | "\(.service // .services)：\(.title)"] | join("；")' <<<"${al}")" || bad "⑤⑥ 沒有 open 的件"
aid="$(jq -r '.hits.hits[0]._source.alert_id // empty' <<<"${al}")"
rep="$(jq -r '.hits.hits[0]._source.report // ""' <<<"${al}")"
[[ "${rep}" == *oracle* || "${rep}" == *Oracle* ]] && pass "⑦ 報告指到 Oracle 那段" || bad "⑦ 報告沒提到 Oracle（前 300 字：${rep:0:300}）"

echo "== E. ⑧ Grafana 通知（notify-sink，最多等 5 分鐘）"
got=""; fire_hms=""
for _ in $(seq 30); do
  got="$(kubectl -n aiops-demo logs deploy/notify-sink --since-time="${start}" 2>/dev/null | tr -d '\r')"
  # 通知 log：「HH:MM:SSZ /路徑 firing」下面每個 alert 一行「firing | 名稱 | 服務 | dept | 摘要」；找第一則帶 rca- 服務的
  fire_hms="$(awk '/^[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z /{h=$1} /^ +firing \|.*\| rca-/{print h; exit}' <<<"${got}")"
  [[ -n "${fire_hms}" ]] && break
  bash "$D" tick >/dev/null 2>&1; sleep 10
done
echo "${got}" | head -12 | sed 's/^/   /'
echo "== E2. 延遲：事故開始 → 偵測 → 開件 → Grafana 通知（每分鐘一輪 tick 模擬排程器，開件以 tick 後第一次看到為準，誤差約 ±1 分鐘）"
t_fire=""; [[ -n "${fire_hms}" ]] && t_fire="$(date -u -d "$(date -u +%F) ${fire_hms%Z}" +%s)"
lat() { [[ -n "$2" ]] && echo "$(( $2 - t_inc ))" || echo ""; }
printf '   %-34s %s\n' "事故開始（wip-worker 查 Oracle 變 1.5 秒）" "0 秒（$(date -u -d "@${t_inc}" +%H:%M:%SZ)）"
printf '   %-34s %s\n' "→ 第一筆異常寫進 aiops-anomalies"  "${t_anom:+$(lat x "${t_anom}") 秒}${t_anom:-沒有}"
printf '   %-34s %s\n' "→ aiops-alerts 有 open 的事故"       "${t_open:+$(lat x "${t_open}") 秒}${t_open:-沒有}"
printf '   %-34s %s\n' "→ Grafana 通知到 notify-sink"        "${t_fire:+$(lat x "${t_fire}") 秒（${fire_hms}）}${t_fire:-沒有}"
if [[ -n "${t_fire}" ]]; then
  (( t_fire - t_inc <= 600 )) && pass "⑧ 事故開始到通知到 ≤ 10 分鐘（$(( t_fire - t_inc )) 秒）" || bad "⑧ 事故開始到通知花了 $(( t_fire - t_inc )) 秒（> 10 分鐘）"
else bad "⑧ 沒有量到通知時間"; fi
grep -qE '^[0-9:]+Z /it firing' <<<"${got}" && pass "⑧ IT 的 webhook 收到 firing" || bad "⑧ IT 的 webhook 沒收到 firing"
grep -qE 'firing \| .*rca-' <<<"${got}" && pass "⑧ 通知帶服務名（一件一則）" || bad "⑧ 通知裡沒有 rca- 服務"

echo "== F. ⑨ 回饋網頁：對這件真告警送「真的」"
if [[ -n "${aid}" ]]; then
  fblog="$(mktemp)"
  (cd "${repo}/aiops-engine/code" && ES_API_KEY="$(cat "${STATE}/engine-key")" FEEDBACK_PORT=18080 python3 -m feedback_web >"${fblog}" 2>&1) &
  web=$!
  for _ in $(seq 30); do curl -s -o /dev/null -m 2 http://localhost:18080/health && break; sleep 1; done
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:18080/feedback?alert=${aid}")"
  [[ "${code}" == 200 ]] && pass "⑨ 回饋表單打得開（${aid}）" || bad "⑨ 回饋表單 HTTP ${code}（log：${fblog}）"
  r="$(curl -sS -m 30 -H 'Content-Type: application/json' http://localhost:18080/api/feedback -d "{\"alert_id\":\"${aid}\",\"verdict\":\"true_positive\",\"by\":\"e2e\",\"root_cause_category\":\"db\",\"root_cause\":\"Oracle 連線池逾時\",\"action\":\"重啟連線池\",\"how_detected\":\"trace 指到 Oracle 那段、log 有 connection pool timeout\"}")"
  echo "   回應：${r}"
  es -X POST "$ES_URL/aiops-incidents,aiops-feedback/_refresh" >/dev/null
  st="$(es "$ES_URL/aiops-incidents/_doc/${aid}" | jq -r '._source.status + "/" + (._source.root_cause_category // "")')"
  [[ "${st}" == "confirmed/db" ]] && pass "⑨ aiops-incidents 變 confirmed、根因 db" || bad "⑨ aiops-incidents：${st}"
  nf="$(es "$ES_URL/aiops-feedback/_count" -d "{\"query\":{\"term\":{\"alert_id\":\"${aid}\"}}}" | jq -r .count)"
  (( nf > 0 )) && pass "⑨ aiops-feedback 有 ${nf} 筆" || bad "⑨ aiops-feedback 沒有這件的回饋"
else bad "⑨ 沒有 alert_id 可以回饋"; fi

echo "== G. 收尾：heal"
bash "$D" heal
echo; (( fail == 0 )) && echo "全部通過" || echo "有項目失敗"
exit "${fail}"
