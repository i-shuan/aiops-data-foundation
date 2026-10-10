{{- define "app-common.deployment" -}}
{{- include "app-common.validate" . -}}
{{- $javaAgent := include "app-common.javaAgentEnabled" . -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "app-common.name" . }}
  labels:
    {{- include "app-common.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicas | default 2 }}
  selector:
    matchLabels:
      {{- include "app-common.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      annotations:
        # 平台的 OpenTelemetry Collector 依這三個標註找到要抓的 metrics，見 docs/metrics.md
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ include "app-common.metricsPort" . | quote }}
        prometheus.io/path: /metrics
      labels:
        {{- include "app-common.labels" . | nindent 8 }}
        {{- with .Values.logFormat }}
        # 中央 ES 依這個 label 選擇 log 的解析規則，見 docs/elasticsearch.md「pipeline」
        log-format: {{ . | quote }}
        {{- end }}
    spec:
      {{- if $javaAgent }}
      initContainers:
        # 把 OpenTelemetry Java agent 複製到共用 volume，程式本身不用改就能輸出標準 metrics 與 trace_id
        - name: otel-java-agent
          image: {{ .Values.otel.javaAgent.image }}
          command: ["cp", "/javaagent.jar", "/otel/opentelemetry-javaagent.jar"]
          volumeMounts:
            - name: otel-agent
              mountPath: /otel
      {{- end }}
      containers:
        - name: {{ include "app-common.name" . }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          ports:
            - name: http
              containerPort: {{ include "app-common.httpPort" . }}
            - name: metrics
              containerPort: {{ include "app-common.metricsPort" . }}
          env:
            # OpenTelemetry 標準設定：服務名稱、版本、trace 傳遞格式由平台統一
            - name: OTEL_SERVICE_NAME
              value: {{ include "app-common.name" . | quote }}
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: {{ printf "service.version=%s,service.namespace=%s" (include "app-common.version" .) .Values.app.partOf | quote }}
            - name: OTEL_PROPAGATORS
              value: tracecontext,baggage
            # metrics 以 Prometheus 格式開在 metrics port，由平台的 OpenTelemetry Collector 來抓
            - name: OTEL_METRICS_EXPORTER
              value: prometheus
            - name: OTEL_EXPORTER_PROMETHEUS_HOST
              value: 0.0.0.0
            - name: OTEL_EXPORTER_PROMETHEUS_PORT
              value: {{ include "app-common.metricsPort" . | quote }}
            # trace 送到同一個節點上的平台 Collector（Service 只連同節點），由它判斷屬於哪個部門再送進 ES。
            # trace_id 同時會寫進 log，用來把 log 和 trace 串起來
            - name: OTEL_TRACES_EXPORTER
              value: otlp
            - name: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
              value: http://otel-node-collector.otel-collector.svc:4318/v1/traces
            - name: OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
              value: http/protobuf
            # 取樣：上游已決定要不要記的跟著上游；自己開始的請求依比例（otel.traces.sampleRatio，預設全部）
            - name: OTEL_TRACES_SAMPLER
              value: parentbased_traceidratio
            - name: OTEL_TRACES_SAMPLER_ARG
              value: {{ include "app-common.traceSampleRatio" . | quote }}
            - name: OTEL_LOGS_EXPORTER
              value: none
            {{- if $javaAgent }}
            - name: JAVA_TOOL_OPTIONS
              value: -javaagent:/otel/opentelemetry-javaagent.jar
            {{- end }}
            {{- with .Values.env }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          readinessProbe:
            {{- toYaml .Values.probes.readiness | nindent 12 }}
          livenessProbe:
            {{- toYaml .Values.probes.liveness | nindent 12 }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          {{- if $javaAgent }}
          volumeMounts:
            - name: otel-agent
              mountPath: /otel
          {{- end }}
      {{- if $javaAgent }}
      volumes:
        - name: otel-agent
          emptyDir: {}
      {{- end }}
{{- end -}}
