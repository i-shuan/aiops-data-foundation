#!/usr/bin/env bash
# 在公司的 ES 上補 AIOps 需要的東西。收值（Elastic APM 寫的 traces-apm*、logs-apm*、metrics-apm*）是公司現有的，這裡不碰。
#
# 用法：ES_URL=https://<elasticsearch-host>:9200 ES_AUTH=elastic:<password> ./elasticsearch-setup.sh
#       只更新結構、不發新 key：SKIP_KEYS=1 ./elasticsearch-setup.sh
#       CI 用：KEYS_OUT_DIR=<資料夾>：key 不印出來，寫成檔（engine、grafana 是 key 本身；
#       engine-values.yaml、grafana-values.yaml 是 chart 的 values，權限 600）。
#       需要 curl、jq。
#
# 做兩件事：
#   0. 結構：traces-merged-* 的 ILM 與 index template（trace-merge 一個請求一筆，nested，要先有 template）。
#      aiops-* 的索引 engine 自己建（帶 mapping），不用在這裡。可以重複執行，結果一樣。
#      snapshot：給了 SNAPSHOT_REPO_TYPE／SNAPSHOT_REPO_SETTINGS 才註冊每天一次的備份。
#   1. API key（兩把）：
#      engine   讀 Elastic APM 的三種資料，寫 aiops-*、traces-merged-*。→ aiops/aiops-secrets 的 ES_API_KEY
#      grafana  只讀 aiops-alerts 等四個索引。→ grafana/grafana-env 的 GRAFANA_ES_API_KEY
set -euo pipefail

cd "$(dirname "$0")/.."   # elasticsearch/code/：設定檔在 storage/

: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH（有 manage_security 權限的帳號）}"

es() {
  curl -sS --max-time 60 -u "${ES_AUTH}" -H 'Content-Type: application/json' "$@"
  echo
}

# Windows 的 jq 每行結尾多一個 CR（\r）：去掉（Linux 上沒影響）
jq() { command jq "$@" | tr -d '\015'; }

# 發一把 API key：沒設 KEYS_OUT_DIR 就印出來（人手動跑）；有設就只寫檔、不印（CI 的 log 不能有 key）。
# issue_key <名稱> <curl 的參數…>
issue_key() {
  local label="$1"; shift
  if [[ -z "${KEYS_OUT_DIR:-}" ]]; then
    es -X POST "${ES_URL}/_security/api_key" "$@"
    return
  fi
  local out enc
  out="$(es -X POST "${ES_URL}/_security/api_key" "$@")"
  enc="$(printf '%s' "${out}" | jq -r '.encoded // empty')"
  [[ -n "${enc}" ]] || { echo "發 ${label} key 失敗：$(printf '%s' "${out}" | jq -r '.error.reason // "沒有 encoded"')" >&2; exit 1; }
  ( umask 077; printf '%s' "${enc}" > "${KEYS_OUT_DIR}/${label}" )
  echo "  已寫入 ${KEYS_OUT_DIR}/${label}（不印出）"
}

put_json() {
  local path="$1" file="$2"
  echo "=== PUT ${path} ==="
  # 結構設定錯了就停，不要帶著壞掉的 template 繼續發 key
  es --fail-with-body -X PUT "${ES_URL}${path}" --data-binary "@${file}"
}

# 0. 結構
tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

# 保留天數：retention.conf 的值填進 ILM 的 JSON（__變數名__ 換成值）
# shellcheck source=retention.conf
source scripts/retention.conf
put_ilm() {  # put_ilm <policy 名稱> <JSON 檔>
  local out="${tmp_dir}/ilm-$1.json" v
  cp "$2" "${out}"
  for v in $(grep -oE "^[A-Z_0-9]+" scripts/retention.conf); do
    sed -i "s/__${v}__/${!v}/g" "${out}"
  done
  if grep -q '__[A-Z_0-9]*__' "${out}"; then echo "retention.conf 少了：$(grep -o '__[A-Z_0-9]*__' "${out}" | sort -u)"; exit 1; fi
  put_json "/_ilm/policy/$1" "${out}"
}
put_ilm traces-merged                              storage/ilm-traces-merged.json
# trace（merged，一個請求一筆）：detector trace-merge 從 traces-apm-* 合成 traces-merged-<group>-<日期>；自己的 template 與 ILM
put_json /_index_template/traces-merged            storage/template-traces-merged.json
echo "=== 已存在的 traces-merged-* 套用新的保留天數 ==="
es -X PUT "${ES_URL}/traces-merged-*/_settings?expand_wildcards=all&allow_no_indices=true" -d '{"index.lifecycle.name": "traces-merged"}' >/dev/null

