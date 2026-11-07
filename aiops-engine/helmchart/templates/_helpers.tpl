{{- define "aiops.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{/* mode 只能是 single／distributed */}}
{{- define "aiops.mode" -}}
{{- $m := .Values.mode | default "single" -}}
{{- if not (has $m (list "single" "distributed")) }}{{ fail (printf "mode 只能是 single 或 distributed，不是 %q" $m) }}{{ end -}}
{{ $m }}
{{- end -}}

{{/* 排程表依 plugin（code/plugins/<plugin>/__init__.py 的 schedule）；observability 再看 source（第一個＝主要的來源）是 otel 還是 apm；並存寫 otel,apm */}}
{{- define "aiops.schedule" -}}
{{- $plugin := .Values.plugin | default "observability" -}}
{{- $schedules := dict "observability" (printf "configs/%s/schedule.yaml" (ternary "apm" "schedule" (hasPrefix "apm" (.Values.source | default "otel")))) "factory" "configs/factory/schedule.yaml" "metrics_only" "configs/metrics_only/schedule.yaml" -}}
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
  args: ["main", "api"]
  env:
    - {name: API_PORT, value: {{ .Values.api.port | quote }}}
    {{- with include "aiops.pgEnv" . }}
    {{- . | nindent 4 }}
    {{- end }}
    {{- with .Values.api.corsOrigin }}
    - {name: API_CORS_ORIGIN, value: {{ . | quote }}}
    {{- end }}
  {{- include "aiops.envFrom" . | nindent 2 }}
  ports:
    - {name: http, containerPort: {{ .Values.api.port }}}
  {{- $mounts := printf "%s%s" (include "aiops.journalMount" .) (include "aiops.configMounts" .) }}
  livenessProbe:
    httpGet: {path: /health, port: http}
  readinessProbe:
    httpGet: {path: /health, port: http}
  resources:
    {{- toYaml .Values.api.resources | nindent 4 }}
  {{- with $mounts }}
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


{{/* PgBouncer（pgbouncer.enabled）：engine 的容器改連同一個 Pod 裡的 PgBouncer（127.0.0.1），PgBouncer 再連真的 PostgreSQL。
   只能 session 模式：工作鎖（advisory lock）、連線時的 SET statement_timeout／search_path 都綁在一條連線上，transaction 模式會壞
   （docs/pdca/meta/2026-11-pg-resilience.md）。Secret 要多一把 AIOPS_PG_DSN_POOLED（帳密跟 AIOPS_PG_DSN 一樣，host 換成 127.0.0.1:<port>） */}}
{{- define "aiops.pgEnv" -}}
{{- if .Values.pgbouncer.enabled }}
- name: AIOPS_PG_DSN
  valueFrom: {secretKeyRef: {name: {{ .Values.secret }}, key: AIOPS_PG_DSN_POOLED}}
{{- end }}
{{- end -}}

{{- define "aiops.pgbouncer" -}}
{{- if .Values.pgbouncer.enabled }}
- name: pgbouncer
  image: {{ .Values.pgbouncer.image }}
  env:
    - name: DATABASE_URL           # 真的 PostgreSQL（edoburu/pgbouncer 這類 image 讀這個；換別的 image 要對它的環境變數）
      valueFrom: {secretKeyRef: {name: {{ .Values.secret }}, key: AIOPS_PG_DSN}}
    - {name: POOL_MODE, value: session}
    - {name: LISTEN_PORT, value: {{ .Values.pgbouncer.port | quote }}}
    - {name: DEFAULT_POOL_SIZE, value: {{ .Values.pgbouncer.poolSize | quote }}}
    - {name: MAX_CLIENT_CONN, value: {{ .Values.pgbouncer.maxClientConn | quote }}}
    - {name: SERVER_RESET_QUERY, value: "DISCARD ALL"}     # 子行程斷線後連線還給池子前清乾淨：SET、advisory lock 都放掉
  ports:
    - {name: pgbouncer, containerPort: {{ .Values.pgbouncer.port }}}
  resources:
    {{- toYaml .Values.pgbouncer.resources | nindent 4 }}
{{- end }}
{{- end -}}


{{/* 本機日誌的 PVC（journal.enabled）：掛在 /var/lib/aiops/journal。fsGroup 讓 image 的使用者（10001）寫得進去 */}}
{{- define "aiops.journalMount" -}}
{{- if .Values.journal.enabled }}
- {name: journal, mountPath: /var/lib/aiops/journal}
{{- end }}
{{- end -}}

{{/* distributed 的 api、worker 在不同 Pod：PVC 要 ReadWriteMany 才能一起掛；不是的話每個 Pod 用自己的 emptyDir
   （容器重啟還在、Pod 刪掉就沒了；同一個 Pod 裡的工作照樣互相補寫） */}}
{{- define "aiops.journalPvc" -}}
{{- if and .Values.journal.enabled (or (eq (include "aiops.mode" .) "single") (eq .Values.journal.accessMode "ReadWriteMany")) }}true{{ end -}}
{{- end -}}

{{- define "aiops.journalVolume" -}}
{{- if .Values.journal.enabled }}
- name: journal
  {{- if include "aiops.journalPvc" . }}
  persistentVolumeClaim: {claimName: {{ .Values.journal.existingClaim | default "aiops-journal" }}}
  {{- else }}
  emptyDir: {}
  {{- end }}
{{- end }}
{{- end -}}

{{/* single 的換版：日誌的 PVC 只能一台機器掛（ReadWriteOnce）時，新的 Pod 可能排到別台、掛不上 → 只能先停再起 */}}
{{- define "aiops.rollingOk" -}}
{{- if or (not .Values.journal.enabled) (eq .Values.journal.accessMode "ReadWriteMany") }}true{{ end -}}
{{- end -}}
