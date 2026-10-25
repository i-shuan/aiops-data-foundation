#!/usr/bin/env bash
# Elasticsearch 權限設定。group 清單要跟 platform/groups.yaml 一致。
#
# 用法：ES_URL=https://<elasticsearch-host>:9200 ES_AUTH=elastic:<password> ./elasticsearch-setup.sh
#       只更新 index 結構、不發新 key：SKIP_KEYS=1 ./elasticsearch-setup.sh
#       需要 curl、jq。
#
# 會建立四類東西：
#   0. index 結構：ILM、mapping、pipeline（含各 log 格式）、index template（./elasticsearch/）。
#      可以重複執行，結果一樣。log 欄位規範見 docs/log/README.md。
#   1. 寫入用 API key（給 OpenTelemetry Collector）：每組只能寫自己 group 的 log、metrics、trace。
#      label 判斷錯了，頂多是那筆資料被拒絕，不會寫進別的 group。
#      回應裡的 "encoded" 放進每個應用 cluster otel-collector namespace 的 Secret es-writer-keys：
#        IT_ES_API_KEY、IMC_ES_API_KEY、UNASSIGNED_ES_API_KEY
#      之後換 key 用 rotate-writer-key.sh（不掉資料）；重跑這支腳本也會發新 key，但舊的不會作廢。
#   2. AIOps 專用唯讀 API key：可讀所有 k8s-*（log）、metrics-*.otel-*（metrics）、traces-*.otel-*（trace）。
#   3. 給人用的唯讀角色：每個 group 一個，只能看自己的 log、事件、metrics、trace。再指派給使用者。
#   另外發一把 AIOps baseline 工具用的 key（aiops/baseline）：讀上面三種資料，只能寫 aiops-*。
#   4. 健康檢查用 key（es-probe，只能 GET /）：給 ES 前面的 LB／Service 的健康檢查用。
#      ES 剛起來的 1 秒多，API key 會被拒（401）但 elastic 帳號、/_cluster/health 都正常；Collector 收到 401
#      會當成「key 錯了」直接丟資料。所以健康檢查一定要用 API key 打 GET /、等 200 才把流量導過去
#      （範例：elasticsearch/healthcheck-haproxy.cfg；docs/pdca/2026-10-es-startup-401.md）。
set -euo pipefail

cd "$(dirname "$0")"

: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH（有 manage_security 權限的帳號）}"

es() {
  curl -sS --max-time 60 -u "${ES_AUTH}" -H 'Content-Type: application/json' "$@"
  echo
}

writer_key() {
  local name="$1" indices="$2"
  echo "=== 寫入 key：${name} ==="
  es -X POST "${ES_URL}/_security/api_key" -d "{
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
      \"names\": [\"k8s-${group}-*\", \"k8s-events-${group}-*\", \"metrics-*-${group}\", \"traces-*.otel-${group}\"],
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
  local tz_script="elasticsearch/pipeline-tz.painless"
  local src=""
  [[ -f "${painless}" ]] && src="$(cat "${painless}")"
  jq --arg src "${src}" --rawfile tzsrc "${tz_script}" --arg tz "${LOG_DEFAULT_TIMEZONE}" \
    'walk(if . == "__SCRIPT__" and $src != "" then $src elif . == "__TZ_SCRIPT__" then $tzsrc
          elif . == "__LOG_DEFAULT_TIMEZONE__" then $tz else . end)' "${json}" > "${out}"
  put_json "/_ingest/pipeline/${name}" "${out}"
}

