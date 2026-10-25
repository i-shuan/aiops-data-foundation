#!/usr/bin/env bash
# level 萃取與正規化的案例測試：把 level-cases.json 每一行原文丟給 k8s-logs pipeline 的 _simulate，
# 比對 log.level 與 log.level_inferred。不需要 K8s，只要連得到裝好 pipeline 的 ES。
# 用法：ES_URL=http://<ip>:9200 ES_AUTH=elastic:<password> e2e/pipeline/run-level-cases.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH}"

docs=$(jq '{docs: [.[] | {_source: {group: "it", log: .log,
  kubernetes: {namespace_name: "case", container_name: "case",
               labels: (if .format then {"log-format": .format} else {} end)}}}]}' "${here}/level-cases.json")
result=$(curl -sS --fail-with-body -u "${ES_AUTH}" -H 'Content-Type: application/json' \
  "${ES_URL}/_ingest/pipeline/k8s-logs/_simulate" -d "${docs}")

jq -r --slurpfile cases "${here}/level-cases.json" '
  [ .docs | to_entries[] | .key as $i | (.value.doc._source // {}) as $d | $cases[0][$i] as $c
    | ($d.log.level // null) as $lv | ($d.log.level_inferred // false) as $inf
    | (($lv == $c.expect) and ($c.expect == null or $inf == $c.inferred)) as $ok
    | "\(if $ok then "PASS" else "FAIL" end)\t\($c.group)\t\($c.log | .[0:60])\t\($lv // "（無）")\(if $inf then "（猜的）" else "" end)" ]
  | .[]' <<<"${result}" | tee /dev/stderr | grep -c '^FAIL' >/dev/null && { echo "有案例失敗"; exit 1; } || true
echo "全部 $(jq length "${here}/level-cases.json") 個案例通過"
