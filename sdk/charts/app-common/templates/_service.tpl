{{- define "app-common.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "app-common.name" . }}
  labels:
    {{- include "app-common.labels" . | nindent 4 }}
spec:
  selector:
    {{- include "app-common.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ include "app-common.httpPort" . }}
      targetPort: http
    {{- /* .NET 的 metrics 用 OTLP 推給 Collector，沒有 metrics port */}}
    {{- if not (include "app-common.dotnetEnabled" .) }}
    - name: metrics
      port: {{ include "app-common.metricsPort" . }}
      targetPort: metrics
    {{- end }}
{{- end -}}
