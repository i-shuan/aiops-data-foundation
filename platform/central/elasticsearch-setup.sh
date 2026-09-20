#!/usr/bin/env bash
# Elasticsearch 權限設定。group 清單要跟 platform/groups.yaml 一致。
#
# 用法：ES_URL=https://<elasticsearch-host>:9200 ES_AUTH=elastic:<password> ./elasticsearch-setup.sh
#
# 會建立三類東西：
#   1. 寫入用 API key（給 Fluent Bit）：每組只能寫自己 group 的 index。
#      label 判斷錯了，頂多是那筆資料被拒絕，不會寫進別的 group。
#      回應裡的 "encoded" 放進每個應用 cluster logging namespace 的 Secret es-writer-keys：
#        IT_ES_API_KEY、IMC_ES_API_KEY、UNASSIGNED_ES_API_KEY
#   2. AIOps 專用唯讀 API key：可讀所有 k8s-* index。
#   3. 給人用的唯讀角色：每個 group 一個，只能看自己的 log 與事件。再指派給使用者。
set -euo pipefail

: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH（有 manage_security 權限的帳號）}"

es() {
  curl -sS -u "${ES_AUTH}" -H 'Content-Type: application/json' "$@"
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
