#!/bin/sh
# log ↔ trace 綁定的測試應用（run-trace-id-e2e.sh）：每 5 秒「處理一個請求」——
# 用 OTLP 送一段 span（狀態 ERROR，trace-sampler 一定留），再用同一個 trace_id 印幾種 log。
# 每筆 log 帶 case、tid（進 ES 後是 app.case、app.tid），檢查時用它找。
#   upper：trace_id、span_id 印成大寫
#   long ：一行超過 16KB（containerd 會切成好幾段，Collector 要接回來）
#   bad  ：trace_id 多兩個字（格式錯）
#   background：背景工作，沒有 trace_id
#   dual ：同一行 stdout 印一份、OTLP 直送一份（應用開了 otel.logs.exporter: otlp 的情況）
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
filler=$(head -c 20000 /dev/zero | tr '\0' 'x')
collector=http://otel-node-collector.otel-collector.svc:4318
while true; do
  tid=$(hex 16); sid=$(hex 8)
  TID=$(echo "${tid}" | tr a-f A-F); SID=$(echo "${sid}" | tr a-f A-F)
  start="$(date +%s)000000000"; ts=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' "${collector}/v1/traces" -d "{\"resourceSpans\":[{
    \"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"logtrace-app\"}}]},
    \"scopeSpans\":[{\"scope\":{\"name\":\"logtrace-app\"},\"spans\":[{\"traceId\":\"${tid}\",\"spanId\":\"${sid}\",
      \"name\":\"GET /lots/{id}\",\"kind\":2,\"startTimeUnixNano\":\"${start}\",\"endTimeUnixNano\":\"${start}\",
      \"status\":{\"code\":2}}]}]}]}" || echo "send span failed" >&2

  echo "{\"timestamp\":\"${ts}\",\"level\":\"ERROR\",\"message\":\"lot not found\",\"case\":\"upper\",\"tid\":\"${tid}\",\"trace_id\":\"${TID}\",\"span_id\":\"${SID}\"}"
  echo "{\"timestamp\":\"${ts}\",\"level\":\"INFO\",\"message\":\"big payload\",\"case\":\"long\",\"tid\":\"${tid}\",\"filler\":\"${filler}\",\"trace_id\":\"${tid}\",\"span_id\":\"${sid}\"}"
  echo "{\"timestamp\":\"${ts}\",\"level\":\"INFO\",\"message\":\"bad id\",\"case\":\"bad\",\"tid\":\"${tid}\",\"trace_id\":\"${tid}zz\"}"
  echo "{\"timestamp\":\"${ts}\",\"level\":\"INFO\",\"message\":\"nightly purge\",\"case\":\"background\",\"tid\":\"${tid}\"}"
  echo "{\"timestamp\":\"${ts}\",\"level\":\"INFO\",\"message\":\"lot handled\",\"case\":\"dual\",\"tid\":\"${tid}\",\"trace_id\":\"${tid}\",\"span_id\":\"${sid}\"}"
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' "${collector}/v1/logs" -d "{\"resourceLogs\":[{
    \"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"logtrace-app\"}}]},
    \"scopeLogs\":[{\"scope\":{\"name\":\"logtrace-app\"},\"logRecords\":[{\"timeUnixNano\":\"${start}\",\"severityText\":\"INFO\",
      \"body\":{\"stringValue\":\"lot handled\"},\"traceId\":\"${tid}\",\"spanId\":\"${sid}\",
      \"attributes\":[{\"key\":\"case\",\"value\":{\"stringValue\":\"dual\"}},{\"key\":\"tid\",\"value\":{\"stringValue\":\"${tid}\"}}]}]}]}]}" \
    || echo "send log failed" >&2
  sleep 5
done
