{{/* 服務名稱：AIOps 的分析單位，上線後不要再改 */}}
{{- define "app.name" -}}
{{- required "app.name 必填" .Values.app.name -}}
{{- end -}}

{{/* 版本：直接取 image tag，每次發版都會變，AIOps 靠它判斷「什麼時候換了版本」 */}}
{{- define "app.version" -}}
{{- .Values.image.tag | toString | trunc 63 -}}
{{- end -}}

{{- define "app.selectorLabels" -}}
app.kubernetes.io/name: {{ include "app.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "app.labels" -}}
{{ include "app.selectorLabels" . }}
app.kubernetes.io/part-of: {{ .Values.app.partOf }}
app.kubernetes.io/component: {{ .Values.app.component }}
app.kubernetes.io/version: {{ include "app.version" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "app.httpPort" -}}
{{- (.Values.ports | default dict).http | default 8080 -}}
{{- end -}}

{{- define "app.metricsPort" -}}
{{- (.Values.ports | default dict).metrics | default 9464 -}}
{{- end -}}

{{- define "app.javaAgentEnabled" -}}
{{- $otel := .Values.otel | default dict -}}
{{- $java := $otel.javaAgent | default dict -}}
{{- if $java.enabled }}true{{ end -}}
{{- end -}}

{{/*
源頭資料規範。不符合就直接部署失敗，CI 只要跑 helm template 就能擋下。
*/}}
{{- define "app.validate" -}}
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
{{- if include "app.javaAgentEnabled" . -}}
{{- $_ := required "otel.javaAgent.enabled 為 true 時，otel.javaAgent.image 必填" $v.otel.javaAgent.image -}}
{{- end -}}
{{- end -}}
