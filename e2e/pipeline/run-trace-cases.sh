#!/usr/bin/env bash
# log ↔ trace 綁定的案例測試：各種 log 輸出格式丟給 k8s-logs pipeline 的 _simulate，看 trace.id、span.id 拿不拿得到
#（docs/pdca/phase1-collect/2026-10-ingest-keys-check.md）。不需要 K8s，只要連得到裝好 pipeline 的 ES。
# 用法：ES_URL=http://<ip>:9200 ES_AUTH=elastic:<password> e2e/pipeline/run-trace-cases.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${ES_URL:?請設定 ES_URL}"
: "${ES_AUTH:?請設定 ES_AUTH}"

docs=$(jq '{docs: [.[] | {_source: {group: "it", log: .log,
  kubernetes: {namespace_name: "mes-prod", pod_name: "mes-api-7d9f-abcde", container_name: "app",
               labels: {"app.kubernetes.io/name": "mes-api"}}}}]}' "${here}/trace-cases.json")
result=$(curl -sS --fail-with-body -u "${ES_AUTH}" -H 'Content-Type: application/json' \
  "${ES_URL}/_ingest/pipeline/k8s-logs/_simulate" -d "${docs}")

jq -r --slurpfile cases "${here}/trace-cases.json" '
  [ .docs | to_entries[] | .key as $i | (.value.doc._source // {}) as $d | $cases[0][$i] as $c
    | ($d.trace.id // null) as $t | ($d.span.id // null) as $s | ($d.log.level // null) as $lv
    | (($t == $c.trace_id) and ($s == $c.span_id) and ($lv == $c.level)) as $ok
    | "\(if $ok then "PASS" else "FAIL" end)\t\($c.group)\t trace.id=\($t // "（無）") span.id=\($s // "（無）") level=\($lv // "（無）")" ]
  | .[]' <<<"${result}" | tee /dev/stderr | grep -c '^FAIL' >/dev/null && { echo "有案例失敗"; exit 1; } || true
echo "全部 $(jq length "${here}/trace-cases.json") 個案例通過（最後兩個「綁不起來」是預期的：沒有 trace_id 才對）"

# ── 第二段：agent 用 OTLP 直接送的 log（走 k8s-logs-otel → k8s-logs）：trace_id 在 log record 本身，不在原文裡 ──
otel_doc='{"docs":[{"_index":"logs-generic.otel-it","_source":{"@timestamp":1793075676692.0,"severity_text":"ERROR","severity_number":17,
  "trace_id":"4bf92f3577b34da6a3ce929d0e0e4799","span_id":"00f067aa0ba902d9",
  "body":{"text":"Connection pool timeout waiting for connection to oracle-1:1521"},
  "attributes":{"lot_id":"L123","thread.name":"http-nio-8080-exec-3"},
  "resource":{"attributes":{"service.name":"mes-api","k8s.namespace.name":"mes-prod","k8s.pod.name":"mes-api-7d9f-abcde","k8s.node.name":"node-1",
    "k8s.pod.labels.app.kubernetes.io/name":"mes-api","dept":"D3120","group":"it","k8s.cluster.name":"c1"}}}}]}'
r=$(curl -sS --fail-with-body -u "${ES_AUTH}" -H 'Content-Type: application/json' "${ES_URL}/_ingest/pipeline/k8s-logs-otel/_simulate" -d "${otel_doc}")
got=$(jq -r '.docs[0].doc | "\(._index) \(._source.trace.id) \(._source.span.id) \(._source.log.level) \(._source.service.name) \(._source.app.lot_id)"' <<<"${r}")
want="k8s-it-c1 4bf92f3577b34da6a3ce929d0e0e4799 00f067aa0ba902d9 error mes-api L123"
if [[ "${got}" == "${want}" ]]; then echo "PASS	OTLP 直送的 log（純文字 body）：trace.id、span.id、level、service.name、自訂欄位都從 log record 補上"; else echo "FAIL	OTLP 直送的 log：得到 [${got}]，預期 [${want}]"; exit 1; fi

# ── 第三段：ES 的 OTLP trace 入口把 span 的例外事件另外存成一筆 log record（沒有 body）：要補 message、level ──
ev_doc='{"docs":[{"_index":"logs-generic.otel-it","_source":{"@timestamp":1793075676692.0,"trace_id":"4bf92f3577b34da6a3ce929d0e0e4799","span_id":"00f067aa0ba902d9",
  "attributes":{"event.name":"exception","exception.type":"java.sql.SQLTransientConnectionException","exception.message":"Connection is not available"},
  "resource":{"attributes":{"service.name":"mes-api","k8s.namespace.name":"mes-prod","k8s.pod.name":"mes-api-7d9f-abcde","k8s.pod.labels.app.kubernetes.io/name":"mes-api","dept":"D3120","group":"it","k8s.cluster.name":"c1"}}}}]}'
r=$(curl -sS --fail-with-body -u "${ES_AUTH}" -H 'Content-Type: application/json' "${ES_URL}/_ingest/pipeline/k8s-logs-otel/_simulate" -d "${ev_doc}")
got=$(jq -r '.docs[0].doc._source | "\(.trace.id) \(.log.level) \(.error.type) | \(.message)"' <<<"${r}")
want="4bf92f3577b34da6a3ce929d0e0e4799 error java.sql.SQLTransientConnectionException | java.sql.SQLTransientConnectionException: Connection is not available"
if [[ "${got}" == "${want}" ]]; then echo "PASS	span 的例外事件變成的 log record：補上 message、level error、error.type，帶 trace.id"; else echo "FAIL	例外事件：得到 [${got}]，預期 [${want}]"; exit 1; fi
