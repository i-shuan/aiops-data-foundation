#!/usr/bin/env bash
# Elasticsearch 權限設定。group 清單要跟 collector/code/groups.yaml 一致。
#
# 用法：ES_URL=https://<elasticsearch-host>:9200 ES_AUTH=elastic:<password> ./elasticsearch-setup.sh
#       只更新 index 結構、不發新 key：SKIP_KEYS=1 ./elasticsearch-setup.sh
#       CI 用：KEYS_OUT_DIR=<資料夾>：4 把 key 不印出來，寫成檔（writer、engine、grafana、probe 是 key 本身；
#       collector-values.yaml、engine-values.yaml、grafana-values.yaml 是 chart 的 values，權限 600）；
#       key 在 log 裡看不到，由 CI 的 artifact 交給後面的部署（.gitlab-ci.yml）。
#       需要 curl、jq。
#
# 會建立四類東西：
#   0. index 結構：ILM、mapping、pipeline（含各 log 格式）、index template（./storage/）。
#      可以重複執行，結果一樣。log 欄位規範見 docs/log/README.md。
#   1. 寫入用 API key（給 OpenTelemetry Collector）：一把，所有 group 共用，只能寫、不能讀。
#      資料屬於哪個 group 由 Collector 依 Pod 決定（data_stream.namespace），不靠 key 擋：沒有部門間寫入隔離的要求，
#      一把 key 少了「key 權限 pattern 漏了 → ES 回 200 但整批拒絕、Collector 看不到」的機會（docs/pdca/phase1-collect/2026-10-ingest-simplify.md）。
#      回應裡的 "encoded" 放進每個應用 cluster otel-collector namespace 的 Secret es-writer-keys 的 ES_WRITER_API_KEY。
#      之後換 key 用 rotate-writer-key.sh（不掉資料）；重跑這支腳本也會發新 key，但舊的不會作廢。
#   2. AIOps 專用唯讀 API key：可讀所有 k8s-*（log）、metrics-*.otel-*（metrics）、traces-*.otel-*（trace）。
#   3. 給人用的唯讀角色：每個 group 一個，只能看自己的 log、事件、metrics、trace。再指派給使用者。
#   另外發一把 AIOps baseline 工具用的 key（aiops-engine/code/configs/baseline）：讀上面三種資料，只能寫 aiops-*。
#   4. 健康檢查用 key（es-probe，只能 GET /）：給 ES 前面的 LB／Service 的健康檢查用。
#      ES 剛起來的 1 秒多，API key 會被拒（401）但 elastic 帳號、/_cluster/health 都正常；Collector 收到 401
#      會當成「key 錯了」直接丟資料。所以健康檢查一定要用 API key 打 GET /、等 200 才把流量導過去
#      （範例：storage/healthcheck-haproxy.cfg；docs/pdca/phase1-collect/2026-10-es-startup-401.md）。
set -euo pipefail

cd "$(dirname "$0")/.."   # elasticsearch/code/：設定檔在 storage/

: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH（有 manage_security 權限的帳號）}"

es() {
  curl -sS --max-time 60 -u "${ES_AUTH}" -H 'Content-Type: application/json' "$@"
  echo
}

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

writer_key() {
  local name="$1" indices="$2"
  echo "=== 寫入 key：${name} ==="
  issue_key writer -d "{
    \"name\": \"${name}\",
    \"role_descriptors\": {
      \"${name}\": {
        \"index\": [{\"names\": ${indices}, \"privileges\": [\"create_doc\", \"create_index\", \"auto_configure\"]}]
      }
    }
  }"
}

reader_role() {
  local group="$1"
  echo "=== 唯讀角色：logs-reader-${group} ==="
  es -X PUT "${ES_URL}/_security/role/logs-reader-${group}" -d "{
    \"indices\": [{
      \"names\": [\"k8s-${group}-*\", \"k8s-events-${group}-*\", \"metrics-*-${group}\", \"traces-*.otel-${group}\", \"traces-merged-${group}-*\"],
      \"privileges\": [\"read\", \"view_index_metadata\"]
    }]
  }"
}

