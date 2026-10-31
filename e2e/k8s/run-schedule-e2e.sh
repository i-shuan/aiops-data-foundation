#!/usr/bin/env bash
# engine 在 K8s 上排程跑：aiops-engine/helmchart 的一隻 pod（scheduler container 照 configs/schedule/schedule.yaml 跑第一階段 4 個工作）
# 用 helm 裝到 namespace aiops-engine-test（values-test.yaml：連 es-test、本機 image、不裝回饋網頁），跑 RUN_MINUTES 分鐘再看。
#
# 每一項印 PASS／FAIL（紀錄來源是 pod 的 log：排程器每次執行印一行 [scheduler] <工作> <排定分鐘> <結果>）：
#   1. rules、detect、alert 每分鐘都跑、都 ok、沒漏掉任何一分鐘、沒有「跳過」；跑最久的那次 ≤ timeout 的 60%
#   2. 心跳（aiops-alerts/_doc/heartbeat）每分鐘都有更新；rules 的觸發寫進 aiops-anomalies
#   3. train：手動跑（python3 -m scheduler --run train）成功
#   4. 不重疊：alert 換成睡 150 秒 → 後面兩分鐘印「跳過：上一次還在跑」、同一分鐘只有一次
#   5. 逾時：detect 換成睡 120 秒、timeout 50 → 印「逾時 50 秒，已砍」
#   6. 失敗會重試：每天／每週那種（retries 2）一直失敗 → 同一分鐘印 3 次失敗、「已重試 2 次，放棄」；
#      ES key 壞掉時每分鐘的 3 個都失敗、修好（重啟 pod）後下一分鐘就 ok
#   7. 排程器自己的 liveness：/tmp/scheduler-alive 每分鐘更新
# 跑完刪掉 namespace、作廢這次發的 API key。
#
# 前提：run-e2e.sh 跑過（es-test 的 ES、Collector 在跑）；Rancher Desktop（nerdctl 建 image 進 k8s.io）。
# 用法：e2e/k8s/run-schedule-e2e.sh        RUN_MINUTES=20（預設）；KEEP=1 跑完不刪；SKIP_BUILD=1 不重建 image
# 部署、改設定、刪除時拿 cluster-lock；觀察的那段時間放掉（排程器只讀 ES、寫 aiops-*，不影響別人）。
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
NS=aiops-engine-test
IMAGE=aiops-engine:p2-schedule
RUN_MINUTES="${RUN_MINUTES:-20}"
ES_PORT="${ES_PORT:-31515}"
ES_PASSWORD=changeme-test-only
ES="http://127.0.0.1:${ES_PORT}"
NERDCTL="${NERDCTL:-$(command -v nerdctl || echo "/c/Program Files/Rancher Desktop/resources/resources/win32/bin/nerdctl.exe")}"
work="$(mktemp -d)"
fail=0
pass() { echo "  PASS  $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
check() { if [[ "$2" == "$3" ]]; then pass "$1：${2}"; else bad "$1：得到 [$2]，預期 [$3]"; fi; }
ts() { date +%H:%M:%S; }
p() { cygpath -m "$1" 2>/dev/null || echo "$1"; }     # Windows 的 helm 要 C:/... 路徑

# kubeconfig 複製一份（不動 ~/.kube/config）；Windows 的 kubectl 要 C:/ 的路徑
cp "${KUBECONFIG_SRC:-${HOME}/.kube/config}" "${work}/kubeconfig"
export KUBECONFIG="$(p "${work}/kubeconfig")"
k() { kubectl --context rancher-desktop "$@"; }
kn() { k -n "${NS}" "$@"; }
es() { curl -sS -m 30 -u "elastic:${ES_PASSWORD}" -H 'Content-Type: application/json' "$@"; }
chart="$(p "${repo}/aiops-engine/helmchart")"
deploy() {  # deploy [helm 額外參數…]：裝／更新 chart，等 pod ready
  helm upgrade --install aiops "${chart}" -n "${NS}" --kube-context rancher-desktop -f "${chart}/values-test.yaml" "$@" >/dev/null
  kn rollout status deployment/aiops-engine --timeout=300s >/dev/null
}
slog() { kn logs deployment/aiops-engine -c scheduler --since-time="$1" 2>/dev/null | grep -F '[scheduler]'; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
sec() { date -u -d "$1" +%s; }
# shellcheck source=cluster-lock.sh
source "${here}/cluster-lock.sh"
pf_pid=""
cleanup() {
  if [[ -n "${pf_pid}" ]]; then
    local win; win="$(cat "/proc/${pf_pid}/winpid" 2>/dev/null)"
    kill "${pf_pid}" 2>/dev/null; wait "${pf_pid}" 2>/dev/null
    [[ -n "${win}" ]] && command -v taskkill >/dev/null && taskkill //F //PID "${win}" >/dev/null 2>&1
    pf_pid=""
  fi
  cluster_unlock >/dev/null 2>&1 || true
}
trap cleanup EXIT
lock() { cluster_lock "p2-adjust-schedule"; trap cleanup EXIT; }
unlock() { cluster_unlock; trap cleanup EXIT; }

echo "== 0. image：nerdctl build 進 k8s.io（K8s 直接用，不用 registry）"
if [[ "${SKIP_BUILD:-}" != "1" ]]; then
  "${NERDCTL}" -n k8s.io build -t "${IMAGE}" "${repo}/aiops-engine/code" >"${work}/build.log" 2>&1 || { tail -20 "${work}/build.log"; exit 1; }
fi
"${NERDCTL}" -n k8s.io images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -F "${IMAGE}" | sed 's/^/   /'

echo "== 1. ES port-forward（${ES}）、發一把 baseline 權限的 key"
k -n es-test port-forward --address 127.0.0.1 svc/elasticsearch "${ES_PORT}:9200" >"${work}/pf.log" 2>&1 &
pf_pid=$!
for _ in $(seq 30); do es "${ES}/" >/dev/null 2>&1 && break; sleep 1; done
echo "   ES $(es "${ES}/" | jq -r '.version.number + "（" + .cluster_name + "）"')"
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
deploy
unlock
kn get pod -o custom-columns='POD:.metadata.name,CONTAINERS:.spec.containers[*].name,READY:.status.containerStatuses[*].ready' | sed 's/^/   /'
kn exec deployment/aiops-engine -c scheduler -- python3 -m scheduler --config configs/schedule/schedule.yaml --list | sed 's/^/   /'

echo "== 3. train 先手動跑一次（detect 要有 baseline）"
t0=$(date +%s)
if kn exec deployment/aiops-engine -c scheduler -- python3 -m scheduler --config configs/schedule/schedule.yaml --run train >"${work}/train.log" 2>&1; then
  grep -E '學習區間|條序列|\[scheduler\]' "${work}/train.log" | head -12 | sed 's/^/   /'
  pass "train 手動跑成功，$(( $(date +%s) - t0 )) 秒"
else tail -5 "${work}/train.log"; bad "train 手動跑失敗"; fi

echo "== 4. 觀察 ${RUN_MINUTES} 分鐘（放掉鎖；每 15 秒記心跳、alive 檔）"
: >"${work}/heartbeat.txt"; : >"${work}/alive.txt"
from_iso="$(now_iso)"; t0=$(date +%s); t_end=$(( t0 + RUN_MINUTES * 60 ))
while (( $(date +%s) < t_end )); do
  es -H "Authorization: ApiKey ${KEY}" "${ES}/aiops-alerts/_doc/heartbeat" 2>/dev/null | jq -r '._source.updated_at // empty' >>"${work}/heartbeat.txt"
  kn exec deployment/aiops-engine -c scheduler -- cat /tmp/scheduler-alive 2>/dev/null >>"${work}/alive.txt"; echo >>"${work}/alive.txt"
  (( ($(date +%s) - t0) % 300 < 15 )) && echo "   $(ts) 還有 $(( (t_end - $(date +%s)) / 60 )) 分鐘"
  sleep 15
done
t1=$(date +%s)
slog "${from_iso}" >"${work}/sched.log"

echo "== 5. 結果（${RUN_MINUTES} 分鐘，$(date -d "@${t0}" +%H:%M)～$(date -d "@${t1}" +%H:%M)）"
# [scheduler] <工作> <YYYY-MM-DDTHH:MM+0800> <結果…>；只算完整落在觀察期間的分鐘
first_min=$(( t0 / 60 + 1 )); last_min=$(( t1 / 60 - 1 )); expect=$(( last_min - first_min + 1 ))
minute_of() { date -u -d "${1:0:16}:00${1:16:3}:${1:19:2}" +%s 2>/dev/null | awk '{print int($1/60)}'; }
for j in rules detect alert; do
  rows="$(awk -v j="${j}" '$1=="[scheduler]" && $2==j {print}' "${work}/sched.log")"
  n_ok=0; n_fail=0; n_skip=0; dmax=0; : >"${work}/${j}.min"
  while read -r _ _ when rest; do
    [[ -z "${when}" ]] && continue
    m="$(minute_of "${when}")"; (( m >= first_min && m <= last_min )) || continue
    echo "${m}" >>"${work}/${j}.min"
    case "${rest}" in
      ok*) n_ok=$((n_ok+1)); d="${rest#ok }"; d="${d%%s*}"; d="${d%.*}"; (( d > dmax )) && dmax=${d} ;;
      跳過*) n_skip=$((n_skip+1)) ;;
      *) n_fail=$((n_fail+1)) ;;
    esac
  done <<<"${rows}"
  missing="$(comm -23 <(seq "${first_min}" "${last_min}") <(sort -n "${work}/${j}.min" | uniq) | wc -l)"
  dup="$(sort -n "${work}/${j}.min" | uniq -d | wc -l)"
  timeout="$(kn exec deployment/aiops-engine -c scheduler -- python3 -m scheduler --config configs/schedule/schedule.yaml --list | awk -v j="${j}" '$1==j {print $6}' | tr -d s)"
  check "${j}：每分鐘跑一次、都 ok、沒漏、沒跳過、沒重複（ok／失敗／跳過／漏掉／同一分鐘兩次）" "${n_ok}/${n_fail}/${n_skip}/${missing}/${dup}" "${expect}/0/0/0/0"
  if (( dmax * 10 <= timeout * 6 )); then pass "${j}：跑最久 ${dmax} 秒，timeout ${timeout} 的 60% 內"; else bad "${j}：跑最久 ${dmax} 秒，timeout ${timeout}"; fi
