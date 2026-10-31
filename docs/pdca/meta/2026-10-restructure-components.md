# 依部署元件重整資料夾：每個元件 `code/`＋`helmchart/`（2026-10）

**狀態**：進行中　　**日期**：2026-10-31　　**相關**：分支 `claude/restructure-components`；上一次改目錄 [2026-10-restructure.md](2026-10-restructure.md)（platform→telemetry、aiops→engine）
**模組**：①～⑨（只動目錄與部署方式，不動行為）

## Plan：這一次要達到什麼

- **需求**：現在的頂層是依「做什麼」分（`telemetry/` 收與存、`engine/` 判斷到回饋），但實際部署是四個獨立的東西：邊緣的 Collector（每個應用 cluster 一套）、中央 ES、中央 Grafana、AIOps engine。設定檔與部署檔散在三處（`telemetry/collectors` 同時放 Collector 設定與 kustomize；ES 的結構設定在 `telemetry/storage`、測試用 ES 的 manifest 在 `e2e/`；Grafana 的設定在 `engine/configs/grafana`、部署在 `engine/deploy/grafana`、demo 用的在 `e2e/rca-demo`），部署方式也不一（kustomize、純 yaml、腳本建 ConfigMap）。要找「這個元件怎麼部署、吃哪些設定」得翻好幾個地方。
- **目的**：一個部署元件一個頂層資料夾，裡面固定三樣：`README.md`（這個元件是什麼、怎麼部署）、`code/`（程式與設定檔）、`helmchart/`（唯一的部署方式，Helm chart；測試環境用 `values-test.yaml`）。舊的部署檔（kustomize、純 yaml、`e2e/local-k3d/`）全部刪掉，不留第二種部署方式。
- **範圍**：

| 新資料夾 | `code/` 放什麼（搬自） | `helmchart/` 部署什麼（搬自／改寫自） |
|---|---|---|
| `elasticsearch/` | `telemetry/storage/`（mapping、pipeline、ILM、template）、`telemetry/scripts/`（`elasticsearch-setup.sh`、`retention.conf`、`rotate-writer-key.sh`） | **只部署測試用的單節點 ES**（`e2e/local-k3s/elasticsearch.yaml`）＋一個 Job 跑 setup。正式 ES 是公司的，只套 `code/` 的設定 |
| `collector/` | `telemetry/collectors/` 的 `common.yaml`、`node.yaml`、`cluster.yaml`、`sampler.yaml`、`telemetry/groups.yaml` | DaemonSet、Deployment、StatefulSet、RBAC、Service（`telemetry/collectors/` 的 kustomize 改寫）；`values.yaml` 抽出 cluster 名、ES 位址、NTP、image、取樣參數 |
| `grafana/` | `engine/configs/grafana/`（alert-rules、contact-points、policies、datasource、dashboards、外掛安裝說明） | Grafana Deployment＋initContainer 裝外掛＋provisioning（`engine/deploy/grafana/`＋`e2e/local-k3s/rca-demo/grafana.yaml` 合成一份）；公司現有 Grafana 用 `plugin-init-patch` 的做法寫進 README |
| `aiops-engine/` | `engine/` 的 Python（`core`、`detector`、`investigator`、`feedback_web`、`interfaces`、`fake-data`、`tests`）、`configs/baseline`、`configs/rca`、`Dockerfile`、`requirements.txt` | `engine/deploy/`（CronJob、回饋網頁；這次先照搬成 chart，排程改成一隻 pod 是下一輪，另開 PDCA） |

- **刪掉**：`telemetry/`、`engine/` 兩個頂層；`engine/deploy/gen-cronjobs.py`（chart 的 template 取代）；`e2e/local-k3s/` 裡的部署檔（`elasticsearch*.yaml`、`otel-collector/`、`engine-schedule/`、`rca-demo/grafana.yaml`、provisioning yaml）；`e2e/local-k3d/` 整個（Docker Desktop 時期的環境，耐久測試跟 `e2e/local-k3s/load/` 重複，結果都在 PDCA）。
- **不動**：`sdk/`（本來就是 Helm）、`docs/`（只改連結與地圖）、`tools/`、`e2e/pipeline/`、`e2e/local-k3s/` 的模擬服務與測試腳本（只改部署指令）。PDCA 紀錄裡的舊路徑不回頭改。
- **行為不變**：Collector 設定、ES 結構、Grafana 規則、engine 程式一個字都不改；chart render 出來的資源要跟原本的 manifest 等價。

## Do：改動哪些 code、改了什麼、為何要改

（實作後補）

## Check：測試的目的與結果

（實作後補）

## Act：Check 後沒改善的、還沒解決的

（實作後補）