put_json() {
  local path="$1" file="$2"
  echo "=== PUT ${path} ==="
  # 結構設定錯了就停，不要帶著壞掉的 template 繼續發 key
  es --fail-with-body -X PUT "${ES_URL}${path}" --data-binary "@${file}"
}

# 0. index 結構（順序有關：template 引用 component 與 pipeline，要先建）
tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

# 應用 log 的時間沒帶時區時，當成哪個時區（中央 pipeline 的 date processor）。各 cluster 不同時區的話要另外處理
LOG_DEFAULT_TIMEZONE="${LOG_DEFAULT_TIMEZONE:-Asia/Taipei}"

# pipeline 的 JSON 裡 "__SCRIPT__" 換成同名 .painless 的內容（沒有 .painless 就原樣送出）；
# "__TZ_SCRIPT__" 換成 pipeline-tz.painless；"__LOG_DEFAULT_TIMEZONE__" 換成上面的時區
put_pipeline() {
  local name="$1" json="$2" painless="${2%.json}.painless" out="${tmp_dir}/$1.json"
  local tz_script="storage/pipeline-tz.painless"
  local src=""
  [[ -f "${painless}" ]] && src="$(cat "${painless}")"
  jq --arg src "${src}" --rawfile tzsrc "${tz_script}" --arg tz "${LOG_DEFAULT_TIMEZONE}" \
    'walk(if . == "__SCRIPT__" and $src != "" then $src elif . == "__TZ_SCRIPT__" then $tzsrc
          elif . == "__LOG_DEFAULT_TIMEZONE__" then $tz else . end)' "${json}" > "${out}"
  put_json "/_ingest/pipeline/${name}" "${out}"
}

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
put_ilm k8s-logs                                   storage/ilm-policy.json
put_ilm metrics-otel                               storage/ilm-metrics-otel.json
put_ilm metrics-hist-otel                          storage/ilm-metrics-hist-otel.json
put_ilm traces-otel                                storage/ilm-traces-otel.json
put_ilm traces-merged                              storage/ilm-traces-merged.json
put_ilm aiops-log-stats                            storage/ilm-aiops-log-stats.json
put_pipeline k8s-logs-fields                       storage/pipeline-fields.json
# 每種 log 格式一個 pipeline：storage/formats/<格式>.json → k8s-logs-format-<格式>
for f in storage/formats/*.json; do
  put_pipeline "k8s-logs-format-$(basename "${f}" .json)" "${f}"
done
put_pipeline k8s-logs                              storage/pipeline.json
put_json /_component_template/k8s-logs-settings    storage/component-settings.json
put_json /_component_template/k8s-logs-mappings    storage/component-mappings.json
put_json /_index_template/k8s-logs                 storage/index-template.json
# metrics：ES（8.19 以上）內建 metrics-otel@template 預留的自訂設定（metrics-*.otel-* 的 TSDS）
put_json /_component_template/metrics-otel@custom  storage/metrics-otel-custom.json
# log 改用 OpenTelemetry Collector 收時（比較中，見 docs/log/README.md）：
# logs-*.otel-* 先跑 k8s-logs-otel，我們的 Collector 送的轉到 k8s-<group>-<cluster>
put_pipeline k8s-logs-otel                         storage/pipeline-otel.json
put_json /_component_template/logs-otel@custom     storage/logs-otel-custom.json
# trace（raw，每一段一筆、只留 2 天）：traces-*.otel-* 用 trace_id + span_id 當 _id，重送不會重複；記 event.ingested 給合成用
put_pipeline traces-otel                           storage/pipeline-traces-otel.json
put_json /_component_template/traces-otel@custom   storage/traces-otel-custom.json
# trace（merged，一個請求一筆）：detector trace-merge 寫的 traces-merged-<group>-<日期>；自己的 template 與 ILM，不用 ES 內建的
put_json /_index_template/traces-merged            storage/template-traces-merged.json
# 應用的直方圖（metrics-generic.otel-*）：同樣照內建 metrics-otel@template 的組成，多疊 metrics-hist-otel@custom 換成不降解析度的 ILM
#   （ES 8.19 的 histogram 欄位降解析度時不合併、只留一筆的值；docs/retention-reference.md）
put_json /_component_template/metrics-hist-otel@custom storage/metrics-hist-otel-custom.json
es "${ES_URL}/_index_template/metrics-otel@template" | jq '.index_templates[0].index_template
  | .index_patterns = ["metrics-generic.otel-*"] | .priority = (.priority + 10)
  | .composed_of += ["metrics-hist-otel@custom"] | del(.version, .created_date, .created_date_millis, .modified_date, .modified_date_millis, .deprecated)
  | ._meta = {"description": "應用的直方圖：metrics-otel@template 加上 metrics-hist-otel@custom（不降解析度）。由 elasticsearch-setup.sh 產生"}' \
  > "${tmp_dir}/metrics-hist-template.json"
put_json /_index_template/metrics-hist-otel        "${tmp_dir}/metrics-hist-template.json"
# 已經存在的資料也換成新的保留天數（新的 backing index 由上面的 template 帶）
echo "=== 已存在的 metrics、trace 套用新的保留天數 ==="
es -X PUT "${ES_URL}/metrics-*.otel-*/_settings?expand_wildcards=all" -d '{"index.lifecycle.name": "metrics-otel"}' >/dev/null
es -X PUT "${ES_URL}/metrics-generic.otel-*/_settings?expand_wildcards=all&allow_no_indices=true" -d '{"index.lifecycle.name": "metrics-hist-otel"}' >/dev/null
es -X PUT "${ES_URL}/traces-generic.otel-*/_settings?expand_wildcards=all" -d '{"index.lifecycle.name": "traces-otel"}' >/dev/null
es -X PUT "${ES_URL}/traces-merged-*/_settings?expand_wildcards=all&allow_no_indices=true" -d '{"index.lifecycle.name": "traces-merged"}' >/dev/null
# ILM 會把「正在進行的階段」的設定暫存起來：寫入中的 index 還在用舊政策（例如 ES 內建、30 天才 rollover、永遠不刪）
# 的 hot 階段，換政策後要等舊的 hot 階段走完才生效。這種資料流先 rollover 一次，新的 index 從頭就用新政策
rollover_if_policy_stale() {  # rollover_if_policy_stale <資料流樣式>
  local ds wi info
  for ds in $(es "${ES_URL}/_data_stream/$1" | jq -r '.data_streams[]? | .name'); do
    wi="$(es "${ES_URL}/_data_stream/${ds}" | jq -r '.data_streams[0].indices[-1].index_name')"
    info="$(es "${ES_URL}/${wi}/_ilm/explain" | jq -r '.indices[] | "\(.policy // "") \(.phase_execution.policy // "")"')"
    read -r want cached <<<"${info}"
    if [[ -n "${cached}" && "${want}" != "${cached}" ]]; then
      echo "=== rollover ${ds}（寫入中的 index 還在用 ${cached}，換成 ${want}）==="
      es -X POST "${ES_URL}/${ds}/_rollover" >/dev/null
    fi
  done
}
rollover_if_policy_stale 'metrics-*.otel-*'
rollover_if_policy_stale 'traces-*.otel-*'
# log 的長期統計：transform 每 5 分鐘把 log 筆數寫進 aiops-log-stats（別名，背後的 index 每 30 天換一個）
put_json /_index_template/aiops-log-stats          storage/template-aiops-log-stats.json
if [[ "$(curl -s -o /dev/null -w '%{http_code}' -u "${ES_AUTH}" "${ES_URL}/_alias/aiops-log-stats")" != "200" ]]; then
  echo "=== 建 aiops-log-stats-000001 ==="
  es -X PUT "${ES_URL}/aiops-log-stats-000001" -d '{"aliases": {"aiops-log-stats": {"is_write_index": true}}}' >/dev/null
fi
if [[ "$(curl -s -o /dev/null -w '%{http_code}' -u "${ES_AUTH}" "${ES_URL}/_transform/aiops-log-stats")" != "200" ]]; then
  put_json /_transform/aiops-log-stats              storage/transform-log-stats.json
else  # 已經有了：更新設定（group_by 這類改不了的要先刪掉 transform 再跑）
  echo "=== 更新 transform aiops-log-stats ==="
  jq 'del(.dest, .pivot)' storage/transform-log-stats.json > "${tmp_dir}/transform-update.json"
  es --fail-with-body -X POST "${ES_URL}/_transform/aiops-log-stats/_update" --data-binary "@${tmp_dir}/transform-update.json" >/dev/null
fi
es -X POST "${ES_URL}/_transform/aiops-log-stats/_start" >/dev/null   # 已經在跑會回錯誤，不影響
# 上面的設定只對新的 backing index 生效：已經存在、還沒用上新 pipeline 的 OTel 資料流換一個新的。
# log 沒換的話，資料會停在 logs-*.otel-*，不會轉到 k8s-*
rollover_if_not() {  # rollover_if_not <資料流樣式> <default pipeline>
  local ds wi
  for ds in $(es "${ES_URL}/_data_stream/$1" | jq -r '.data_streams[]? | .name'); do
    wi="$(es "${ES_URL}/_data_stream/${ds}" | jq -r '.data_streams[0].indices[-1].index_name')"
    if [[ "$(es "${ES_URL}/${wi}/_settings" | jq -r '.[].settings.index.default_pipeline')" != "$2" ]]; then
      echo "=== rollover ${ds}（換成 $2）==="
      es -X POST "${ES_URL}/${ds}/_rollover" >/dev/null
    fi
  done
}
rollover_if_not 'logs-*.otel-*' k8s-logs-otel
rollover_if_not 'traces-*.otel-*' traces-otel
# mapping 也只對新的 backing index 生效：trace 的寫入中 index 還沒有 event.ingested（trace-merge 靠它找新 span）的換一個
rollover_if_no_field() {  # rollover_if_no_field <資料流樣式> <欄位>
  local ds wi
  for ds in $(es "${ES_URL}/_data_stream/$1" | jq -r '.data_streams[]? | .name'); do
    wi="$(es "${ES_URL}/_data_stream/${ds}" | jq -r '.data_streams[0].indices[-1].index_name')"
    if [[ "$(es "${ES_URL}/${wi}/_mapping/field/$2" | jq -r --arg f "$2" '.[].mappings[$f].mapping | length')" == "0" ]]; then
      echo "=== rollover ${ds}（寫入中的 index 沒有 $2）==="
      es -X POST "${ES_URL}/${ds}/_rollover" >/dev/null
    fi
  done
}
rollover_if_no_field 'traces-*.otel-*' event.ingested

# failure store：被拒的文件（pipeline 失敗、型別不合）留在 <data stream>::failures。template 只管新建的資料流，
# 已經存在的要另外打開（docs/pdca/phase1-collect/2026-10-ingest-simplify.md）。OTel 的 metrics、traces 資料流 ES 預設就開了
if [[ -n "$(es "${ES_URL}/_data_stream/k8s-*" | jq -r '.data_streams[]? | .name' | head -1)" ]]; then
  es -X PUT "${ES_URL}/_data_stream/k8s-*/_options" -d '{"failure_store": {"enabled": true}}' >/dev/null
fi

# snapshot：人工判斷的索引（回饋、事故紀錄、調整）掉了不能重算，每天備份一次（SLM；天數、索引在 retention.conf）。
# 儲存庫由管 ES 的人決定：SNAPSHOT_REPO_TYPE（fs、s3、azure、gcs…）＋ SNAPSHOT_REPO_SETTINGS（該類型的 settings JSON）。
# 沒給就跳過（印一行提醒）。fs 的 location 要在每個 master、data 節點的 path.repo 底下（測試 chart 是 /snapshots）。
# 還原步驟見 elasticsearch/README.md「備份與還原」
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

# 1. 寫入用：一把，log、metrics、trace、所有 group 共用（都由 OpenTelemetry Collector 送）。
#   metrics-*.otel-*：metrics 的資料流是 metrics-<來源>.otel-<group>
#   logs-*.otel-*：log 的入口，ES 會再轉到 k8s-<group>-*（轉過去時 ES 會再檢查一次權限，所以 k8s-* 也要）
#   traces-*.otel-*：trace（raw，每一段一筆；合成一筆的 traces-merged-* 由 engine 寫，不是這把 key）
writer_key "es-writer" '["k8s-*", "metrics-*.otel-*", "logs-*.otel-*", "traces-*.otel-*"]'

# 2. AIOps 唯讀
echo "=== AIOps 唯讀 key ==="
issue_key reader -d '{
  "name": "aiops-reader",
  "role_descriptors": {
    "aiops-reader": {
      "index": [{"names": ["k8s-*", "metrics-*.otel-*", "traces-*.otel-*", "traces-merged-*"], "privileges": ["read", "view_index_metadata"]}]
    }
  }
}'

