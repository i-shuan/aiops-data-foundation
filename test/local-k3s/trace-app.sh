#!/bin/sh
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
while true; do
  tid=$(hex 16); sid=$(hex 8)
  start="$(date +%s)000000000"
  sleep 1
  end="$(date +%s)000000000"
  echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",\"level\":\"INFO\",\"message\":\"order handled\",\"trace_id\":\"${tid}\",\"span_id\":\"${sid}\"}"
  curl -s -o /dev/null -w '' -X POST -H 'Content-Type: application/json' \
    "http://otel-node-collector.otel-collector.svc:4318/v1/traces" -d "{\"resourceSpans\":[{
      \"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"trace-app\"}}${SPOOF}]},
      \"scopeSpans\":[{\"scope\":{\"name\":\"trace-app\"},\"spans\":[{\"traceId\":\"${tid}\",\"spanId\":\"${sid}\",
        \"name\":\"GET /orders\",\"kind\":2,\"startTimeUnixNano\":\"${start}\",\"endTimeUnixNano\":\"${end}\",
        \"attributes\":[{\"key\":\"http.request.method\",\"value\":{\"stringValue\":\"GET\"}},
                        {\"key\":\"http.response.status_code\",\"value\":{\"intValue\":\"200\"}}],
        \"status\":{}}]}]}]}" || echo "send failed" >&2
  sleep 4
done