# snapshot：人工判斷的索引（回饋、事故紀錄、調整）掉了不能重算，每天備份一次（SLM；天數、索引在 retention.conf）。
# 儲存庫由管 ES 的人決定：SNAPSHOT_REPO_TYPE（fs、s3、azure、gcs…）＋ SNAPSHOT_REPO_SETTINGS（該類型的 settings JSON）。
# 沒給就跳過（印一行提醒）。還原步驟見 elasticsearch/README.md「備份與還原」
SNAPSHOT_REPO="${SNAPSHOT_REPO:-aiops}"
if [[ -n "${SNAPSHOT_REPO_TYPE:-}" && -n "${SNAPSHOT_REPO_SETTINGS:-}" ]]; then
  echo "=== snapshot 儲存庫 ${SNAPSHOT_REPO}（${SNAPSHOT_REPO_TYPE}）與每天的 SLM 政策 aiops-daily ==="
  es --fail-with-body -X PUT "${ES_URL}/_snapshot/${SNAPSHOT_REPO}" \
    -d "{\"type\": \"${SNAPSHOT_REPO_TYPE}\", \"settings\": ${SNAPSHOT_REPO_SETTINGS}}" >/dev/null
  es --fail-with-body -X POST "${ES_URL}/_snapshot/${SNAPSHOT_REPO}/_verify" >/dev/null
  es --fail-with-body -X PUT "${ES_URL}/_slm/policy/aiops-daily" -d "{
    \"schedule\": \"${SNAPSHOT_SCHEDULE}\", \"name\": \"<aiops-{now/d}>\", \"repository\": \"${SNAPSHOT_REPO}\",
    \"config\": {\"indices\": \"${SNAPSHOT_INDICES}\", \"ignore_unavailable\": true, \"include_global_state\": false},
    \"retention\": {\"expire_after\": \"${SNAPSHOT_KEEP}\", \"min_count\": 7, \"max_count\": 60}}" >/dev/null
  es -X POST "${ES_URL}/_slm/start" >/dev/null
else
  echo "=== 沒給 SNAPSHOT_REPO_TYPE／SNAPSHOT_REPO_SETTINGS，跳過 snapshot（回饋、事故紀錄、調整沒有備份；見 elasticsearch/README.md「備份與還原」）==="
fi

[[ "${SKIP_KEYS:-}" == "1" ]] && exit 0

# 1. engine：讀 Elastic APM 的資料（traces-apm*、logs-apm*、metrics-apm*），只能寫自己的 aiops-* 和 traces-merged-*。
echo "=== AIOps engine key ==="
issue_key engine -d '{
  "name": "aiops-engine",
  "role_descriptors": {
    "aiops-engine": {
      "index": [
        {"names": ["traces-apm*", "logs-apm*", "metrics-apm*"], "privileges": ["read", "view_index_metadata"]},
        {"names": ["aiops-*", "traces-merged-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}
      ]
    }
  }
}'
echo

# 2. Grafana 唯讀 key：讀 aiops-alerts（AIOps 合併過的事件，Grafana 依它發通知）和儀表板用的 aiops-anomalies、aiops-tuning、
#    aiops-feedback（grafana/code/datasource.yaml 的四個 data source；只給 aiops-alerts 的話另外三個讀取 403、儀表板空白）。
#    cluster monitor：Grafana 的 ES data source 會先打 GET / 看版本（沒有這個權限 data source 測試回 403）
echo "=== Grafana 唯讀 key ==="
issue_key grafana -d '{
  "name": "grafana-aiops-alerts",
  "role_descriptors": {"grafana-aiops-alerts": {"cluster": ["monitor"], "index": [{"names": ["aiops-alerts", "aiops-anomalies", "aiops-tuning", "aiops-feedback"], "privileges": ["read", "view_index_metadata"]}]}}
}'
echo

# CI：把 key 組成各 chart 的 values（chart 用它建 Secret）
if [[ -n "${KEYS_OUT_DIR:-}" ]]; then
  ( umask 077
    printf 'esApiKey: "%s"\n' "$(cat "${KEYS_OUT_DIR}/engine")" > "${KEYS_OUT_DIR}/engine-values.yaml"
    # 第一行 secretData: 由部署 job 補上其他欄位（ES_URL、webhook、admin 密碼）；這裡只放 key
    printf 'secretData:\n  GRAFANA_ES_API_KEY: "%s"\n' "$(cat "${KEYS_OUT_DIR}/grafana")" > "${KEYS_OUT_DIR}/grafana-values.yaml" )
  echo "=== key 與 values 檔寫在 ${KEYS_OUT_DIR}/（不印出）==="
fi