# 3. 給人用的唯讀角色
# 2b. AIOps baseline 工具（aiops-engine/code/configs/baseline）：讀 log、metrics、trace，只能寫自己的 aiops-*（baseline、異常）
#     和 traces-merged-*（trace-merge 把每個請求合成一筆）。
#     read_failure_store：quality.ingest_rejected 要數被 ES 拒絕、留在 failure store 的文件
echo "=== AIOps baseline key ==="
issue_key engine -d '{
  "name": "aiops-baseline",
  "role_descriptors": {
    "aiops-baseline": {
      "index": [
        {"names": ["k8s-*", "metrics-*.otel-*", "traces-*.otel-*"], "privileges": ["read", "view_index_metadata", "read_failure_store"]},
        {"names": ["aiops-*", "traces-merged-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}
      ]
    }
  }
}'
echo

# 5. Grafana 唯讀 key：只讀 aiops-alerts（AIOps 合併過的事件），Grafana 依它發通知（grafana/code/）。
#    cluster monitor：Grafana 的 ES data source 會先打 GET / 看版本（實測沒有這個權限 data source 測試回 403）
echo "=== Grafana 唯讀 key ==="
issue_key grafana -d '{
  "name": "grafana-aiops-alerts",
  "role_descriptors": {"grafana-aiops-alerts": {"cluster": ["monitor"], "index": [{"names": ["aiops-alerts"], "privileges": ["read", "view_index_metadata"]}]}}
}'
echo

