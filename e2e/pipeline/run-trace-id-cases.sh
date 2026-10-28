#!/usr/bin/env bash
# log 的 trace.id／span.id 在各種格式、各種值下取得對不對（docs/pdca/phase1-collect/2026-10-log-trace-bind.md）。
# 每個案例做成 Collector 送來的形狀（file_log 讀的、或應用 OTLP 直送的），丟 k8s-logs-otel 的 _simulate，
# 比對 trace.id、span.id、app.trace_id_invalid、app.span_id_invalid、log.source（及案例指定的 @timestamp、app.*）。
# 不需要 K8s，只要連得到裝好 pipeline 的 ES。
# 用法：ES_URL=http://<ip>:9200 ES_AUTH=elastic:<password> e2e/pipeline/run-trace-id-cases.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH}"

# 一筆案例 → Collector 送的 log record。file：attributes 有 log.iostream、log.file.path；otlp：trace_id 等在 record 本身
to_doc='def doc: {_index: "logs-generic.otel-it", _source: ({
    "@timestamp": 1793062923456.0,
    body: {text: .log},
    attributes: (if (.kind // "file") == "file"
                 then {"log.iostream": "stdout", "log.file.path": "/var/log/pods/mes-prod_mes-api-7d9f-abcde_u1/app/0.log"}
                 else {} end),
    resource: {attributes: ({"service.name": "mes-api", "k8s.namespace.name": "mes-prod", "k8s.pod.name": "mes-api-7d9f-abcde",
      "k8s.pod.uid": "u1", "k8s.container.name": "app", "k8s.node.name": "node-1", "k8s.cluster.name": "c1",
      "k8s.pod.labels.app.kubernetes.io/name": "mes-api", dept: "D3120", group: "it"}
      + (if .format then {"k8s.pod.labels.log-format": .format} else {} end))}
  } + (.otel // {}))};'

sim() {
  curl -sS --fail-with-body -u "${ES_AUTH}" -H 'Content-Type: application/json' \
    "${ES_URL}/_ingest/pipeline/k8s-logs-otel/_simulate" -d @-
}

result=$(jq "${to_doc}"' {docs: [.[] | doc]}' "${here}/trace-id-cases.json" | sim)

jq -r --slurpfile cases "${here}/trace-id-cases.json" '
  [ .docs | to_entries[] | .key as $i | (.value.doc._source // {}) as $d | $cases[0][$i] as $c | $c.expect as $e
    | { trace: ($d.trace.id // null), span: ($d.span.id // null),
        trace_invalid: ($d.app.trace_id_invalid // null), span_invalid: ($d.app.span_id_invalid // null),
        source: ($d.log.source // null), timestamp: ($d["@timestamp"] // null) } as $got
    | ([ ($got.trace == ($e.trace // null)), ($got.span == ($e.span // null)),
         ($got.trace_invalid == ($e.trace_invalid // null)), ($got.span_invalid == ($e.span_invalid // null)),
         ($got.source == ($e.source // "file")),
         (if $e.timestamp then $got.timestamp == $e.timestamp else true end),
         (($e.app // {}) | to_entries | all(.value == ($d.app[.key] // null))),
         (.value.error == null) ] | all) as $ok
    | "\(if $ok then "PASS" else "FAIL" end)\t[\($c.group)] \($c.name)\t trace.id=\($got.trace // "（無）") span.id=\($got.span // "（無）")"
      + (if $got.trace_invalid then " trace_id_invalid=\($got.trace_invalid)" else "" end)
      + (if $got.span_invalid then " span_id_invalid=\($got.span_invalid)" else "" end)
      + " source=\($got.source)"
      + (if $e.timestamp then " @timestamp=\($got.timestamp)" else "" end)
      + (if .value.error then " 錯誤=\(.value.error.reason)" else "" end) ]
  | .[]' <<<"${result}" | tee "${TMPDIR:-/tmp}/trace-id-cases.out"
fail=$(grep -c '^FAIL' "${TMPDIR:-/tmp}/trace-id-cases.out" || true)

# ── 檔案和 OTLP 同時開：同一行進來兩份。兩份都綁得到同一個 trace，_id 不同（不會互相去重），用 log.source 分 ──
line='{"@timestamp":"2026-10-27T01:02:03.456Z","level":"INFO","message":"order 5 handled","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7"}'
dual=$(jq -n --arg l "${line}" "${to_doc}"' {docs: [
  ({log: $l} | doc),
  ({log: "order 5 handled", kind: "otlp", otel: {trace_id: "4bf92f3577b34da6a3ce929d0e0e4736", span_id: "00f067aa0ba902b7", severity_text: "INFO"}} | doc)]}' | sim)
got=$(jq -r '[.docs[].doc | "\(._source.log.source):\(._source.trace.id):\(._source.message)"] | join(" | ")' <<<"${dual}")
ids=$(jq -r '[.docs[].doc._id] | unique | length' <<<"${dual}")
want="file:4bf92f3577b34da6a3ce929d0e0e4736:order 5 handled | otlp:4bf92f3577b34da6a3ce929d0e0e4736:order 5 handled"
if [[ "${got}" == "${want}" && "${ids}" == 2 ]]; then
  echo "PASS	[檔案＋OTLP] 同一行兩份：${got}；_id 不同（${ids} 個）"
else
  echo "FAIL	[檔案＋OTLP] 得到 [${got}]、_id ${ids} 個；預期 [${want}]、2 個"; fail=$((fail + 1))
fi

total=$(( $(jq length "${here}/trace-id-cases.json") + 1 ))
if (( fail > 0 )); then echo "${fail}／${total} 個案例失敗"; exit 1; fi
echo "全部 ${total} 個案例通過"
