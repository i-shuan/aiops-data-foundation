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
#   1. 寫入用 API key（給 Fluent Bit）：每組只能寫自己 group 的 index。
#      label 判斷錯了，頂多是那筆資料被拒絕，不會寫進別的 group。
#      回應裡的 "encoded" 放進每個應用 cluster logging namespace 的 Secret es-writer-keys：
#        IT_ES_API_KEY、IMC_ES_API_KEY、UNASSIGNED_ES_API_KEY
#   2. AIOps 專用唯讀 API key：可讀所有 k8s-* index。
#   3. 給人用的唯讀角色：每個 group 一個，只能看自己的 log 與事件。再指派給使用者。
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
      \"names\": [\"k8s-${group}-*\", \"k8s-events-${group}-*\"],
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

[[ "${SKIP_KEYS:-}" == "1" ]] && exit 0

# 1. 寫入用
writer_key "fluent-bit-it"         '["k8s-it-*", "k8s-events-it-*"]'
writer_key "fluent-bit-imc"        '["k8s-imc-*", "k8s-events-imc-*"]'
writer_key "fluent-bit-unassigned" '["k8s-unassigned-*", "k8s-events-unassigned-*"]'

# 2. AIOps 唯讀
echo "=== AIOps 唯讀 key ==="
es -X POST "${ES_URL}/_security/api_key" -d '{
  "name": "aiops-reader",
  "role_descriptors": {
    "aiops-reader": {
      "index": [{"names": ["k8s-*"], "privileges": ["read", "view_index_metadata"]}]
    }
  }
}'

# 3. 給人用的唯讀角色
reader_role it
reader_role imc
