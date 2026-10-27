{{- define "app-common.deployment" -}}
{{- include "app-common.validate" . -}}
{{- $javaAgent := include "app-common.javaAgentEnabled" . -}}
{{- $dotnet := include "app-common.dotnetEnabled" . -}}
{{- $dotnetArch := ((.Values.otel).dotnet).arch | default "linux-x64" -}}
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
      {{- if not $dotnet }}
      annotations:
        # 平台的 OpenTelemetry Collector 依這三個標註找到要抓的 metrics，見 docs/metrics/README.md
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ include "app-common.metricsPort" . | quote }}
        prometheus.io/path: /metrics
      {{- end }}
      labels:
        {{- include "app-common.labels" . | nindent 8 }}
        {{- with .Values.logFormat }}
        # 中央 ES 依這個 label 選擇 log 的解析規則，見 docs/log/README.md「pipeline」
        log-format: {{ . | quote }}
        {{- end }}
    spec:
      {{- if $dotnet }}
      initContainers:
        # 把 OpenTelemetry .NET 自動埋點複製到共用 volume，程式本身不用改（見 docs/metrics/dotnet.md）
        - name: otel-dotnet
          image: {{ .Values.otel.dotnet.image }}
          command: ["cp", "-r", "/autoinstrumentation/.", "/otel/"]
          volumeMounts:
            - name: otel-agent
              mountPath: /otel
      {{- end }}
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
            {{- if not $dotnet }}
            - name: metrics
              containerPort: {{ include "app-common.metricsPort" . }}
            {{- end }}
          env:
            # OpenTelemetry 標準設定：服務名稱、版本、trace 傳遞格式由平台統一
            - name: OTEL_SERVICE_NAME
              value: {{ include "app-common.name" . | quote }}
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: {{ printf "service.version=%s,service.namespace=%s" (include "app-common.version" .) .Values.app.partOf | quote }}
            - name: OTEL_PROPAGATORS
              value: tracecontext,baggage
            {{- if $dotnet }}
            # .NET：metrics 用 OTLP 推給同節點的 Collector（.NET 自動埋點的 Prometheus 端點只聽 localhost，Collector 抓不到）
            - name: OTEL_METRICS_EXPORTER
              value: otlp
            - name: OTEL_EXPORTER_OTLP_METRICS_ENDPOINT
              value: http://otel-node-collector.otel-collector.svc:4318/v1/metrics
            - name: OTEL_METRIC_EXPORT_INTERVAL
              value: "30000"
            {{- else }}
            # metrics 以 Prometheus 格式開在 metrics port，由平台的 OpenTelemetry Collector 來抓
            - name: OTEL_METRICS_EXPORTER
              value: prometheus
            - name: OTEL_EXPORTER_PROMETHEUS_HOST
              value: 0.0.0.0
            - name: OTEL_EXPORTER_PROMETHEUS_PORT
              value: {{ include "app-common.metricsPort" . | quote }}
            {{- end }}
            # trace 送到同一個節點上的平台 Collector（Service 只連同節點），由它判斷屬於哪個部門再送進 ES。
            # trace_id 同時會寫進 log，用來把 log 和 trace 串起來
            - name: OTEL_TRACES_EXPORTER
              value: otlp
            - name: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
              value: http://otel-node-collector.otel-collector.svc:4318/v1/traces
            - name: OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
              value: http/protobuf
            # 取樣：上游已決定要不要記的跟著上游；自己開始的請求依比例（otel.traces.sampleRatio，預設全部送，
            # 平台的 trace-sampler 在請求結束後才決定留不留，見 docs/trace/README.md）
            - name: OTEL_TRACES_SAMPLER
              value: parentbased_traceidratio
            - name: OTEL_TRACES_SAMPLER_ARG
              value: {{ include "app-common.traceSampleRatio" . | quote }}
            # log：預設走 stdout（平台從容器 log 檔收，trace_id 要 log 格式有印 MDC／scope）。
            # 應用的 log pattern 不印 trace_id、又不能改時，設 otel.logs.exporter: otlp：agent 把 log 直接送給 Collector，
            # 每筆都帶 trace_id（stdout 照印，平台會收到兩份，所以只在需要時開）
            - name: OTEL_LOGS_EXPORTER
              value: {{ include "app-common.logsExporter" . | quote }}
            {{- if eq (include "app-common.logsExporter" .) "otlp" }}
            - name: OTEL_EXPORTER_OTLP_LOGS_ENDPOINT
              value: http://otel-node-collector.otel-collector.svc:4318/v1/logs
            - name: OTEL_EXPORTER_OTLP_LOGS_PROTOCOL
              value: http/protobuf
            {{- end }}
            {{- if $javaAgent }}
            - name: JAVA_TOOL_OPTIONS
              value: -javaagent:/otel/opentelemetry-javaagent.jar
            {{- end }}
            {{- if $dotnet }}
            # OpenTelemetry .NET 自動埋點（CLR profiler ＋ startup hook）
            - {name: CORECLR_ENABLE_PROFILING, value: "1"}
            - {name: CORECLR_PROFILER, value: "{918728DD-259F-4A6A-AC2B-B85E1B658318}"}
            - {name: CORECLR_PROFILER_PATH, value: "/otel/{{ $dotnetArch }}/OpenTelemetry.AutoInstrumentation.Native.so"}
            - {name: DOTNET_ADDITIONAL_DEPS, value: /otel/AdditionalDeps}
            - {name: DOTNET_SHARED_STORE, value: /otel/store}
            - {name: DOTNET_STARTUP_HOOKS, value: /otel/net/OpenTelemetry.AutoInstrumentation.StartupHook.dll}
            - {name: OTEL_DOTNET_AUTO_HOME, value: /otel}
            # ASP.NET Core 的 HTTP 指標（http.server.request.duration）預設沒開，要加
            - {name: OTEL_DOTNET_AUTO_METRICS_ADDITIONAL_SOURCES, value: "Microsoft.AspNetCore.Hosting,Microsoft.AspNetCore.Server.Kestrel"}
            # ASP.NET Core 預設每個請求印好幾行 Information，log 量會暴增；框架的只留 Warning 以上（要看可在 env 蓋掉）
            - {name: Logging__LogLevel__Microsoft.AspNetCore, value: Warning}
            # log 用 JSON、帶 scope（trace_id 在裡面），中央 pipeline 認得；用環境變數設定，舊專案不用改程式。
            # 程式自己用 Serilog、NLog 的話這幾個沒作用，要另外設定（見 docs/metrics/dotnet.md）
            - {name: Logging__Console__FormatterName, value: json}
            - {name: Logging__Console__FormatterOptions__IncludeScopes, value: "true"}
            - {name: Logging__Console__FormatterOptions__UseUtcTimestamp, value: "true"}
            - {name: Logging__Console__FormatterOptions__TimestampFormat, value: "yyyy-MM-ddTHH:mm:ss.fffZ"}
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
          {{- if or $javaAgent $dotnet }}
          volumeMounts:
            - name: otel-agent
              mountPath: /otel
          {{- end }}
      {{- if or $javaAgent $dotnet }}
      volumes:
        - name: otel-agent
          emptyDir: {}
      {{- end }}
{{- end -}}
