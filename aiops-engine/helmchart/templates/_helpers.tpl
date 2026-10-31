{{- define "aiops.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{/* 蓋設定檔用的 volumeMounts／volumes（configOverrides 有東西才有） */}}
{{- define "aiops.configMounts" -}}
{{- range $dir, $files := .Values.configOverrides }}
{{- range $name, $_ := $files }}
- {name: configs, mountPath: /app/configs/{{ $dir }}/{{ $name }}.yaml, subPath: {{ $dir }}__{{ $name }}.yaml, readOnly: true}
{{- end }}
{{- end }}
{{- end -}}

{{- define "aiops.configVolumes" -}}
{{- if .Values.configOverrides }}
- name: configs
  configMap: {name: aiops-configs}
{{- end }}
{{- end -}}