# 4. 健康檢查 key：只能 GET /（cluster:monitor/main）。放進 ES 前面 LB 的健康檢查、或 ES Pod 的 readinessProbe
#    （e2e/k8s/elasticsearch.yaml 的 Secret es-probe-key）
echo "=== 健康檢查 key ==="
issue_key probe -d '{
  "name": "es-probe",
  "role_descriptors": {"es-probe": {"cluster": ["monitor"]}}
}'
echo

reader_role it
reader_role imc

# CI：把 key 組成各 chart 的 values（chart 用它建 Secret）。collector 是每個應用 cluster 各部署一次，同一個 values 檔給每個 cluster 用
if [[ -n "${KEYS_OUT_DIR:-}" ]]; then
  ( umask 077
    printf 'esWriterApiKey: "%s"\n' "$(cat "${KEYS_OUT_DIR}/writer")" > "${KEYS_OUT_DIR}/collector-values.yaml"
    printf 'esApiKey: "%s"\n' "$(cat "${KEYS_OUT_DIR}/engine")" > "${KEYS_OUT_DIR}/engine-values.yaml"
    # 第一行 secretData: 由部署 job 補上其他欄位（ES_URL、webhook、admin 密碼）；這裡只放 key
    printf 'secretData:\n  GRAFANA_ES_API_KEY: "%s"\n' "$(cat "${KEYS_OUT_DIR}/grafana")" > "${KEYS_OUT_DIR}/grafana-values.yaml" )
  echo "=== key 與 values 檔寫在 ${KEYS_OUT_DIR}/（不印出）==="
fi
