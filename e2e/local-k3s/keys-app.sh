#!/bin/sh
# 共同鑰匙圈的測試應用（keys-apps.yaml；run-keys-e2e.sh 建成 ConfigMap keys-app）。
# 每 3 秒「處理一個請求」，同一個請求同時產生五種資料，全部帶 case=<情境>（不是鑰匙的欄位，用來找回這筆）：
#   trace       OTLP span 送同節點的 Collector
#   log_stdout  stdout 一行 JSON（帶 trace_id），Collector 從容器 log 檔讀
#   log_otlp    OTLP log record（帶 trace_id），走 logs/otlp
#   met_prom    /metrics 給 Collector 抓（Prometheus）
#   met_otlp    OTLP metrics 推給 Collector
# 環境變數：CASE、SVC（應用自己設的 service.name，空的就不帶）、SPOOF（自帶的資源欄位，OTLP JSON，以逗號開頭）、
#   LOG_SPOOF（stdout JSON 多帶的欄位，以逗號開頭）、PROM_LABELS（Prometheus 多帶的 label，以逗號開頭）、PROM_PORT
OTEL=http://otel-node-collector.otel-collector.svc:4318
PORT=${PROM_PORT:-9464}
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
s() { echo "{\"key\":\"$1\",\"value\":{\"stringValue\":\"$2\"}}"; }
post() { wget -q -O /dev/null -T 5 --header 'Content-Type: application/json' --post-data "$2" "$OTEL/v1/$1" >/dev/null 2>&1 || echo "send $1 failed" >&2; }

res="{\"attributes\":[$(s case "$CASE")${SVC:+,$(s service.name "$SVC")}${SPOOF}]}"
mkdir -p /www
trap 'exit 0' TERM   # 測「容器重啟」時用 kill 1 結束（sh 當 PID 1 預設不理 TERM）
httpd -p "$PORT" -h /www
start="$(date +%s)000000000"
n=0
while true; do
  n=$((n+1)); tid=$(hex 16); sid=$(hex 8); t="$(date +%s)000000000"
  # 訊息帶情境名稱：對不到 Pod 的 OTLP log（hostNetwork）內容、時間一樣時，ES 去重複會把兩個 Pod 的當成同一筆（keys-k8s 的 Act）
  echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",\"level\":\"INFO\",\"message\":\"request $n ($CASE)\",\"trace_id\":\"$tid\",\"span_id\":\"$sid\",\"case\":\"$CASE\",\"src\":\"stdout\"${LOG_SPOOF}}"
  # 狀態碼 500：讓 trace-sampler 一定留（http-5xx 規則），不受隨機取樣比例影響
  post traces "{\"resourceSpans\":[{\"resource\":$res,\"scopeSpans\":[{\"scope\":{\"name\":\"keys-app\"},\"spans\":[{\"traceId\":\"$tid\",\"spanId\":\"$sid\",\"name\":\"GET /keys\",\"kind\":2,\"startTimeUnixNano\":\"$t\",\"endTimeUnixNano\":\"$t\",\"attributes\":[$(s case "$CASE"),{\"key\":\"http.response.status_code\",\"value\":{\"intValue\":\"500\"}}]}]}]}]}"
  post logs "{\"resourceLogs\":[{\"resource\":$res,\"scopeLogs\":[{\"scope\":{\"name\":\"keys-app\"},\"logRecords\":[{\"timeUnixNano\":\"$t\",\"severityText\":\"INFO\",\"body\":{\"stringValue\":\"request $n ($CASE)\"},\"traceId\":\"$tid\",\"spanId\":\"$sid\",\"attributes\":[$(s case "$CASE"),$(s src otlp)${SPOOF}]}]}]}]}"
  post metrics "{\"resourceMetrics\":[{\"resource\":$res,\"scopeMetrics\":[{\"scope\":{\"name\":\"keys-app\"},\"metrics\":[{\"name\":\"keys.otlp.requests\",\"sum\":{\"aggregationTemporality\":2,\"isMonotonic\":true,\"dataPoints\":[{\"asInt\":\"$n\",\"startTimeUnixNano\":\"$start\",\"timeUnixNano\":\"$t\",\"attributes\":[$(s case "$CASE")]}]}}]}]}]}"
  printf '# TYPE keys_requests_total counter\nkeys_requests_total{case="%s"%s} %d\n' "$CASE" "$PROM_LABELS" "$n" > /www/metrics.tmp && mv /www/metrics.tmp /www/metrics
  sleep 3
done
