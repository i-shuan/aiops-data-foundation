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
      labels:
        {{- include "app-common.labels" . | nindent 8 }}
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
            {{- if .Values.ports.http }}
            - name: http
              containerPort: {{ include "app-common.httpPort" . }}
            {{- end }}
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
            # metrics 以 Prometheus 格式開在 metrics port，由平台的 VMAgent 來抓
            - name: OTEL_METRICS_EXPORTER
              value: prometheus
            - name: OTEL_EXPORTER_PROMETHEUS_HOST
              value: 0.0.0.0
            - name: OTEL_EXPORTER_PROMETHEUS_PORT
              value: {{ include "app-common.metricsPort" . | quote }}
            # 還沒有 tracing 後端：trace 不送出，但 trace_id 仍會產生、傳遞並寫進 log
            - name: OTEL_TRACES_EXPORTER
              value: none
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