# 保留天數：retention.conf 的值填進 ILM 的 JSON（__變數名__ 換成值）
# shellcheck source=retention.conf
source retention.conf
put_ilm() {  # put_ilm <policy 名稱> <JSON 檔>
  local out="${tmp_dir}/ilm-$1.json" v
  cp "$2" "${out}"
  for v in $(grep -oE '^[A-Z_0-9]+' retention.conf); do
    sed -i "s/__${v}__/${!v}/g" "${out}"
  done
  if grep -q '__[A-Z_0-9]*__' "${out}"; then echo "retention.conf 少了：$(grep -o '__[A-Z_0-9]*__' "${out}" | sort -u)"; exit 1; fi
  put_json "/_ilm/policy/$1" "${out}"
}
put_ilm k8s-logs                                   elasticsearch/ilm-policy.json
put_ilm metrics-otel                               elasticsearch/ilm-metrics-otel.json
put_ilm traces-otel                                elasticsearch/ilm-traces-otel.json
put_ilm traces-problem-otel                        elasticsearch/ilm-traces-problem-otel.json
put_ilm aiops-log-stats                            elasticsearch/ilm-aiops-log-stats.json
put_pipeline k8s-logs-fields                       elasticsearch/pipeline-fields.json
# 每種 log 格式一個 pipeline：elasticsearch/formats/<格式>.json → k8s-logs-format-<格式>
for f in elasticsearch/formats/*.json; do
  put_pipeline "k8s-logs-format-$(basename "${f}" .json)" "${f}"
done
put_pipeline k8s-logs                              elasticsearch/pipeline.json
put_json /_component_template/k8s-logs-settings    elasticsearch/component-settings.json
put_json /_component_template/k8s-logs-mappings    elasticsearch/component-mappings.json
put_json /_index_template/k8s-logs                 elasticsearch/index-template.json
# metrics：ES 9 內建 metrics-otel@template 預留的自訂設定（metrics-*.otel-* 的 TSDS）
put_json /_component_template/metrics-otel@custom  elasticsearch/metrics-otel-custom.json
# log 改用 OpenTelemetry Collector 收時（比較中，見 docs/log/fluentbit-vs-otel.md）：
# logs-*.otel-* 先跑 k8s-logs-otel，我們的 Collector 送的轉到 k8s-<group>-<cluster>
put_pipeline k8s-logs-otel                         elasticsearch/pipeline-otel.json
put_json /_component_template/logs-otel@custom     elasticsearch/logs-otel-custom.json
# trace：traces-*.otel-* 用 trace_id + span_id 當 _id，重送不會重複
put_pipeline traces-otel                           elasticsearch/pipeline-traces-otel.json
put_json /_component_template/traces-otel@custom   elasticsearch/traces-otel-custom.json
# 有問題的 trace（traces-otel pipeline 轉到 traces-problem.otel-<group>）：照內建 traces-otel@template 的組成，
# 多疊一個 traces-problem-otel@custom 換保留天數。每次從內建的重新組，ES 升級改了內建組成也會跟上
put_json /_component_template/traces-problem-otel@custom elasticsearch/traces-problem-otel-custom.json
es "${ES_URL}/_index_template/traces-otel@template" | jq '.index_templates[0].index_template
  | .index_patterns = ["traces-problem.otel-*"] | .priority = (.priority + 10)
  | .composed_of += ["traces-problem-otel@custom"] | del(.version, .created_date, .created_date_millis, .modified_date, .modified_date_millis, .deprecated)
  | ._meta = {"description": "有問題的 trace：traces-otel@template 加上 traces-problem-otel@custom（保留天數較長）。由 elasticsearch-setup.sh 產生"}' \
  > "${tmp_dir}/traces-problem-template.json"
put_json /_index_template/traces-problem-otel      "${tmp_dir}/traces-problem-template.json"
# 已經存在的資料也換成新的保留天數（新的 backing index 由上面的 template 帶）
echo "=== 已存在的 metrics、trace 套用新的保留天數 ==="
es -X PUT "${ES_URL}/metrics-*.otel-*/_settings?expand_wildcards=all" -d '{"index.lifecycle.name": "metrics-otel"}' >/dev/null
es -X PUT "${ES_URL}/traces-generic.otel-*/_settings?expand_wildcards=all" -d '{"index.lifecycle.name": "traces-otel"}' >/dev/null
es -X PUT "${ES_URL}/traces-problem.otel-*/_settings?expand_wildcards=all&allow_no_indices=true" -d '{"index.lifecycle.name": "traces-problem-otel"}' >/dev/null
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
put_json /_index_template/aiops-log-stats          elasticsearch/template-aiops-log-stats.json
if [[ "$(curl -s -o /dev/null -w '%{http_code}' -u "${ES_AUTH}" "${ES_URL}/_alias/aiops-log-stats")" != "200" ]]; then
  echo "=== 建 aiops-log-stats-000001 ==="
  es -X PUT "${ES_URL}/aiops-log-stats-000001" -d '{"aliases": {"aiops-log-stats": {"is_write_index": true}}}' >/dev/null
