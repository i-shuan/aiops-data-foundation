{{- define "app.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "app.name" . }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  selector:
    {{- include "app.selectorLabels" . | nindent 4 }}
  ports:
    {{- /* 沒有對外 HTTP 的服務（worker、batch）不要開一個沒人聽的 port */}}
    {{- if .Values.ports.http }}
    - name: http
      port: {{ include "app.httpPort" . }}
      targetPort: http
    {{- end }}
    - name: metrics
      port: {{ include "app.metricsPort" . }}
      targetPort: metrics
{{- end -}}