done
gaps="$(sort -u "${work}/heartbeat.txt" | while read -r t; do sec "${t}"; done | awk -v t0="${t0}" '$1 >= t0' | awk 'NR>1 {g=$1-p; if (g>m) m=g} {p=$1} END {print m+0, NR}')"
hb_gap="${gaps% *}"; hb_n="${gaps#* }"
if (( hb_n >= RUN_MINUTES - 2 && hb_gap <= 120 )); then pass "心跳：${RUN_MINUTES} 分鐘更新 ${hb_n} 次，最長間隔 ${hb_gap} 秒"
else bad "心跳：${RUN_MINUTES} 分鐘更新 ${hb_n} 次，最長間隔 ${hb_gap} 秒（預期每分鐘、間隔 ≤ 120 秒）"; fi
alive_gap="$(grep -E '^[0-9]+$' "${work}/alive.txt" | sort -un | awk 'NR>1 {g=$1-p; if (g>m) m=g} {p=$1} END {print m+0}')"
if (( alive_gap > 0 && alive_gap <= 120 )); then pass "liveness：/tmp/scheduler-alive 每分鐘更新，最長間隔 ${alive_gap} 秒"; else bad "liveness：alive 檔最長間隔 ${alive_gap} 秒"; fi
from="$(date -u -d "@${t0}" +%Y-%m-%dT%H:%M:%SZ)"; to="$(date -u -d "@${t1}" +%Y-%m-%dT%H:%M:%SZ)"
fired="$(kn logs deployment/aiops-engine -c scheduler --since-time="${from_iso}" | grep -cE '^  \[[a-z]+\] ')"
n_rule="$(es -H "Authorization: ApiKey ${KEY}" "${ES}/aiops-anomalies/_count" -d "{\"query\":{\"bool\":{\"filter\":[{\"term\":{\"level\":\"rule\"}},{\"range\":{\"detected_at\":{\"gte\":\"${from}\",\"lte\":\"${to}\"}}}]}}}" | jq -r .count)"
if (( fired > 0 && n_rule > 0 )); then pass "rules 寫進 aiops-anomalies：log 觸發 ${fired} 次；ES 這段時間 level rule 的 ${n_rule} 筆"
else bad "rules 寫進 aiops-anomalies：log 觸發 ${fired} 次；ES 這段時間 level rule 的 ${n_rule} 筆"; fi

