# 依部署元件重整資料夾：每個元件 `code/`＋`helmchart/`（2026-10）

**狀態**：已完成　　**日期**：2026-10-31　　**相關**：分支 `claude/restructure-components`；上一次改目錄 [2026-10-restructure.md](2026-10-restructure.md)（platform→telemetry、aiops→engine）
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
- **不動**：`apps/`（本來就是 Helm）、`docs/`（只改連結與地圖）、`tools/`、`e2e/pipeline/`、`e2e/local-k3s/` 的模擬服務與測試腳本（只改部署指令）。PDCA 紀錄裡的舊路徑不回頭改。
- **行為不變**：Collector 設定、ES 結構、Grafana 規則、engine 程式一個字都不改；chart render 出來的資源要跟原本的 manifest 等價。

## Do：改動哪些 code、改了什麼、為何要改

純搬動的部分（`git mv`，內容不變）：`telemetry/storage`、`telemetry/scripts` → `elasticsearch/code/`；`telemetry/collectors/*.yaml`（四個設定檔）、`groups.yaml` → `collector/code/`；`engine/configs/grafana/` → `grafana/code/`；`engine/` 的 Python、`configs/baseline`、`configs/rca`、`Dockerfile`、`requirements.txt`、`.cache` → `aiops-engine/`；`e2e/local-k3s` → `e2e/k8s`。308 個檔案，216 個是改名。

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `collector/helmchart/` | kustomize 改寫成 chart：DaemonSet、Deployment、StatefulSet、RBAC、Service 的 template；`values.yaml` 抽出 `clusterName`、`esEndpoint`、`ntpServer`、image、trace-sampler 的參數與資源；`values-test.yaml` 是原本 `e2e/local-k3s/otel-collector` overlay 的值 | 設定檔不複製進 chart：Helm 讀不到 chart 目錄以外的檔，所以用 `--set-file config.<名>=collector/code/<名>.yaml` 帶，`code/` 仍是唯一來源。Pod 加 `checksum/config` 註記，改設定自動滾動（原本要手動 `rollout restart`）。ClusterRole 名字加 namespace 後綴，兩個 release 不會撞 |
| `elasticsearch/helmchart/` | 原 `e2e/local-k3s/elasticsearch.yaml` 改寫成 chart；`twoNode.enabled` 一個開關涵蓋原本 `elasticsearch-2node.yaml` 的差異（transport 走 127.0.0.1＋socat、憑證 Secret、兩個 headless Service） | 原本兩份 manifest 幾乎一樣、只差兩節點那段，合成一份少一份要同步的；Chart 名叫 `elasticsearch-test`，說明只給測試用。**計畫裡的「Job 跑 setup」沒做**：setup 腳本要讀 `code/storage/` 的四十幾個檔，chart 讀不到 chart 外的檔，照舊從主機跑 `elasticsearch-setup.sh`（e2e 腳本本來就這樣） |
| `grafana/helmchart/` | 原 `engine/deploy/grafana/grafana-example.yaml`＋`e2e/local-k3s/rca-demo/grafana.yaml` 合成一份：initContainer 裝外掛（`plugin.enabled`）、provisioning 的 ConfigMap 由 `--set-file provisioning.{datasources,alerting,dashboards}.<檔名>=…` 帶、provider 由 chart 產生；`values-test.yaml` 是 rca-demo 的值（匿名唯讀、LoadBalancer 3300、不裝外掛） | 兩份 Grafana manifest 一直各改各的。`code/dashboards.yaml`（provider）刪掉，chart 的 provider 固定載進 AIOps 資料夾。`install-plugin.sh` 放 `helmchart/files/`（chart 用 `.Files.Get`）；給公司現有 Grafana 的 `plugin-init-patch.yaml` 留在 `grafana/code/` |
| `aiops-engine/helmchart/` | `gen-cronjobs.py` 那張表搬進 `values.yaml` 的 `jobs`，CronJob 由 template `range` 產生（`backoffLimit` 照原規則：分鐘欄是 `*` 給 0、否則 2；`startingDeadlineSeconds` = min(deadline, 300)）；`stage2: false` 預設只開 4 個；`configOverrides.<資料夾>.<檔名>` 蓋 image 裡的設定檔；`feedback.enabled` 控制回饋網頁；`values-test.yaml` 是原 `engine-schedule` overlay | 原本改頻率要改 Python 再重新產生 yaml、測試再比對兩邊一致；現在只有一處 |
| `aiops-engine/tests/test_deploy.py` | 改成 `helm template` 後解析 YAML 驗同樣的事（Forbid、逾時、重試、stage 1 只有 4 個、train 4Gi、configOverrides 掛對位置）；Grafana 外掛的測試改讀 chart 的 values 與 `code/plugin-init-patch.yaml`；沒有 helm 就跳過 | 原本驗的是 yaml 跟產生器一致，產生器沒了 |
| `aiops-engine/tests/test_grafana_form.py` | 儀表板 json 的路徑改到 `grafana/code/dashboards/` | 檔案搬了 |
| `e2e/k8s/deploy-collector.sh`、`deploy-es.sh`（新） | 兩行包 `helm upgrade --install … -f values-test.yaml --set-file …`；`CONFIG_DIR` 可換設定檔來源、`KCTX` 選 context | 14 支腳本原本各自 `kubectl apply -k otel-collector`，改成呼叫這支；壓測改佇列大小的 `with_config` 改成複製 `collector/code` 到暫存目錄、sed、`CONFIG_DIR` 指過去 |
| `e2e/k8s/run-e2e.sh`、`run-es-2node.sh`、`run-schedule-e2e.sh`、`rca-demo/run-rca-demo.sh` | ES 用 `deploy-es.sh`（兩節點 `--set twoNode.enabled=true`）；engine 用 `helm … aiops-engine/helmchart -f values-test.yaml`；Grafana 用 `helm … grafana/helmchart -f values-test.yaml --set-file …`，demo 的 webhook 另存 `rca-demo/notify-sink.yaml`、demo 的 data source 留 `rca-demo/datasources.yaml` | 部署檔刪了 |
| `e2e/k8s/load/run-durability.sh` | 環境變數 `APPLY_KUSTOMIZE=<目錄>` 改成 `DEPLOY_COLLECTOR=1` | 同上 |
| `e2e/k8s/metrics_trace_check.py`、`run-metrics-scenarios.sh` 等 | 只改路徑字串 | — |
| `collector/README.md`、`elasticsearch/README.md`、`grafana/README.md`、`aiops-engine/README.md` | 各一份：資料夾放什麼、怎麼部署（helm 指令）、怎麼驗；`aiops-engine/README.md` 併入原 `engine/deploy/README.md` 的排程表、心跳、先只寫入不通知、驗證過的；`grafana/README.md` 是原 `engine/configs/grafana/README.md` 搬上來加「部署」一節 | 一個元件一個入口 |
| `CLAUDE.md`、`README.md`、`docs/README.md`、`docs/architecture.md`、`docs/*/README.md`、`sonar-project.properties`、`tools/check-docs.py` | 結構表、文件地圖、路徑；`docs/README.md` 的「部署與測試環境」改列四個元件的 README | 路徑都變了。PDCA 紀錄只改連結的路徑、不改措辭；指到已刪檔案（`e2e/local-k3d/run-es-outage.sh`）的連結改成純文字並註明刪了 |


## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 四個 chart 語法對、render 得出來 | `helm lint`、`helm template`（collector、grafana 用 `--set-file` 帶 `code/` 的檔；aiops-engine 開 `stage2`；elasticsearch 開 `twoNode`） | 4 個 chart lint 0 failed，全部 render 成功 |
| chart render 的資源跟舊 manifest **等價**（行為不變） | 舊的 `kubectl kustomize`（Collector overlay）、舊 `cronjobs*.yaml`、`feedback-web.yaml`、`elasticsearch.yaml` 和新 render 逐資源、逐欄位比對（Python 載 YAML） | Collector 7 種資源、engine 11 個、ES 3 個全部一樣。差異只有：namespace 改由 helm 帶（不在 yaml 裡）、Pod 多 `checksum/config` 註記、image 由 values 給（舊的靠 kustomize 的 `images:`）、ConfigMap 內容的換行（舊 archive 是 CRLF） |
| 單元測試照跑；`test_deploy.py` 改驗 chart 後仍驗到同樣的事 | `cd aiops-engine && python -m pytest -q`（host venv；Docker Desktop 沒開） | 352 passed（原 346 ＋ chart 的新測試）。中途 `test_grafana_form.py` 讀不到儀表板 json，路徑改到 `grafana/code/dashboards/` |
| 文件連結沒斷、索引齊、類型標示齊 | `tools/check-docs.py` | 0 個問題。搬過的 md 相對連結層數變了，用腳本對壞連結試加減一層 `../`，修 52 處；PDCA 紀錄 21 處連結改路徑 |
| **從零用新 chart 裝起、整條線通** | Rancher Desktop 上刪掉 `otel-collector`、`es-test`（含 PVC）、`aiops-demo`、三個測試 namespace 和舊 ClusterRole；`run-e2e.sh`（helm 裝 ES → setup → helm 裝 Collector → 模擬服務）、`run-metrics-e2e.sh`、`run-trace-e2e.sh` | **log 54／54、metrics 46／46、trace 24／24**，跟重整前一樣 |
| Windows 上 helm 吃路徑 | 第一次跑 `run-e2e.sh` | Windows 的 helm 看不懂 Git Bash 的 `/c/...`，`--set-file` 開檔失敗；`deploy-collector.sh`、`deploy-es.sh`、`run-rca-demo.sh`、`run-schedule-e2e.sh` 都先 `cygpath -m` 轉成 `C:/...`（Linux 上 cygpath 不存在就原樣） |

