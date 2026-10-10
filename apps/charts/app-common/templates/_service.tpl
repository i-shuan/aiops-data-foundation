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
    - name: metrics
      port: {{ include "app-common.metricsPort" . }}
      targetPort: metrics
{{- end -}}
