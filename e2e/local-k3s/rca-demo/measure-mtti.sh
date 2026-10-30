#!/usr/bin/env bash
# MTTI（根因初篩時間）：從注入事故到 aiops-alerts 那件帶 report，花多久（docs/pdca/meta/2026-10-poc-acceptance.md 的 KPI，目標 < 3 分鐘）。
#
# 用法（前提：run-rca-demo.sh up 跑過、ES 連得到；這支自己在背景跑 tick，不要另外開 loop）：
#   measure-mtti.sh <incident|incident-external> [次數，預設 3]
# 環境變數：ES_URL、RCA_DEMO_STATE、LLM_ENV_FILE 跟 run-rca-demo.sh 一樣；
#   TICK_EVERY    每隔幾秒 tick 一次（預設 60，正式排程的 CronJob 是每分鐘）
#   MTTI_TIMEOUT  一次最多等幾秒（預設 600）
#   MTTI_OUT      每一次的結果追加一行 TSV 到這個檔（預設不存）
#   MTTI_QUIET    兩次之間離上次 heal 至少幾秒（預設 900：根因分析的「平常」是出事前 10～15 分鐘，太近會把上一次當平常）
# 每一次：
#   1. 等上一次的影響過去（rca-mes-api 最近 6 分鐘沒有新的規則異常、離上次 heal 滿 MTTI_QUIET），
#      再把還開著的那件強制 resolved——不然新異常會併進開著的那件、不會再產一次報告（正式環境是 close_after 30 分鐘才結束）
#   2. 記注入時間、注入
#   3. 每 10 秒查 ES：第一筆 detected_at ≥ 注入時間的規則異常（aiops-anomalies）、第一次看到 report_at ≥ 注入時間且帶 report 的那件（aiops-alerts）
#   4. heal，印這一次各階段的時間
# 階段（都減掉注入時間）：
#   異常寫入  aiops-anomalies 的 detected_at（規則真的寫進去的時間；@timestamp 是取整到分鐘的時槽，不是寫入時間）
#   開件      aiops-alerts 的 report_at（investigator alert 那一輪開始的時間；新開的那件同一輪就跑根因分析）
#   報告可見  輪詢第一次看到那件帶 report 的時間（根因分析＋LLM 跑完、寫進 ES；含最多 10 秒的輪詢誤差）＝ MTTI
#   aiops-alerts 的 @timestamp 故意往前 1 分鐘寫（docs/pdca/phase3-locate/2026-10-alert-flap.md），不能拿來算。
# 最後印每個階段的中位數。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
demo="${here}/run-rca-demo.sh"
ES_URL="${ES_URL:-http://127.0.0.1:29200}"
ES_PASSWORD=changeme-test-only
STATE="${RCA_DEMO_STATE:-${HOME}/.rca-demo}"
SVC=rca-mes-api
TICK_EVERY="${TICK_EVERY:-60}"
MTTI_TIMEOUT="${MTTI_TIMEOUT:-600}"
POLL=10
# 上一次 heal 之後至少等這麼久（而且最近 6 分鐘沒有新的規則異常）才注入下一次：規則的 5 分鐘窗 6 分鐘就清掉，
# 但根因分析的「平常」是出事前 ref_gap（10 分鐘）再往前一個窗（5 分鐘），間隔不到 15 分鐘，第 2 次起的報告會把上一次的事故當平常
# （實測：第 2 次「平常 4.41 s」）。只要 MTTI 數字、不看報告內容的話可以 MTTI_QUIET=360
QUIET="${MTTI_QUIET:-900}"
WINDOW=360         # 規則窗清掉：最近這麼多秒沒有新的規則異常

scenario="${1:-}"
runs="${2:-3}"
case "${scenario}" in
  incident) heal=heal ;;
  incident-external) heal=heal-external ;;
  *) sed -n '2,21p' "$0"; exit 1 ;;
esac

es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
epoch() { date -u -d "$1" +%s; }          # ES 回的 ISO 時間 → epoch 秒
fmt() { printf '%d:%02d' $(( $1 / 60 )) $(( $1 % 60 )); }
log() { echo "$(date -u +%H:%M:%SZ) $*"; }