fi
if [[ "$(curl -s -o /dev/null -w '%{http_code}' -u "${ES_AUTH}" "${ES_URL}/_transform/aiops-log-stats")" != "200" ]]; then
  put_json /_transform/aiops-log-stats              elasticsearch/transform-log-stats.json
else  # 已經有了：更新設定（group_by 這類改不了的要先刪掉 transform 再跑）
  echo "=== 更新 transform aiops-log-stats ==="
  jq 'del(.dest, .pivot)' elasticsearch/transform-log-stats.json > "${tmp_dir}/transform-update.json"
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

[[ "${SKIP_KEYS:-}" == "1" ]] && exit 0

# 1. 寫入用
# log、metrics、trace 共用同一把 key（都由 OpenTelemetry Collector 送）。
#   metrics-*-<group>：metrics 的資料流是 metrics-<來源>.otel-<group>
#   logs-*.otel-<group>：log 的入口，ES 會再轉到 k8s-<group>-*；轉過去時 ES 會再檢查一次權限，
#     所以 IT 的 key 送 group=imc 的資料一樣寫不進 IMC
#   traces-*.otel-<group>：trace
writer_key "es-writer-it"         '["k8s-it-*", "k8s-events-it-*", "metrics-*-it", "logs-*.otel-it", "traces-*.otel-it"]'
writer_key "es-writer-imc"        '["k8s-imc-*", "k8s-events-imc-*", "metrics-*-imc", "logs-*.otel-imc", "traces-*.otel-imc"]'
writer_key "es-writer-unassigned" '["k8s-unassigned-*", "k8s-events-unassigned-*", "metrics-*-unassigned", "logs-*.otel-unassigned", "traces-*.otel-unassigned"]'

# 2. AIOps 唯讀
echo "=== AIOps 唯讀 key ==="
es -X POST "${ES_URL}/_security/api_key" -d '{
  "name": "aiops-reader",
  "role_descriptors": {
    "aiops-reader": {
      "index": [{"names": ["k8s-*", "metrics-*.otel-*", "traces-*.otel-*"], "privileges": ["read", "view_index_metadata"]}]
    }
  }
}'

# 3. 給人用的唯讀角色
# 2b. AIOps baseline 工具（aiops/baseline）：讀 log、metrics、trace，只能寫自己的 aiops-*（baseline、異常）
echo "=== AIOps baseline key ==="
es -X POST "${ES_URL}/_security/api_key" -d '{
  "name": "aiops-baseline",
  "role_descriptors": {
    "aiops-baseline": {
      "index": [
        {"names": ["k8s-*", "metrics-*.otel-*", "traces-*.otel-*"], "privileges": ["read", "view_index_metadata"]},
        {"names": ["aiops-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}
      ]
    }
  }
}'
echo

# 4. 健康檢查 key：只能 GET /（cluster:monitor/main）。放進 ES 前面 LB 的健康檢查、或 ES Pod 的 readinessProbe
#    （test/local-k3s/elasticsearch.yaml 的 Secret es-probe-key）
echo "=== 健康檢查 key ==="
es -X POST "${ES_URL}/_security/api_key" -d '{
  "name": "es-probe",
  "role_descriptors": {"es-probe": {"cluster": ["monitor"]}}
}'
echo

reader_role it
reader_role imc