echo "== 6. 不重疊（alert 睡 150 秒）、逾時（detect 睡 120 秒、timeout 50）、重試（每天那種一直失敗）"
# 用 configOverrides.schedule.schedule 換一張排程表：三個假工作（scheduler.sleep），rules 照跑當對照
cat >"${work}/schedule-test.yaml" <<EOF
timezone: Asia/Taipei
jobs:
  - {name: rules,  cron: "* * * * *", timeout: 50,  args: [detector, rules, --config, configs/baseline/rules.yaml]}
  - {name: alert,  cron: "* * * * *", timeout: 600, args: [scheduler.sleep, "150"]}
  - {name: detect, cron: "* * * * *", timeout: 50,  args: [scheduler.sleep, "120"]}
  - {name: daily,  cron: "* * * * *", timeout: 30,  retries: 2, args: [scheduler.sleep, "0", "1"]}
EOF
lock
deploy --set-file "configOverrides.schedule.schedule=$(p "${work}/schedule-test.yaml")"
unlock
p_min=$(( $(date +%s) / 60 + 1 ))          # 新 pod 起來後第一個排定分鐘
from_iso="$(now_iso)"
sleep $(( p_min * 60 - $(date +%s) + 200 ))  # 到第 3 分鐘過 20 秒：alert 睡完一次、detect 砍過、daily 重試過
slog "${from_iso}" >"${work}/sched2.log"
skips="$(grep -c 'alert .* 跳過：上一次還在跑' "${work}/sched2.log")"
check "不重疊：alert 睡 150 秒時，後面兩分鐘「跳過：上一次還在跑」" "${skips}" "2"
check "不重疊：alert 的 ok 只有 1 次（第一次那筆）" "$(grep -c 'alert .* ok' "${work}/sched2.log")" "1"
check "逾時：detect 每分鐘都被砍（逾時 50 秒）" "$(grep -c 'detect .* 逾時 50 秒，已砍' "${work}/sched2.log" | awk '{print ($1>=2)?"≥2":$1}')" "≥2"
first_daily="$(grep 'daily ' "${work}/sched2.log" | head -1 | awk '{print $3}')"
check "重試：daily（retries 2）同一分鐘失敗 3 次" "$(grep -c "daily ${first_daily} 失敗 rc=1" "${work}/sched2.log")" "3"
check "重試：最後印「已重試 2 次，放棄」" "$(grep -c "daily ${first_daily} .*已重試 2 次，放棄" "${work}/sched2.log" | awk '{print ($1>=1)?1:0}')" "1"
check "對照：rules 照常每分鐘 ok" "$(grep -c 'rules .* ok' "${work}/sched2.log" | awk '{print ($1>=2)?"≥2":$1}')" "≥2"