- **沒測到的**：`run-rca-demo.sh`（Grafana chart 真的裝起來、告警能發）、`run-schedule-e2e.sh`（engine chart）、`run-es-2node.sh`（`twoNode`）、`load/run-stress.sh` 的 `with_config`、`tail-sampling` 這幾支改了部署指令的腳本只有 `bash -n` 和 `helm template`，沒在 cluster 上跑；Grafana chart 的 `plugin.enabled=true`（initContainer 裝外掛）沒有 Nexus 可以實跑。


## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 計畫裡「ES chart 帶一個 Job 跑 setup」沒做 | 寫 chart 時：setup 要讀 `code/storage/` 四十幾個檔，Helm 讀不到 chart 目錄外的檔；要做得把 storage 複製進 chart 或打成 image | 測試 ES 裝好後還是要從主機跑 `elasticsearch-setup.sh`（e2e 腳本本來就這樣，正式 ES 也是這樣） | 不做；README 寫清楚兩步 |
| 設定檔靠 `--set-file` 帶，`helm install` 指令很長 | 設計時 | 每個元件的 README 和 e2e 的 deploy 腳本都寫好完整指令；正式環境如果用 CD 工具（Argo CD 等）要把 `code/` 的檔塞進 values | 有 CD 工具時再看要不要改成 chart 內 `files/`＋同步腳本 |
| 五支改了部署指令的 e2e 腳本沒在 cluster 上跑過（見 Check「沒測到的」） | 時間；Docker Desktop 也沒開（image build 用的 nerdctl 走 Rancher Desktop，`run-schedule-e2e.sh` 要先 build） | 它們的 helm 指令可能還有路徑或參數的小錯 | 下一次跑各自的 e2e 時順手驗；`run-rca-demo.sh` 最值得先跑（Grafana chart 第一次真的裝） |
| `e2e/local-k3d/` 的兩支耐久測試（`run-es-outage.sh`、`run-durability.sh`）刪了，Grafana 13 通知測試（`run-grafana-notify.sh`）也刪了 | 計畫就決定刪 | 耐久測試 `e2e/k8s/load/run-durability.sh` 有涵蓋；Grafana 通知測試現在只剩 `e2e/pipeline/run-alert-flap.py`（要一台 Grafana）和 `rca-demo`。結果都在 [collector-es-exporter](2026-10-collector-es-exporter.md)、[pin-es8-grafana13](../../meta/2026-10-pin-es8-grafana13.md) | 要再測 Grafana 通知就用 `rca-demo`（chart 裝的 Grafana＋notify-sink） |

- **決定**：部署只留 Helm 一種；`code/` 是設定的唯一來源，chart 不複製它（`--set-file`）。
- **下一輪**：排程改成一隻 pod（排程器＋回饋網頁兩個 container），在新結構的 `aiops-engine/` 上做。

