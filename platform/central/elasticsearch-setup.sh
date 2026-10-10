#!/usr/bin/env bash
# Elasticsearch 權限設定。group 清單要跟 platform/groups.yaml 一致。
#
# 用法：ES_URL=https://<elasticsearch-host>:9200 ES_AUTH=elastic:<password> ./elasticsearch-setup.sh
#       只更新 index 結構、不發新 key：SKIP_KEYS=1 ./elasticsearch-setup.sh
#       需要 curl、jq。
#
# 會建立四類東西：
#   0. index 結構：ILM、mapping、pipeline（含各 log 格式）、index template（./elasticsearch/）。
#      可以重複執行，結果一樣。log 欄位規範見 docs/elasticsearch.md。
#   1. 寫入用 API key（給 OpenTelemetry Collector）：每組只能寫自己 group 的 log、metrics、trace。
#      label 判斷錯了，頂多是那筆資料被拒絕，不會寫進別的 group。
#      回應裡的 "encoded" 放進每個應用 cluster otel-collector namespace 的 Secret es-writer-keys：
#        IT_ES_API_KEY、IMC_ES_API_KEY、UNASSIGNED_ES_API_KEY
#      之後換 key 用 rotate-writer-key.sh（不掉資料）；重跑這支腳本也會發新 key，但舊的不會作廢。
#   2. AIOps 專用唯讀 API key：可讀所有 k8s-*（log）、metrics-*.otel-*（metrics）、traces-*.otel-*（trace）。
#   3. 給人用的唯讀角色：每個 group 一個，只能看自己的 log、事件、metrics、trace。再指派給使用者。
#   另外發一把 AIOps baseline 工具用的 key（aiops/baseline）：讀上面三種資料，只能寫 aiops-*。
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

# pipeline 的 JSON 裡 "__SCRIPT__" 換成同名 .painless 的內容（沒有 .painless 就原樣送出）
put_pipeline() {
  local name="$1" json="$2" painless="${2%.json}.painless" out="${tmp_dir}/$1.json"
  if [[ -f "${painless}" ]]; then
    jq --rawfile src "${painless}" 'walk(if . == "__SCRIPT__" then $src else . end)' "${json}" > "${out}"
  else
    cp "${json}" "${out}"
  fi
  put_json "/_ingest/pipeline/${name}" "${out}"
}

put_json /_ilm/policy/k8s-logs                     elasticsearch/ilm-policy.json
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
# log 改用 OpenTelemetry Collector 收時（比較中，見 docs/otel-logs-compare.md）：
# logs-*.otel-* 先跑 k8s-logs-otel，我們的 Collector 送的轉到 k8s-<group>-<cluster>
put_pipeline k8s-logs-otel                         elasticsearch/pipeline-otel.json
put_json /_component_template/logs-otel@custom     elasticsearch/logs-otel-custom.json
# trace：traces-*.otel-* 用 trace_id + span_id 當 _id，重送不會重複
put_pipeline traces-otel                           elasticsearch/pipeline-traces-otel.json
put_json /_component_template/traces-otel@custom   elasticsearch/traces-otel-custom.json
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

reader_role it
reader_role imc
