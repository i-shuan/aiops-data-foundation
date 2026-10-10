# rca-apps.yaml 兩個模擬服務共用：時間、span、送到 Collector、印 log。run-rca-e2e.sh 建成 ConfigMap rca-lib。
OTEL=http://otel-node-collector.otel-collector.svc:4318/v1/traces
BT=$(awk '/btime/{print $2}' /proc/stat)
now() { read up _ < /proc/uptime; echo $(( BT * 1000 + ${up%.*}${up#*.} * 10 )); }
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
# span <trace> <span> <parent> <name> <kind 2=server 3=client> <開始 ms> <結束 ms> <attributes JSON> <錯誤 1/空>
span() {
  st='{}'; [ -n "$9" ] && st='{"code":2}'
  echo "{\"traceId\":\"$1\",\"spanId\":\"$2\",\"parentSpanId\":\"$3\",\"name\":\"$4\",\"kind\":$5,\"startTimeUnixNano\":\"${6}000000\",\"endTimeUnixNano\":\"${7}000000\",\"attributes\":[$8],\"status\":$st}"
}
# send <service.name> <span JSON,...>：背景送出，不影響回應時間
send() {
  wget -q -O /dev/null -T 5 --header 'Content-Type: application/json' \
    --post-data "{\"resourceSpans\":[{\"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"$1\"}}]},\"scopeSpans\":[{\"scope\":{\"name\":\"rca-sim\"},\"spans\":[$2]}]}]}" \
    "$OTEL" >/dev/null 2>&1 </dev/null &
}
attr() { echo "{\"key\":\"$1\",\"value\":{\"stringValue\":\"$2\"}}"; }
log() { echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",\"level\":\"$1\",\"message\":\"$2\",\"trace_id\":\"$3\",\"span_id\":\"$4\"}"; }
