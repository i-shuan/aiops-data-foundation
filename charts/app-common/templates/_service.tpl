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
    {{- /* 沒有對外 HTTP 的服務（worker、batch）不要開一個沒人聽的 port */}}
    {{- if .Values.ports.http }}
    - name: http
      port: {{ include "app-common.httpPort" . }}
      targetPort: http
    {{- end }}
    - name: metrics
      port: {{ include "app-common.metricsPort" . }}
      targetPort: metrics
{{- end -}}
