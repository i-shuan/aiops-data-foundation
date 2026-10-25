#!/usr/bin/env bash
# 換寫入 key（所有 group 共用一把 es-writer），不掉資料、不重啟 Collector。
#
# 用法：ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> platform/central/rotate-writer-key.sh [kube context...]
#   不給 context 就用目前的 context。這把 key 在哪些 cluster 用，就要全部列上，不然沒列到的 cluster 會在
#   舊 key 作廢後寫不進去（而且那段資料會被丟掉）。
#
# 順序（新舊 key 有一段時間同時有效，所以不會有空檔）：
#   1. 記下目前有效的 es-writer（可能不只一把）
#   2. 用同樣的權限發一把新 key
#   3. 每個 cluster 更新 Secret es-writer-keys 裡的 ES_WRITER_API_KEY
#   4. 等每個 cluster 的每個 Collector 都換用新 key（Collector 的 log 印出 refresh token），最多 5 分鐘。
#      有任何一個沒換到就停在這裡，舊 key 不作廢，新舊兩把都有效，不影響寫入
#   5. 作廢第 1 步記下的舊 key
set -euo pipefail
: "${ES_URL:?請設定 ES_URL}" "${ES_AUTH:?請設定 ES_AUTH（例如 elastic:<password>）}"
contexts=("$@"); [[ ${#contexts[@]} -eq 0 ]] && contexts=("$(kubectl config current-context)")
name=es-writer
field=ES_WRITER_API_KEY
NS=otel-collector
es() { curl -sS -m 60 --fail-with-body -u "${ES_AUTH}" -H 'Content-Type: application/json' "$@"; }
ts() { date -u +%H:%M:%S; }

echo "== 1. 目前有效的 ${name}"
current="$(es "${ES_URL}/_security/api_key?name=${name}&active_only=true")"
old_ids="$(jq -c '[.api_keys[].id]' <<<"${current}")"
[[ "$(jq length <<<"${old_ids}")" -gt 0 ]] || { echo "找不到有效的 ${name}，先跑 elasticsearch-setup.sh"; exit 1; }
roles="$(jq -c '.api_keys | max_by(.creation) | .role_descriptors' <<<"${current}")"
echo "   ${old_ids}"

echo "== 2. 發新 key（同樣的權限）"
newkey="$(es -X POST "${ES_URL}/_security/api_key" -d "{\"name\":\"${name}\",\"role_descriptors\":${roles},
  \"metadata\":{\"rotated_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}" | jq -r .encoded)"
[[ -n "${newkey}" && "${newkey}" != null ]] || { echo "發 key 失敗"; exit 1; }

echo "== 3. 更新各 cluster 的 Secret"
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
for ctx in "${contexts[@]}"; do
  kubectl --context "${ctx}" -n "${NS}" patch secret es-writer-keys --type merge \
    -p "{\"data\":{\"${field}\":\"$(printf %s "${newkey}" | base64 -w0)\"}}" >/dev/null
  echo "   $(ts) ${ctx}"
done

echo "== 4. 等每個 Collector 換用新 key（最多 5 分鐘）"
# Collector 換 key 時印：refresh token {"filename": "/etc/es-keys/ES_WRITER_API_KEY"}
refreshed() {  # refreshed <context> <pod>
  local out
  if out="$(kubectl --context "$1" -n "${NS}" logs "$2" --since-time="${since}" 2>/dev/null)"; then
    grep 'refresh token' <<<"${out}" | grep -q "${field}"
  else
    # kubectl logs 不能用時（例如直接在節點上跑）：讀節點上的 Pod log 檔，只看 since 之後的行
    cat /var/log/pods/${NS}_${2#pod/}_*/*/*.log 2>/dev/null \
      | awk -v t="${since}" '$1 >= t' | grep 'refresh token' | grep -q "${field}"
  fi
}
deadline=$(( SECONDS + 300 ))
for ctx in "${contexts[@]}"; do
  for pod in $(kubectl --context "${ctx}" -n "${NS}" get pods -o name); do
    until refreshed "${ctx}" "${pod}"; do
      if (( SECONDS > deadline )); then
        echo "   ${ctx} ${pod} 5 分鐘內沒換到新 key。舊 key 沒有作廢，新舊兩把都有效，寫入不受影響。"
        echo "   查原因後重跑這支腳本（會再發一把新的）；或確認那個 Pod 已經換上後，手動作廢舊 key：${old_ids}"
        exit 1
      fi
      sleep 5
    done
    echo "   $(ts) ${ctx} ${pod#pod/} 已換用新 key"
  done
done

echo "== 5. 作廢舊 key"
# 多等一下，讓用舊 key 已經送出的請求完成
sleep 30
es -X DELETE "${ES_URL}/_security/api_key" -d "{\"ids\":${old_ids}}" | jq -c '{invalidated: .invalidated_api_keys}'
echo "完成：${name} 已換成新 key"
