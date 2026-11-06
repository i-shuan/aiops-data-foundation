{{- define "aiops.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{/* mode 只能是 single／distributed */}}
{{- define "aiops.mode" -}}
{{- $m := .Values.mode | default "single" -}}
{{- if not (has $m (list "single" "distributed")) }}{{ fail (printf "mode 只能是 single 或 distributed，不是 %q" $m) }}{{ end -}}
{{ $m }}
{{- end -}}

{{/* 排程表依 plugin（code/plugins/<plugin>/__init__.py 的 schedule）；observability 再看 source 是 otel 還是 apm */}}
{{- define "aiops.schedule" -}}
{{- $plugin := .Values.plugin | default "observability" -}}
{{- $schedules := dict "observability" (printf "configs/%s/schedule.yaml" (ternary "apm" "schedule" (eq (.Values.source | default "otel") "apm"))) "factory" "configs/factory/schedule.yaml" "metrics_only" "configs/metrics_only/schedule.yaml" -}}
{{- $schedule := index $schedules $plugin -}}
{{- if not $schedule }}{{ fail (printf "plugin 只能是 observability、factory、metrics_only，不是 %q" $plugin) }}{{ end -}}
{{ $schedule }}
{{- end -}}

{{/* 佇列在哪（distributed）：redis.url，沒填就是 chart 裝的那個 */}}
{{- define "aiops.brokerUrl" -}}
{{- if .Values.redis.url }}{{ .Values.redis.url }}{{ else }}redis://aiops-redis:6379/0{{ end -}}
{{- end -}}

{{/* 每個 container 都一樣的 envFrom：ES、LLM、API token */}}
{{- define "aiops.envFrom" -}}
envFrom:
  - configMapRef: {name: aiops-env}
  - secretRef: {name: {{ .Values.secret }}}
  - secretRef: {name: aiops-llm}
  - secretRef: {name: aiops-api}
{{- end -}}

{{/* api container（single 跟 distributed 共用） */}}
{{- define "aiops.apiContainer" -}}
- name: api
  image: {{ include "aiops.image" . }}
  args: ["aiops", "api"]
  env:
    - {name: API_PORT, value: {{ .Values.api.port | quote }}}
    {{- with .Values.api.corsOrigin }}
    - {name: API_CORS_ORIGIN, value: {{ . | quote }}}
    {{- end }}
  {{- include "aiops.envFrom" . | nindent 2 }}
  ports:
    - {name: http, containerPort: {{ .Values.api.port }}}
  livenessProbe:
    httpGet: {path: /health, port: http}
  readinessProbe:
    httpGet: {path: /health, port: http}
  resources:
    {{- toYaml .Values.api.resources | nindent 4 }}
  {{- with include "aiops.configMounts" . }}
  volumeMounts:
    {{- . | nindent 4 }}
  {{- end }}
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
