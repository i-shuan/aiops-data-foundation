#!/usr/bin/env bash
# engine 第一次在 K8s 上排程跑：aiops-engine/helmchart 的第一階段 4 個 CronJob（rules、detect、alert、train）
# 用 helm 裝到 namespace aiops-engine-test（values-test.yaml：連 es-test、本機 image、不裝回饋網頁），跑 RUN_MINUTES 分鐘再看。
#
# 每一項印 PASS／FAIL：
#   1. rules、detect、alert 每分鐘都有排、都成功、沒漏掉任何一分鐘；跑最久的那次 ≤ activeDeadlineSeconds 的 60%
#   2. 同一個 CronJob 任何時候最多 1 個在跑（每 15 秒看一次）
#   3. 心跳（aiops-alerts/_doc/heartbeat）每分鐘都有更新；rules 的觸發寫進 aiops-anomalies
#   4. train：手動觸發成功；排程改成 2 分鐘後（台北時間）那一分鐘真的跑（timeZone 有效），再改回 02:00
#   5. Forbid：alert 先睡 150 秒，下一、兩分鐘不會再開一個
#   6. activeDeadlineSeconds：detect 卡住（睡 120 秒）在 50 秒被砍（DeadlineExceeded）
#   7. 失敗會重試：ES key 壞掉時每分鐘的 3 個失敗、修好後下一分鐘就成功；train 連不到 ES 時 Job 內重試 2 次
# 跑完刪掉 namespace、作廢這次發的 API key。
#
# 前提：run-e2e.sh 跑過（es-test 的 ES、Collector 在跑）；Rancher Desktop（nerdctl 建 image 進 k8s.io）。
# 用法：e2e/k8s/run-schedule-e2e.sh        RUN_MINUTES=30（預設）；KEEP=1 跑完不刪；SKIP_BUILD=1 不重建 image
# 部署、改設定、刪除時拿 cluster-lock；觀察的那段時間放掉（CronJob 只讀 ES、寫 aiops-*，不影響別人）。
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
NS=aiops-engine-test
IMAGE=aiops-engine:p2-schedule
RUN_MINUTES="${RUN_MINUTES:-30}"
ES_PORT="${ES_PORT:-31515}"
ES_PASSWORD=changeme-test-only
ES="http://127.0.0.1:${ES_PORT}"
NERDCTL="${NERDCTL:-$(command -v nerdctl || echo "/c/Program Files/Rancher Desktop/resources/resources/win32/bin/nerdctl.exe")}"
work="$(mktemp -d)"
logs="${work}/logs"; mkdir -p "${logs}"
fail=0
pass() { echo "  PASS  $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
check() { if [[ "$2" == "$3" ]]; then pass "$1：${2}"; else bad "$1：得到 [$2]，預期 [$3]"; fi; }
ts() { date +%H:%M:%S; }

# kubeconfig 複製一份（不動 ~/.kube/config）；Windows 的 kubectl 要 C:/ 的路徑
cp "${KUBECONFIG_SRC:-${HOME}/.kube/config}" "${work}/kubeconfig"
export KUBECONFIG="$(cygpath -m "${work}/kubeconfig" 2>/dev/null || echo "${work}/kubeconfig")"
k() { kubectl --context rancher-desktop "$@"; }
kn() { k -n "${NS}" "$@"; }
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
# shellcheck source=cluster-lock.sh
source "${here}/cluster-lock.sh"
pf_pid=""
cleanup() {
  if [[ -n "${pf_pid}" ]]; then
    # Git Bash 裡 kill 有時停不掉原生的 kubectl.exe（實測留下一個 port-forward）：再用 Windows 的 PID 砍一次
    local win; win="$(cat "/proc/${pf_pid}/winpid" 2>/dev/null)"
    kill "${pf_pid}" 2>/dev/null; wait "${pf_pid}" 2>/dev/null
    [[ -n "${win}" ]] && command -v taskkill >/dev/null && taskkill //F //PID "${win}" >/dev/null 2>&1
    pf_pid=""
  fi
  cluster_unlock >/dev/null 2>&1 || true
}
trap cleanup EXIT
# cluster_lock／cluster_unlock 會改 EXIT trap，拿、放之後把自己的收尾（關 port-forward）裝回去
lock() { cluster_lock "p2-adjust-schedule"; trap cleanup EXIT; }
unlock() { cluster_unlock; trap cleanup EXIT; }

minute_of() { echo "${1##*-}"; }                         # CronJob 排的 Job 名稱尾巴＝排定時間（Unix 分鐘）
iso_min() { date -u -d "@$(( $1 * 60 ))" +%Y-%m-%dT%H:%MZ; }
sec() { date -u -d "$1" +%s; }

echo "== 0. image：nerdctl build 進 k8s.io（K8s 直接用，不用 registry）"
if [[ "${SKIP_BUILD:-}" != "1" ]]; then
  "${NERDCTL}" -n k8s.io build -t "${IMAGE}" "${repo}/engine" >"${work}/build.log" 2>&1 || { tail -20 "${work}/build.log"; exit 1; }
fi
"${NERDCTL}" -n k8s.io images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -F "${IMAGE}" | sed 's/^/   /'

echo "== 1. ES port-forward（${ES}）、發一把 baseline 權限的 key"
k -n es-test port-forward --address 127.0.0.1 svc/elasticsearch "${ES_PORT}:9200" >"${work}/pf.log" 2>&1 &
pf_pid=$!
for _ in $(seq 30); do es "${ES}/" >/dev/null 2>&1 && break; sleep 1; done
echo "   ES $(es "${ES}/" | jq -r '.version.number + "（" + .cluster_name + "）"')"
# 跟 elasticsearch-setup.sh 的「AIOps baseline key」同一組權限；只發 key，不動 index 結構。1 天後自己失效
key_json="$(es -X POST "${ES}/_security/api_key" -d '{
  "name": "aiops-engine-test", "expiration": "1d",
  "role_descriptors": {"aiops-baseline": {"index": [
    {"names": ["k8s-*", "metrics-*.otel-*", "traces-*.otel-*"], "privileges": ["read", "view_index_metadata", "read_failure_store"]},
    {"names": ["aiops-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}]}}}')"
KEY="$(jq -r .encoded <<<"${key_json}")"; KEY_ID="$(jq -r .id <<<"${key_json}")"
[[ -n "${KEY}" && "${KEY}" != null ]] || { echo "發 key 失敗：${key_json}"; exit 1; }

echo "== 2. 部署（拿鎖）"
lock
k delete namespace "${NS}" --ignore-not-found --wait=true >/dev/null
k create namespace "${NS}" >/dev/null
kn create secret generic aiops-secrets --from-literal=ES_API_KEY="${KEY}" --from-literal=LLM_API_KEY= --save-config >/dev/null
helm upgrade --install aiops "${repo}/aiops-engine/helmchart" -n "${NS}" --kube-context rancher-desktop -f "${repo}/aiops-engine/helmchart/values-test.yaml" | sed 's/^/   /'
unlock
kn get cronjob -o custom-columns='NAME:.metadata.name,SCHEDULE:.spec.schedule,TZ:.spec.timeZone,POLICY:.spec.concurrencyPolicy,DEADLINE:.spec.jobTemplate.spec.activeDeadlineSeconds,BACKOFF:.spec.jobTemplate.spec.backoffLimit' | sed 's/^/   /'

echo "== 3. train 先手動跑一次（detect 要有 baseline）"
kn create job train-now --from=cronjob/aiops-train >/dev/null
kn wait --for=condition=complete job/train-now --timeout=900s >/dev/null 2>&1
kn logs job/train-now >"${logs}/train-now.log" 2>&1
tn="$(kn get job train-now -o json)"
tn_ok="$(jq -r '[.status.conditions[]? | select(.status=="True") | .type] | join(",")' <<<"${tn}")"
tn_dur=$(( $(sec "$(jq -r '.status.completionTime // .status.startTime' <<<"${tn}")") - $(sec "$(jq -r .status.startTime <<<"${tn}")") ))
grep -E '學習區間|條序列' "${logs}/train-now.log" | head -12 | sed 's/^/   /'
if [[ "${tn_ok}" == *Complete* && ${tn_dur} -le 2160 ]]; then pass "train 手動觸發成功，${tn_dur} 秒（activeDeadlineSeconds 3600 的 60% 內）"
else bad "train 手動觸發：${tn_ok}，${tn_dur} 秒"; fi

echo "== 4. 觀察 ${RUN_MINUTES} 分鐘（放掉鎖；每 15 秒記 Job、心跳）"
declare -A seen      # Job 名稱 → 最後看到的狀態（TSV）
declare -A logged
declare -A maxact=([aiops-rules]=0 [aiops-detect]=0 [aiops-alert]=0 [aiops-train]=0)
: >"${work}/heartbeat.txt"
t0=$(date +%s); t_end=$(( t0 + RUN_MINUTES * 60 ))
poll() {
  local js
  js="$(kn get jobs -o json 2>/dev/null)" || return
  while IFS=$'\t' read -r name owner created start done ok failed active cond; do
    [[ -z "${name}" ]] && continue
    seen[${name}]="${owner}"$'\t'"${created}"$'\t'"${start}"$'\t'"${done}"$'\t'"${ok}"$'\t'"${failed}"$'\t'"${cond}"
    if [[ -z "${logged[${name}]:-}" && ( "${ok}" != 0 || "${failed}" != 0 ) ]]; then
      kn logs "job/${name}" --all-containers >"${logs}/${name}.log" 2>&1 && logged[${name}]=1
    fi
  done < <(jq -r '.items[] | [.metadata.name, (.metadata.ownerReferences[0].name // "manual"), .metadata.creationTimestamp,
      (.status.startTime // ""), (.status.completionTime // ""), (.status.succeeded // 0), (.status.failed // 0), (.status.active // 0),
      ((.status.conditions // []) | map(select(.status=="True")) | map(.type + ":" + (.reason // "")) | join(","))] | @tsv' <<<"${js}")
  # 同一個 CronJob 同時在跑的 Job 數（Job 有 active Pod、或還沒結束）
  while read -r owner n; do
    [[ -n "${owner}" && ${n} -gt ${maxact[${owner}]:-0} ]] && maxact[${owner}]=${n}
  done < <(jq -r '[.items[] | select((.status.conditions // []) | map(select(.status=="True" and (.type=="Complete" or .type=="Failed"))) | length == 0)
      | (.metadata.ownerReferences[0].name // "manual")] | group_by(.) | .[] | "\(.[0]) \(length)"' <<<"${js}")
  es -H "Authorization: ApiKey ${KEY}" "${ES}/aiops-alerts/_doc/heartbeat" 2>/dev/null | jq -r '._source.updated_at // empty' >>"${work}/heartbeat.txt"
}
while (( $(date +%s) < t_end )); do
  poll
  left=$(( (t_end - $(date +%s)) / 60 ))
  (( $(date +%s) % 300 < 15 )) && echo "   $(ts) 還有 ${left} 分鐘；看過 ${#seen[@]} 個 Job"
  sleep 15
done
poll
t1=$(date +%s)
printf '%s\n' "${!seen[@]}" | sort | while read -r n; do echo -e "${n}\t${seen[${n}]}"; done >"${work}/jobs.tsv"

echo "== 5. 結果（${RUN_MINUTES} 分鐘，$(date -d "@${t0}" +%H:%M)～$(date -d "@${t1}" +%H:%M)）"
first_min=$(( t0 / 60 + 1 )); last_min=$(( t1 / 60 - 1 ))   # 完整落在觀察期間的排定時間
for cj in rules detect alert; do
  deadline="$(kn get cronjob "aiops-${cj}" -o jsonpath='{.spec.jobTemplate.spec.activeDeadlineSeconds}')"
  rows="$(awk -F'\t' -v o="aiops-${cj}" -v a="${first_min}" -v b="${last_min}" '$2==o { n=split($1,p,"-"); m=p[n]; if (m>=a && m<=b) print }' "${work}/jobs.tsv")"
  n_sched="$(grep -c . <<<"${rows}")"
  n_ok="$(awk -F'\t' '$8 ~ /Complete/' <<<"${rows}" | grep -c .)"
  missing="$(comm -23 <(seq "${first_min}" "${last_min}") <(awk -F'\t' '{n=split($1,p,"-"); print p[n]}' <<<"${rows}" | sort -n) | wc -l)"
  durs="$(awk -F'\t' '$8 ~ /Complete/ {print $4 "\t" $5}' <<<"${rows}" | while IFS=$'\t' read -r s d; do echo $(( $(sec "${d}") - $(sec "${s}") )); done | sort -n)"
  dmax="$(tail -1 <<<"${durs}")"; dmed="$(awk '{a[NR]=$1} END {print a[int((NR+1)/2)]}' <<<"${durs}")"
  expect=$(( last_min - first_min + 1 ))
  check "aiops-${cj}：每分鐘排一次、都成功、沒漏（排了／成功／漏掉）" "${n_sched}/${n_ok}/${missing}" "${expect}/${expect}/0"
  if [[ -n "${dmax}" ]] && (( dmax * 10 <= deadline * 6 )); then pass "aiops-${cj}：跑最久 ${dmax} 秒、中位數 ${dmed} 秒，activeDeadlineSeconds ${deadline} 的 60% 內"
  else bad "aiops-${cj}：跑最久 ${dmax:-?} 秒（中位數 ${dmed:-?}），activeDeadlineSeconds ${deadline}"; fi
  check "aiops-${cj}：同時在跑的 Job 最多幾個（Forbid）" "${maxact[aiops-${cj}]}" "1"
done
# 心跳：每次更新的間隔
gaps="$(sort -u "${work}/heartbeat.txt" | while read -r t; do sec "${t}"; done | awk -v t0="${t0}" '$1 >= t0' | awk 'NR>1 {g=$1-p; if (g>m) m=g} {p=$1} END {print m+0, NR}')"
hb_gap="${gaps% *}"; hb_n="${gaps#* }"
if (( hb_n >= RUN_MINUTES - 2 && hb_gap <= 120 )); then pass "心跳：${RUN_MINUTES} 分鐘更新 ${hb_n} 次，最長間隔 ${hb_gap} 秒"
else bad "心跳：${RUN_MINUTES} 分鐘更新 ${hb_n} 次，最長間隔 ${hb_gap} 秒（預期每分鐘、間隔 ≤ 120 秒）"; fi
alert_runs="$(cat "${logs}"/aiops-alert-*.log 2>/dev/null | grep -c '新開')"
echo "   alert 的 log：${alert_runs} 次「新開…件、進行中…件」；最後一次：$(cat "${logs}"/aiops-alert-*.log 2>/dev/null | grep '新開' | tail -1)"
from="$(date -u -d "@${t0}" +%Y-%m-%dT%H:%M:%SZ)"; to="$(date -u -d "@${t1}" +%Y-%m-%dT%H:%M:%SZ)"
fired="$(cat "${logs}"/aiops-rules-*.log 2>/dev/null | grep -cE '^  \[[a-z]+\] ')"
n_rule="$(es -H "Authorization: ApiKey ${KEY}" "${ES}/aiops-anomalies/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"level\":\"rule\"}},{\"range\":{\"detected_at\":{\"gte\":\"${from}\",\"lte\":\"${to}\"}}}]}}}" | jq .count)"
n_base="$(es -H "Authorization: ApiKey ${KEY}" "${ES}/aiops-anomalies/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"terms\":{\"level\":[\"dow_hour\",\"hour\",\"all\",\"group\"]}},{\"range\":{\"detected_at\":{\"gte\":\"${from}\",\"lte\":\"${to}\"}}}]}}}" | jq .count)"
n_det="$(cat "${logs}"/aiops-detect-*.log 2>/dev/null | grep -oE '^共 [0-9]+ 個異常' | awk '{s+=$2} END {print s+0}')"
n_alert="$(es -H "Authorization: ApiKey ${KEY}" "${ES}/aiops-alerts/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"category\":\"service\"}},{\"range\":{\"updated_at\":{\"gte\":\"${from}\",\"lte\":\"${to}\"}}}]}}}" | jq .count)"
if (( fired > 0 && n_rule > 0 )); then pass "rules 寫進 aiops-anomalies：log 觸發 ${fired} 次；ES 這段時間 level rule 的 ${n_rule} 筆"
else bad "rules 寫進 aiops-anomalies：log 觸發 ${fired} 次；ES 這段時間 level rule 的 ${n_rule} 筆"; fi
echo "   detect 的 log 合計「共 N 個異常」${n_det}；ES 這段時間 baseline 異常 ${n_base} 筆；aiops-alerts 這段時間更新的件 ${n_alert}"

echo "== 6. train 照排程（台北時間）跑：排程改成 2 分鐘後，看那一分鐘有沒有排"
lock
at_min=$(( $(date +%s) / 60 + 2 ))
# Git Bash 沒有 zoneinfo，TZ=Asia/Taipei 會默默變成 UTC；POSIX 寫法 UTC-8 就是 +08:00
cron="$(TZ=UTC-8 date -d "@$(( at_min * 60 ))" +'%-M %-H * * *')"
kn patch cronjob aiops-train --type merge -p "{\"spec\":{\"schedule\":\"${cron}\"}}" >/dev/null
unlock
echo "   排程暫時改成「${cron}」（台北時間）＝ $(iso_min "${at_min}")"
got=""
for _ in $(seq 40); do
  got="$(kn get jobs -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -E "^aiops-train-[0-9]+$" | head -1)"
  [[ -n "${got}" ]] && break; sleep 10
done
tw_hm="$(TZ=UTC-8 date -d "@$(( at_min * 60 ))" +%H:%M)"
check "train 在排定的那一分鐘（台北時間 ${tw_hm}）被排出來" "${got:+$(iso_min "$(minute_of "${got}")")}" "$(iso_min "${at_min}")"
kn wait --for=condition=complete "job/${got}" --timeout=900s >/dev/null 2>&1 && pass "排程跑的 train 成功" || bad "排程跑的 train 沒有成功"
lock
kn patch cronjob aiops-train --type merge -p '{"spec":{"schedule":"0 2 * * *"}}' >/dev/null
unlock

echo "== 7. Forbid（alert 先睡 150 秒）、activeDeadlineSeconds（detect 睡 120 秒）"
lock
kn patch cronjob aiops-alert --type json -p '[{"op":"add","path":"/spec/jobTemplate/spec/template/spec/containers/0/command","value":["sh","-c","sleep 150; exec python3 -m investigator alert --config configs/rca/rca.yaml"]}]' >/dev/null
kn patch cronjob aiops-detect --type json -p '[{"op":"add","path":"/spec/jobTemplate/spec/template/spec/containers/0/command","value":["sh","-c","sleep 120; exec python3 -m detector detect --config configs/baseline/series.yaml --window 10m"]}]' >/dev/null
unlock
p_min=$(( $(date +%s) / 60 + 1 ))      # 改完之後第一個排定時間
# 從那一分鐘起每 5 秒看：alert 同時沒結束的 Job 數；第 140 秒（alert 還在睡，後面兩個排定時間都過了）記下已經開的
act=0; aj=""
while (( $(date +%s) < p_min * 60 + 200 )); do
  js="$(kn get jobs -o json 2>/dev/null)" || { sleep 5; continue; }
  n="$(jq '[.items[] | select(.metadata.ownerReferences[0].name=="aiops-alert")
          | select((.status.conditions // []) | map(select(.status=="True" and (.type=="Complete" or .type=="Failed"))) | length == 0)] | length' <<<"${js}")"
  (( n > act )) && act=${n}
  if [[ -z "${aj}" ]] && (( $(date +%s) >= p_min * 60 + 140 )); then
    aj="$(jq -r --argjson m "${p_min}" '[.items[] | select(.metadata.ownerReferences[0].name=="aiops-alert")
          | (.metadata.name | split("-") | last | tonumber) | select(. >= $m) | . - $m] | sort | map(tostring) | join(" ")' <<<"${js}")"
  fi
  sleep 5
done
check "Forbid：alert 睡 150 秒時，後面兩個排定時間不開新的（第 140 秒時已開的，排定分鐘 − 第一個）" "${aj}" "0"
check "Forbid：同時沒結束的 alert Job 最多幾個" "${act}" "1"
dj="$(kn get jobs -o json | jq -r --argjson m "${p_min}" '[.items[] | select(.metadata.ownerReferences[0].name=="aiops-detect" and ((.metadata.name | split("-") | last | tonumber) >= $m))
     | ((.status.conditions // []) | map(select(.status=="True")) | map(.type + ":" + (.reason // "")) | join(","))] | first // ""')"
check "activeDeadlineSeconds：detect 卡住（睡 120 秒）被砍" "${dj}" "Failed:DeadlineExceeded"
dd="$(kn get jobs -o json | jq -r --argjson m "${p_min}" '[.items[] | select(.metadata.ownerReferences[0].name=="aiops-detect" and ((.metadata.name | split("-") | last | tonumber) >= $m))
     | ((.status.conditions[]? | select(.type=="Failed") | .lastTransitionTime) as $f | [.status.startTime, $f])] | first | @tsv')"
[[ -n "${dd}" ]] && echo "     從開始到被砍 $(( $(sec "${dd#*$'\t'}") - $(sec "${dd%$'\t'*}") )) 秒（activeDeadlineSeconds 50）"
lock
kn patch cronjob aiops-alert --type json -p '[{"op":"remove","path":"/spec/jobTemplate/spec/template/spec/containers/0/command"}]' >/dev/null
kn patch cronjob aiops-detect --type json -p '[{"op":"remove","path":"/spec/jobTemplate/spec/template/spec/containers/0/command"}]' >/dev/null
unlock

echo "== 8. 失敗會重試"
# (a) 每分鐘的：ES key 壞掉 → 失敗；修好 → 下一分鐘成功
lock
kn create secret generic aiops-secrets --from-literal=ES_API_KEY=bogus --from-literal=LLM_API_KEY= --dry-run=client -o yaml | kn apply -f - >/dev/null
# (b) train：連不到 ES → Job 內重試（backoffLimit）
kn create job train-retry --from=cronjob/aiops-train --dry-run=client -o json \
  | jq '.spec.template.spec.containers[0].env = [{"name": "ES_URL", "value": "http://127.0.0.1:9"}]' | kn apply -f - >/dev/null
unlock
bad_from=$(( $(date +%s) / 60 + 1 ))
sleep $(( bad_from * 60 - $(date +%s) + 150 ))     # 壞 2 分半：至少 2 輪
lock
kn create secret generic aiops-secrets --from-literal=ES_API_KEY="${KEY}" --from-literal=LLM_API_KEY= --dry-run=client -o yaml | kn apply -f - >/dev/null
unlock
good_from=$(( $(date +%s) / 60 + 1 ))
sleep $(( good_from * 60 - $(date +%s) + 110 ))
js="$(kn get jobs -o json)"
for cj in rules detect alert; do
  st="$(jq -r --arg o "aiops-${cj}" --argjson a "${bad_from}" --argjson g "${good_from}" '
    [.items[] | select(.metadata.ownerReferences[0].name==$o) | {m: (.metadata.name | split("-") | last | tonumber),
      s: ((.status.conditions // []) | map(select(.status=="True")) | map(.type) | join(","))}]
    | [ (map(select(.m >= $a and .m < $g and .s == "Failed")) | length > 0),
        (map(select(.m >= $g and .s == "Complete")) | length > 0) ] | map(tostring) | join("/")' <<<"${js}")"
  check "aiops-${cj}：key 壞掉時失敗（Job Failed）／修好後下一輪成功" "${st}" "true/true"
  kn logs "job/$(jq -r --arg o "aiops-${cj}" '[.items[] | select(.metadata.ownerReferences[0].name==$o and ((.status.failed // 0) > 0))] | last | .metadata.name' <<<"${js}")" 2>/dev/null \
    | grep -E 'Error|401|RuntimeError' | tail -1 | cut -c1-160 | sed 's/^/     /'
done
kn wait --for=condition=failed job/train-retry --timeout=600s >/dev/null 2>&1
tr="$(kn get job train-retry -o json)"
check "train 連不到 ES：Job 內跑了幾次 Pod、最後的狀態（backoffLimit 2 → 3 次）" \
  "$(jq -r '"\(.status.failed // 0) 次/" + ([(.status.conditions // [])[] | select(.status=="True") | .type + ":" + (.reason // "")] | join(","))' <<<"${tr}")" \
  "3 次/Failed:BackoffLimitExceeded"

echo "== 9. 收尾"
cp "${work}/jobs.tsv" "${work}/heartbeat.txt" "${logs}/" 2>/dev/null
echo "   Job 清單、log：${logs}"
if [[ "${KEEP:-}" != "1" ]]; then
  lock
  k delete namespace "${NS}" --wait=true >/dev/null && echo "   刪掉 namespace ${NS}"
  unlock
  es -X DELETE "${ES}/_security/api_key" -d "{\"ids\":[\"${KEY_ID}\"]}" | jq -c '{invalidated: .invalidated_api_keys}' | sed 's/^/   作廢 key：/'
fi
echo
if (( fail )); then echo "有項目失敗"; exit 1; else echo "全部通過"; fi