echo "== 7. ES key 壞掉 → 失敗；修好 → ok（env 是 pod 起來時讀的，換 key 要重啟）"
lock
kn create secret generic aiops-secrets --from-literal=ES_API_KEY=bogus --from-literal=LLM_API_KEY= --dry-run=client -o yaml | kn apply -f - >/dev/null
deploy      # 排程表換回正式的（沒有 override），同時 pod 重啟讀到壞 key
unlock
bad_from="$(now_iso)"; b_min=$(( $(date +%s) / 60 + 1 ))
sleep $(( b_min * 60 - $(date +%s) + 130 ))
slog "${bad_from}" >"${work}/sched3.log"
for j in rules detect alert; do
  check "${j}：key 壞掉時失敗（rc≠0）、沒有 ok" "$(grep -c "${j} .* 失敗 rc=" "${work}/sched3.log" | awk '{print ($1>=2)?"≥2":$1}')/$(grep -c "${j} .* ok" "${work}/sched3.log")" "≥2/0"
done
grep -E 'Error|401|RuntimeError' <(kn logs deployment/aiops-engine -c scheduler --since-time="${bad_from}") | tail -1 | cut -c1-160 | sed 's/^/     /'
lock
kn create secret generic aiops-secrets --from-literal=ES_API_KEY="${KEY}" --from-literal=LLM_API_KEY= --dry-run=client -o yaml | kn apply -f - >/dev/null
kn rollout restart deployment/aiops-engine >/dev/null; kn rollout status deployment/aiops-engine --timeout=300s >/dev/null
unlock
good_from="$(now_iso)"; g_min=$(( $(date +%s) / 60 + 1 ))
sleep $(( g_min * 60 - $(date +%s) + 70 ))
slog "${good_from}" >"${work}/sched4.log"
for j in rules detect alert; do
  check "${j}：修好重啟後下一分鐘 ok" "$(grep -c "${j} .* ok" "${work}/sched4.log" | awk '{print ($1>=1)?"≥1":$1}')" "≥1"
done

echo "== 8. 收尾"
cp "${work}"/sched*.log "${work}/heartbeat.txt" "${work}/train.log" "${work}/" 2>/dev/null
echo "   log：${work}"
if [[ "${KEEP:-}" != "1" ]]; then
  lock
  k delete namespace "${NS}" --wait=true >/dev/null && echo "   刪掉 namespace ${NS}"
  unlock
  es -X DELETE "${ES}/_security/api_key" -d "{\"ids\":[\"${KEY_ID}\"]}" | jq -c '{invalidated: .invalidated_api_keys}' | sed 's/^/   作廢 key：/'
fi
echo
if (( fail )); then echo "有項目失敗"; exit 1; else echo "全部通過"; fi
