#!/usr/bin/env bash
# 時間解析的案例測試：應用自己寫的時間（帶時區、沒帶時區、各種格式、epoch、不認得）丟給 k8s-logs pipeline 的 _simulate，
# 比對 @timestamp（一律 UTC）和 log.timestamp_source。Collector 讀到的時間固定是 2026-10-24T02:00:05Z（台北 10:00:05）。
# 不需要 K8s，只要連得到裝好 pipeline 的 ES（elasticsearch-setup.sh 用預設的 LOG_DEFAULT_TIMEZONE=Asia/Taipei）。
# 用法：ES_URL=http://<ip>:9200 ES_AUTH=elastic:<password> e2e/pipeline/run-timestamp-cases.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH}"

docs=$(jq '{docs: [.[] | {_source: {group: "it", "@timestamp": "2026-10-24T02:00:05.000Z",
  log: (if .ts then {timestamp: .ts, level: "INFO", message: "x"} else {level: "INFO", message: "x"} end | tojson),
  kubernetes: {namespace_name: "case", container_name: "case", labels: {}}}}]}' "${here}/timestamp-cases.json")
result=$(curl -sS --fail-with-body -u "${ES_AUTH}" -H 'Content-Type: application/json' \
  "${ES_URL}/_ingest/pipeline/k8s-logs/_simulate" -d "${docs}")

jq -r --slurpfile cases "${here}/timestamp-cases.json" '
  .docs | to_entries[] | .value.doc._source as $d | $cases[0][.key] as $c
  | (($d["@timestamp"] == $c.want) and ($d.log.timestamp_source == $c.src)
     and ($d.event.created == "2026-10-24T02:00:05.000Z")) as $ok
  | "\(if $ok then "PASS" else "FAIL" end)\t\($c.name)\t\($c.ts // "（沒寫）")\t→ \($d["@timestamp"])\t\($d.log.timestamp_source)\t\($d.event.timezone // "")"' \
  <<<"${result}" | tee /dev/stderr | grep -q '^FAIL' && { echo "有案例失敗"; exit 1; } || true
echo "全部 $(jq length "${here}/timestamp-cases.json") 個案例都對"
