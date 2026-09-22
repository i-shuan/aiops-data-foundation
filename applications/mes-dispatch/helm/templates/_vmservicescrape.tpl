{{- define "app.vmservicescrape" -}}
apiVersion: operator.victoriametrics.com/v1beta1
kind: VMServiceScrape
metadata:
  name: {{ include "app.name" . }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  selector:
    matchLabels:
      {{- include "app.selectorLabels" . | nindent 6 }}
  endpoints:
    - port: metrics
      path: /metrics
      # 把 pod 上的標準 label 直接蓋到每一筆 metrics 上，欄位名稱跟 log 一致：
      # app / version / part_of / component
      relabelConfigs:
        - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
          target_label: app
        - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_version]
          target_label: version
        - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_part_of]
          target_label: part_of
        - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_component]
          target_label: component
{{- end -}}
