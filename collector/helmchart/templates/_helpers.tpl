{{/* 四個設定檔都要用 --set-file 帶進來，少一個就不裝 */}}
{{- define "collector.config" -}}
{{- range $name := list "common" "node" "cluster" "sampler" }}
{{- $_ := required (printf "缺 config.%s：helm 加 --set-file config.%s=collector/code/%s.yaml" $name $name $name) (index $.Values.config $name) }}
{{- end }}
{{- end -}}

{{- define "collector.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{/* 設定或環境變數改了就滾動重啟（key 不用：Secret 掛成檔案，Collector 自己換） */}}
{{- define "collector.checksums" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
{{- end -}}