# 最近幾秒內這個服務的規則異常有幾筆（detected_at 是真的寫入時間）
recent_anomalies() {
  es "${ES_URL}/aiops-anomalies/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"level\":\"rule\"}},
      {\"term\":{\"keys.resource.attributes.service.name\":\"${SVC}\"}},{\"range\":{\"detected_at\":{\"gte\":\"now-${1}s\"}}}]}}}" | jq -r .count
}
# 第一筆 detected_at ≥ 注入時間的規則異常：印 "detected_at<TAB>series<TAB>時槽"
first_anomaly() {
  es "${ES_URL}/aiops-anomalies/_search" -d "{\"size\":1,\"sort\":[{\"detected_at\":\"asc\"}],\"_source\":[\"detected_at\",\"series\",\"@timestamp\"],
      \"query\":{\"bool\":{\"filter\":[{\"term\":{\"level\":\"rule\"}},{\"term\":{\"keys.resource.attributes.service.name\":\"${SVC}\"}},
      {\"range\":{\"detected_at\":{\"gte\":\"$1\"}}}]}}}" | jq -r '.hits.hits[0]._source // empty | [.detected_at, .series, .["@timestamp"]] | @tsv'
}
# report_at ≥ 注入時間、帶 report 的那件：印 "report_at<TAB>alert_id<TAB>started_at"
first_report() {
  es "${ES_URL}/aiops-alerts/_search" -d "{\"size\":1,\"sort\":[{\"report_at\":\"asc\"}],\"_source\":[\"report_at\",\"alert_id\",\"started_at\",\"report\"],
      \"query\":{\"bool\":{\"filter\":[{\"term\":{\"service\":\"${SVC}\"}},{\"term\":{\"category\":\"service\"}},
      {\"range\":{\"report_at\":{\"gte\":\"$1\"}}}]}}}" | jq -r '.hits.hits[0]._source // empty | select((.report // "") != "") | [.report_at, .alert_id, .started_at] | @tsv'
}
# 把這個服務還開著的件強制結束（量測用；正式環境由 close_after 決定）
force_resolve() {
  local t; t="$(iso "$(date +%s)")"
  es "${ES_URL}/aiops-alerts/_update_by_query?refresh=true&conflicts=proceed" -d "{\"script\":{\"source\":
      \"ctx._source.status='resolved'; ctx._source.resolved_at=params.t; ctx._source['@timestamp']=params.t;\",\"params\":{\"t\":\"${t}\"}},
      \"query\":{\"bool\":{\"filter\":[{\"term\":{\"status\":\"open\"}},{\"term\":{\"service\":\"${SVC}\"}}]}}}" | jq -r '"   強制結束上一件：\(.updated) 件"'
}

mkdir -p "${STATE}"
ticklog="${STATE}/mtti-tick.log"
healed=1
tick_pid=""
cleanup() {
  [[ -n "${tick_pid}" ]] && kill "${tick_pid}" 2>/dev/null || true
  [[ "${healed}" == 1 ]] || "${demo}" "${heal}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
log "情境 ${scenario}，量 ${runs} 次；tick 每 ${TICK_EVERY} 秒（輸出在 ${ticklog}）"
"${demo}" tick >> "${ticklog}" 2>&1 || true        # 先跑一輪：上一次還沒退出窗的異常現在就寫進去，下面的檢查才準
( while :; do "${demo}" tick || true; sleep "${TICK_EVERY}"; done ) >> "${ticklog}" 2>&1 &
tick_pid=$!

results=()      # 每一次：d_anom d_open d_seen
last_heal=0
for ((i = 1; i <= runs; i++)); do
  # 1. 等上一次退出規則的 5 分鐘窗、離開根因分析的「平常」範圍
  while :; do
    n="$(recent_anomalies "${WINDOW}")"
    since=$(( $(date +%s) - last_heal ))
    [[ "${n}" == 0 && ${since} -ge ${QUIET} ]] && break
    log "等上一次的影響過去（最近 ${WINDOW} 秒的規則異常 ${n} 筆、離上次 heal ${since} 秒，要滿 ${QUIET}）…"
    sleep 30
  done
  force_resolve
  # 2. 注入
  t0=$(date +%s); t0iso="$(iso "${t0}")"
  "${demo}" "${scenario}" | sed 's/^/   /'
  healed=0
  log "第 ${i} 次：注入 ${t0iso}"
  # 3. 輪詢
  t_anom=""; t_open=""; t_seen=""; series=""; aid=""
  while :; do
    now=$(date +%s)
    if [[ -z "${t_anom}" ]]; then
      row="$(first_anomaly "${t0iso}")"
      if [[ -n "${row}" ]]; then
        t_anom="$(epoch "$(cut -f1 <<< "${row}")")"; series="$(cut -f2 <<< "${row}")"
        log "   異常寫入 +$(fmt $(( t_anom - t0 )))（${series}，時槽 $(cut -f3 <<< "${row}")）"
      fi
    fi
    row="$(first_report "${t0iso}")"
    if [[ -n "${row}" ]]; then
      t_open="$(epoch "$(cut -f1 <<< "${row}")")"; aid="$(cut -f2 <<< "${row}")"; t_seen="${now}"
      log "   開件（alert 那一輪開始）+$(fmt $(( t_open - t0 )))、報告可見 +$(fmt $(( t_seen - t0 )))（${aid}）"
      break
    fi
    if (( now - t0 >= MTTI_TIMEOUT )); then log "   ${MTTI_TIMEOUT} 秒內沒有報告，這次算失敗"; break; fi
    sleep "${POLL}"
  done
  # 4. heal
  "${demo}" "${heal}" | sed 's/^/   /'; healed=1; last_heal=$(date +%s)
  if [[ -n "${t_seen}" ]]; then
    results+=("$(( ${t_anom:-$t0} - t0 )) $(( t_open - t0 )) $(( t_seen - t0 ))")
    if [[ -n "${MTTI_OUT:-}" ]]; then
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${scenario}" "${i}" "${t0iso}" "$(( ${t_anom:-$t0} - t0 ))" "$(( t_open - t0 ))" \
        "$(( t_seen - t0 ))" "${series}" "${aid}" "${TICK_EVERY}" >> "${MTTI_OUT}"
    fi
  fi
done

median() { sort -n | awk '{a[NR]=$1} END{if(NR==0){print "-";exit} m=(NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2; printf "%d:%02d\n", m/60, m%60}'; }
col() { local c="$1"; shift; printf '%s\n' "$@" | awk -v c="${c}" '{print $c}' | median; }
echo
echo "情境 ${scenario}：${#results[@]}/${runs} 次有報告（tick 每 ${TICK_EVERY} 秒；時間都是「注入後幾分幾秒」）"
echo "| 次 | 異常寫入 | 開件 | 報告可見（MTTI） |"
echo "|---|---|---|---|"
k=0
for r in "${results[@]}"; do
  k=$((k + 1)); set -- ${r}
  echo "| ${k} | $(fmt "$1") | $(fmt "$2") | $(fmt "$3") |"
done
if (( ${#results[@]} > 0 )); then
  echo "| 中位數 | $(col 1 "${results[@]}") | $(col 2 "${results[@]}") | $(col 3 "${results[@]}") |"
fi
