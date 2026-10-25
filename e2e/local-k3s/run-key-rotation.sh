#!/usr/bin/env bash
# 換 key 不掉資料的測試：log 與 metrics 持續寫入時，用 telemetry/scripts/rotate-writer-key.sh 換寫入 key（所有 group 共用一把）。
# 確認：log 0 遺失 0 重複、IMC 的 metrics 沒有空檔、Collector 沒有重啟、沒有 401、舊 key 已作廢。
#
# 前提：run-e2e.sh、run-metrics-e2e.sh 跑過；要在節點上執行（讀 /var/log/pods）。
# 用法：ES_URL=http://<ip>:9200 e2e/local-k3s/run-key-rotation.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
: "${ES_URL:?請設定 ES_URL}"
A=elastic:changeme-test-only
es() { curl -sS -m 60 -u "${A}" -H 'Content-Type: application/json' "$@"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ts() { date -u +%H:%M:%S; }
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1：得到 [$2]，預期 [$3]"; fail=1; fi; }
check_ge() { if [[ "$2" -ge "$3" ]]; then echo "  PASS  $1（$2）"; else echo "  FAIL  $1：得到 $2，預期至少 $3"; fail=1; fi; }
pods() { kubectl -n otel-collector get pods -o jsonpath='{range .items[*]}{.metadata.name}:{.status.containerStatuses[0].restartCount}{"\n"}{end}' | sort; }

run="rot-$(date +%H%M%S)"
start="$(now)"
pods_before="$(pods)"
old_ids="$(es "${ES_URL}/_security/api_key?name=es-writer&active_only=true" | jq -c '[.api_keys[].id]')"
kubectl apply -f "${here}/load/generator.yaml" >/dev/null
sed -e "s/\${RUN}/${run}/g" -e "s/\${RATE}/200/g" -e "s/\${DURATION}/300/g" "${here}/load/job.tpl.yaml" | kubectl apply -f - >/dev/null
echo "== $(ts) 產生器開始（IMC，每秒 200 行，300 秒，run=${run}）；60 秒後換寫入 key"
sleep 60
ES_URL="${ES_URL}" ES_AUTH="${A}" "${repo}/telemetry/scripts/rotate-writer-key.sh" | sed 's/^/   | /'

until kubectl -n load-test get job "gen-${run}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null | grep -qE 'Complete|Failed'; do sleep 5; done
end="$(now)"
emitted=$(cat /var/log/pods/load-test_gen-${run}-*/gen/*.log | grep -o '"emitted": [0-9]*' | grep -o '[0-9]*')
sleep 30; es -X POST "${ES_URL}/k8s-imc-local,metrics-*.otel-imc/_refresh" >/dev/null

echo
echo "== log（IMC，序號 1～${emitted}）"
q="{\"bool\":{\"filter\":[{\"term\":{\"app.run\":\"${run}\"}},{\"exists\":{\"field\":\"app.seq\"}}]}}"
got=$(es "${ES_URL}/k8s-imc-local/_count" -d "{\"query\":${q}}" | jq .count)
# 不同序號有幾個：逐頁數（cardinality 超過 4 萬是估計值，不準）
uniq=0; after=""
while :; do
  r="$(es "${ES_URL}/k8s-imc-local/_search" -d "{\"size\":0,\"query\":${q},\"aggs\":{\"s\":{\"composite\":{\"size\":10000,
    \"sources\":[{\"seq\":{\"terms\":{\"field\":\"app.seq\"}}}]${after:+,\"after\":${after}}}}}}")"
  uniq=$(( uniq + $(jq '.aggregations.s.buckets | length' <<<"${r}") ))
  after="$(jq -c '.aggregations.s.after_key // empty' <<<"${r}")"; [[ -n "${after}" ]] || break
done
check "沒有遺失" "$(( emitted - uniq ))" 0
check "沒有重複" "$(( got - uniq ))" 0

echo "== metrics（IMC 的測試應用，每 30 秒一筆）"
n=$(es "${ES_URL}/metrics-prometheusreceiver.otel-imc/_count" -d "{\"query\":{\"bool\":{\"filter\":[
  {\"exists\":{\"field\":\"metrics.demo_requests_total\"}},{\"term\":{\"resource.attributes.k8s.namespace.name\":\"imc-wip-prod\"}},
  {\"range\":{\"@timestamp\":{\"gte\":\"${start}\",\"lt\":\"${end}\"}}}]}}}" | jq .count)
check_ge "沒有空檔" "${n}" $(( ($(date -u -d "${end}" +%s) - $(date -u -d "${start}" +%s)) / 30 - 1 ))

echo "== Collector 與 key"
check "Collector 沒有重啟（Pod 與重啟次數都沒變）" "$(pods)" "${pods_before}"
unauthorized=0
for f in /var/log/pods/otel-collector_*/otelcol/*.log; do
  unauthorized=$(( unauthorized + $(awk -v t="${start}" '$1 >= t' "${f}" | grep -cE '401|Unauthorized|unauthorized' || true) ))
done
check "換 key 期間沒有 401" "${unauthorized}" 0
check "舊 key 都已作廢" "$(es "${ES_URL}/_security/api_key?name=es-writer&active_only=true" | jq -c --argjson old "${old_ids}" '[.api_keys[].id] - ([.api_keys[].id] - $old) | length')" 0
check "只剩一把有效的新 key" "$(es "${ES_URL}/_security/api_key?name=es-writer&active_only=true" | jq '.api_keys | length')" 1

echo
if [[ ${fail} -eq 0 ]]; then echo "全部通過"; else echo "有項目失敗"; exit 1; fi
