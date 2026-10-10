{{/* 服務名稱：AIOps 的分析單位，上線後不要再改 */}}
{{- define "app-common.name" -}}
{{- required "app.name 必填" .Values.app.name -}}
{{- end -}}

{{/* 版本：直接取 image tag，每次發版都會變，AIOps 靠它判斷「什麼時候換了版本」 */}}
{{- define "app-common.version" -}}
{{- .Values.image.tag | toString | trunc 63 -}}
{{- end -}}

{{- define "app-common.selectorLabels" -}}
app.kubernetes.io/name: {{ include "app-common.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "app-common.labels" -}}
{{ include "app-common.selectorLabels" . }}
app.kubernetes.io/part-of: {{ .Values.app.partOf }}
app.kubernetes.io/component: {{ .Values.app.component }}
app.kubernetes.io/version: {{ include "app-common.version" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "app-common.httpPort" -}}
{{- (.Values.ports | default dict).http | default 8080 -}}
{{- end -}}

{{- define "app-common.metricsPort" -}}
{{- (.Values.ports | default dict).metrics | default 9464 -}}
{{- end -}}

{{- define "app-common.traceSampleRatio" -}}
{{- $otel := .Values.otel | default dict -}}
{{- $traces := $otel.traces | default dict -}}
{{- if hasKey $traces "sampleRatio" }}{{ $traces.sampleRatio }}{{ else }}1.0{{ end -}}
{{- end -}}

{{- define "app-common.javaAgentEnabled" -}}
{{- $otel := .Values.otel | default dict -}}
{{- $java := $otel.javaAgent | default dict -}}
{{- if $java.enabled }}true{{ end -}}
{{- end -}}

{{/*
源頭資料規範。不符合就直接部署失敗，CI 只要跑 helm template 就能擋下。
*/}}
{{- define "app-common.validate" -}}
{{- $v := .Values -}}
{{- $app := $v.app | default dict -}}
{{- $_ := required "app.name 必填（服務名稱，例如 mes-api）" $app.name -}}
{{- $_ := required "app.partOf 必填（所屬系統，例如 mes）" $app.partOf -}}
{{- $components := list "api" "web" "worker" "batch" "gateway" -}}
{{- if not (has $app.component $components) -}}
{{- fail (printf "app.component 只能是 %s 其中一個" (join "、" $components)) -}}
{{- end -}}
{{- $image := $v.image | default dict -}}
{{- $_ := required "image.repository 必填" $image.repository -}}
{{- $tag := required "image.tag 必填（版本號）" $image.tag | toString -}}
{{- if eq $tag "latest" -}}
{{- fail "image.tag 不能用 latest，要用版本號，版本才對得回 app.kubernetes.io/version" -}}
{{- end -}}
{{- $res := $v.resources | default dict -}}
{{- $limits := $res.limits | default dict -}}
{{- if not (and $res.requests $limits.memory) -}}
{{- fail "resources.requests 與 resources.limits.memory 必填，沒有 limits 就算不出記憶體使用率" -}}
{{- end -}}
{{- $probes := $v.probes | default dict -}}
{{- if not (and $probes.readiness $probes.liveness) -}}
{{- fail "probes.readiness 與 probes.liveness 必填，探測失敗的事件是 AIOps 的訊號來源" -}}
{{- end -}}
{{- with $v.logFormat -}}
{{- if not (regexMatch "^[a-z0-9-]{1,32}$" (toString .)) -}}
{{- fail "logFormat 只能用小寫英數與減號（例如 logfmt、secs），對應中央 ES 的 k8s-logs-format-<logFormat>" -}}
{{- end -}}
{{- end -}}
{{- if include "app-common.javaAgentEnabled" . -}}
{{- $_ := required "otel.javaAgent.enabled 為 true 時，otel.javaAgent.image 必填" $v.otel.javaAgent.image -}}
{{- end -}}
{{- $ratio := include "app-common.traceSampleRatio" . | float64 -}}
{{- if or (lt $ratio 0.0) (gt $ratio 1.0) -}}
{{- fail "otel.traces.sampleRatio 要在 0 到 1 之間（例如 0.1 表示記一成的請求）" -}}
{{- end -}}
{{- end -}}
