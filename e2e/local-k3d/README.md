# 本機 k3d（K8s 實測）

給沒有現成 K8s 的環境用：單節點 k3d，把 `telemetry/collectors`、`sdk/charts/app-common`、Grafana 的 initContainer 放上去跑一次。驗證紀錄：[docs/pdca/phase1-collect/2026-10-k3d-e2e.md](../../docs/pdca/phase1-collect/2026-10-k3d-e2e.md)。

| 腳本 | 做什麼 |
|---|---|
| `create-cluster.sh` | 建 cluster。這個環境（Firecracker VM、沒有 `CAP_SYS_RESOURCE`、proxy 換憑證）要兩個特別處理：containerd 設 `restrict_oom_score_adj`，不然所有 Pod 的 runc 都失敗；節點拉不到 image，所以先在主機拉、用 `load-images.sh` 匯進去 |
| `load-images.sh <image>…` | 主機 docker 的 image 匯進節點。要單一平台（amd64）匯出：docker 29 的 containerd image store 預設匯出多平台索引，缺 blob，`k3d image import` 會失敗 |
| `deploy-collectors.sh` | 部署平台 Collector（node、cluster、trace-sampler）；ES 用主機 docker 的測試 ES |
| `otel-collector/` | kustomize overlay：沿用正式設定，只改連線目標與 trace-sampler 的取樣 |

一般有正常網路和權限的機器不需要這些處理，直接 `k3d cluster create` 就好。

應用的部署：`helm template sdk/examples/mes-api -n <ns> --set otel.logs.exporter=otlp --set image.repository=… | kubectl apply -f -`；namespace 要有 `dept` 標籤（例如 `kubectl label ns mes-prod dept=D3120`）。Grafana：`engine/deploy/grafana/grafana-example.yaml`（把 `PLUGIN_URL` 指到 Nexus 或這台主機的 HTTP 服務）。
