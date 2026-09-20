{{- define "ns-labeler.name" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "ns-labeler.labels" -}}
app.kubernetes.io/name: {{ include "ns-labeler.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: observability
{{- end -}}
