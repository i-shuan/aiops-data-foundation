#!/usr/bin/env bash
# Grafana 實際寄出通知的測試（docs/pdca/meta/2026-10-pin-es8-grafana13.md）：用 engine/configs/grafana 的告警規則、contact point、policy，
# 往 aiops-alerts 寫「像 investigator alert 寫的」文件，看假的 webhook 收到什麼：
#   A 心跳不在 → 心跳規則 firing，送平台　　　　B 補一筆心跳 → resolved
#   C 三件 open（dept D3120、D1110、沒有 dept）→ 各自 firing，送 it-oncall、imc-oncall、platform，一件一則
#   D 三件改 resolved → 各自 resolved
# 用法：GRAFANA_CONTAINER=gf13 ES_URL=http://localhost:9201 e2e/local-k3d/run-grafana-notify.sh
# 前提：Grafana 容器已經用 engine/configs/grafana 的設定檔 provisioning 起來，通知網址指到本機 NOTIFY_PORT（預設 18099）的 /it、/imc、/platform；
#       ES 有 aiops-alerts，Grafana 的 data source 讀得到。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
G="${GRAFANA_CONTAINER:-gf13}"; ES="${ES_URL:-http://localhost:9201}"; PORT="${NOTIFY_PORT:-18099}"
A=elastic:changeme-test-only
LOG="${NOTIFY_LOG:-/tmp/notify.log}"
ts() { date -u +%H:%M:%S; }
es() { curl -sS -m 30 -u "${A}" -H 'Content-Type: application/json' "$@"; }
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
# 等 LOG 出現符合的行（grep -E），最多 $2 秒
wait_log() { local i; for (( i=0; i<$2; i+=5 )); do grep -qE "$1" "${LOG}" && return 0; sleep 5; done; return 1; }
put() {  # put <id> <status> <dept|-> <severity>
  local dept=""; [[ "$3" != "-" ]] && dept="\"dept\":\"$3\","
  es -X PUT "${ES}/aiops-alerts/_doc/$1?refresh=true" -d "{\"@timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"alert_id\":\"$1\",${dept}\"service\":\"svc-$1\",
    \"group\":\"g\",\"severity\":\"$4\",\"status\":\"$2\",\"summary\":\"測試事件 $1\",\"report_url\":\"http://r/$1\",\"category\":\"service\"}" >/dev/null
}
beat() { es -X PUT "${ES}/aiops-alerts/_doc/heartbeat?refresh=true" -d "{\"@timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"alert_id\":\"heartbeat\",\"category\":\"heartbeat\",\"status\":\"ok\",\"service\":\"aiops\"}" >/dev/null; }
# 模擬 investigator alert 每分鐘重寫一次 @timestamp（Grafana 只看最近 5 分鐘）
tick() { beat_on="$1"; for id in "${open_ids[@]:-}"; do [[ -n "${id}" ]] && put "${id}" open "${depts[${id}]}" "${sev[${id}]}"; done; [[ "${beat_on}" == 1 ]] && beat; true; }

# 假 webhook：沿用 rca-demo 的，只換 port
pkill -f 'notify-sink-k3d' 2>/dev/null || true
sed "s/8000/${PORT}/" "${here}/../local-k3s/rca-demo/webhook.py" > /tmp/notify-sink-k3d.py
: > "${LOG}"
nohup python3 /tmp/notify-sink-k3d.py >"${LOG}" 2>&1 &
sink=$!
trap 'kill ${sink} 2>/dev/null || true' EXIT

# 索引要用 investigator alert 自己建的對應（alert_id 等是 keyword，Grafana 才能依它分組）：不能讓 ES 動態對應
es -X DELETE "${ES}/aiops-alerts" >/dev/null || true
mapping="$(cd "${here}/../../engine" && python3 -c 'import json; from investigator.cmd_alert import ALERT_MAPPING; print(json.dumps(ALERT_MAPPING))')"
es -X PUT "${ES}/aiops-alerts" -d "${mapping}" | grep -q '"acknowledged":true'
docker restart "${G}" >/dev/null
until curl -sf "http://localhost:3001/api/health" >/dev/null; do sleep 2; done
echo "== $(ts) Grafana $(curl -s http://localhost:3001/api/health | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])') 起來；aiops-alerts 已清空（沒有心跳）"

declare -A depts sev; open_ids=()
echo "== A 心跳不在 → firing（規則 for 2m、每分鐘評估，預期 3～5 分鐘）"
if wait_log 'firing \| AIOps 停了' 420; then echo "  PASS  心跳規則 firing 送出（$(grep -c 'firing | AIOps 停了' "${LOG}") 則）"; else echo "  FAIL  7 分鐘內沒收到心跳 firing"; fail=1; fi
check "心跳 firing 送到平台（/platform）" "$(grep -B1 'firing | AIOps 停了' "${LOG}" | grep -c ' /platform ')" "1"

echo "== B 補一筆心跳 → resolved"
beat
if wait_log 'resolved \| AIOps 停了' 300; then echo "  PASS  心跳 resolved 送出"; else echo "  FAIL  5 分鐘內沒收到心跳 resolved"; fail=1; fi

echo "== C 三件 open"
open_ids=(inc-it inc-imc inc-none); depts=([inc-it]=D3120 [inc-imc]=D1110 [inc-none]=-); sev=([inc-it]=critical [inc-imc]=warning [inc-none]=warning)
tick 1
for _ in $(seq 1 5); do sleep 60; tick 1; grep -q 'firing | AIOps 事件 | svc-inc-none' "${LOG}" && grep -q 'firing | AIOps 事件 | svc-inc-it' "${LOG}" && grep -q 'firing | AIOps 事件 | svc-inc-imc' "${LOG}" && break; done
for k in it imc none; do
  echo "  $(grep -c "firing | AIOps 事件 | svc-inc-$k" "${LOG}") 則 firing：inc-$k"
done
route() { awk -v s="svc-inc-$1" '/^[0-9:]+Z \//{p=$2} index($0,"firing | AIOps 事件 | " s){print p; exit}' "${LOG}"; }
check "D3120 的件送 it-oncall"  "$(route it)"   "/it"
check "D1110 的件送 imc-oncall" "$(route imc)"  "/imc"
check "沒有 dept 的件送平台"    "$(route none)" "/platform"
check "一件一則（三件各一則 firing，不合併）" "$(grep -c 'firing | AIOps 事件' "${LOG}")" "3"

echo "== D 三件改 resolved"
for id in inc-it inc-imc inc-none; do put "${id}" resolved - warning; done; open_ids=()
for _ in $(seq 1 6); do sleep 60; beat; [[ "$(grep -c 'resolved | AIOps 事件' "${LOG}")" -ge 3 ]] && break; done
check "三件各收到一則 resolved" "$(grep -c 'resolved | AIOps 事件' "${LOG}")" "3"

echo; echo "== 收到的全部通知"; cat "${LOG}"
exit "${fail}"
