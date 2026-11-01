# ES 結構設定改在 K8s 裡跑：es-setup chart（Job＋開關）（2026-11）

**狀態**：已完成（GitLab 上沒跑過）　　**日期**：2026-11-01　　**相關**：分支 `claude/es-setup-helm-job`；前案 [gitlab-ci](2026-11-gitlab-ci.md)
**模組**：③ 存、① 收值（寫入 key）

## Plan：這一次要達到什麼

- **需求**：`elasticsearch-setup.sh` 原本由 GitLab Runner 直接連 ES 跑；公司內網要讓 Runner 連到 ES 很麻煩，ES 只有 K8s 裡面連得到。要改成在 K8s 裡用 Job 跑，用 values 開關控制，日常部署不跑。
- **前提**（使用者確認）：ES 和 AIOps（aiops-engine、Grafana）在同一個 cluster；Collector 每個應用 cluster 各一套。
- **目的**：
  - 新 chart `elasticsearch/setup-helmchart/`（`es-setup`）：`esSetup.enabled` 預設 `false`；`true` 時建 Job（hook post-install、post-upgrade）跑 setup，發的 key 不印出來，直接寫進同一個 cluster 的 Secret（`aiops` 的 `aiops-secrets`、`grafana` 的 `grafana-env` 的 `GRAFANA_ES_API_KEY`），5 把 key 另外存在 `es-setup-keys`。
  - 腳本與設定檔（`elasticsearch/code/`，約 24KB）在 CI 打包成一個檔用 `--set-file` 帶進 ConfigMap，Job 解開執行；chart 讀不到 chart 外面的檔。
  - CI：`es-setup` job 改成建 ES 管理員帳號的 Secret、`helm upgrade --set esSetup.enabled=true`；Collector job 從 AIOps cluster 的 `es-setup-keys` 讀寫入 key，寫進應用 cluster 的 `es-writer-keys`；其他部署 job 不再從 artifact 拿 key。
  - 修 setup 腳本在 Windows 上跑會失敗（`jq` 輸出帶 `\r`，拼進網址後 `curl` 報 URL rejected）。
- **範圍**：
  - 做：chart（Job、ConfigMap、RBAC、開關、`skipKeys`）、腳本 `\r` 修正、CI 四份 yaml 調整、測試 cluster 實跑 Job、文件。
  - 不做：Job 跨 cluster 寫 Secret；健康檢查 key 自動放進 LB。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `elasticsearch/setup-helmchart/`（新，chart `es-setup`） | `esSetup.enabled`（預設 `false`）才建：ConfigMap `es-setup-bundle`（`--set-file bundle` 帶進來的 `elasticsearch/code/` tar.gz）、`es-setup-run`（`files/run.sh`）、ServiceAccount 與 Role（自己 namespace＋`targets` 的 namespace，只有 Secret 的 get、create、patch）、Job（hook post-install、post-upgrade，`backoffLimit: 0`）；`esSetup.skipKeys` 只更新結構；`adminSecret`（`ES_AUTH`）chart 不建 | 日常部署不跑；失敗不自動重試（重試就會再發 key）；管理員密碼不進 values |
| `setup-helmchart/files/run.sh`（新） | 解開 bundle、把 `.sh`、`.conf` 的 CR 去掉、跑 setup（`KEYS_OUT_DIR`）；5 把 key 寫進 `es-setup-keys`，`targets` 各寫一個欄位；都用 server-side apply（field manager `es-setup`） | 只管 key 那個欄位，`grafana-env` 其他欄位不被蓋掉；不存 annotation |
| `elasticsearch/code/scripts/elasticsearch-setup.sh` | `jq()` 把輸出的 CR 去掉（`tr -d '\015'`） | Windows 的 jq 每行多一個 CR，資料流名稱帶著它拼進網址，`curl` 報 URL rejected；前案記成「測試 ES 本來就失敗」，其實是這個 |
| 四份 `.gitlab-ci.yml` | `es-setup`：建 `es-setup-admin`、打包、`helm upgrade --set esSetup.enabled=true`（`ES_SETUP_SKIP_KEYS`）、印 Job log，不再有 artifact；`deploy-collector`：用 `KUBE_CONFIG_AIOPS` 讀 `es-setup-keys` 的寫入 key，寫進應用 cluster 的 `es-writer-keys`；`deploy-aiops-engine`、`deploy-grafana`：不碰 key（Grafana 只寫自己的欄位） | Runner 不用連 ES；key 不經過 CI |
| `elasticsearch/README.md` | 「用 GitLab CI 跑」改寫：什麼時候按哪個、發生什麼 | 操作文件 |

前案的 `KEYS_OUT_DIR`、三個 chart 從 values 建 Secret（`esWriterApiKey`、`esApiKey`、`secretData`）都留著（手動用），CI 不再用。

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| chart 渲染、開關 | `helm template`：預設、開但沒 bundle、開＋bundle | 預設 0 個資源；沒 bundle 擋下並說明；開＋bundle 建 SA、2 個 ConfigMap、3 組 Role／RoleBinding（自己、`aiops`、`grafana`）、Job，`TARGETS` 兩行；bundle 約 32KB |
| Job 真的在 cluster 裡跑通 | 測試 cluster 建 `es-setup`、`aiops`、`grafana` namespace，`grafana-env` 先用 field manager `ci` 寫 `ES_URL`、`NOTIFY_IT_URL`，`helm upgrade --install` 開 Job（image `alpine/k8s:1.30.4`、ES `elasticsearch.es-test.svc`） | 第一次失敗：在 Windows 打包的檔是 CRLF，`retention.conf` 報 `$'\r'`；run.sh 加去 CR 後成功。log 68 行、沒有 key、沒有錯誤 |
| key 寫對、權限對、不蓋掉別人的欄位 | 看三個 Secret、拿 key 打 ES | `es-setup-keys` 5 個欄位；`aiops-secrets` 有 `ES_API_KEY`；`grafana-env` 有 `ES_URL`（還是 CI 寫的值）、`NOTIFY_IT_URL`、`GRAFANA_ES_API_KEY`，沒有 annotation；grafana key `GET /` 200、engine key 讀 `aiops-alerts` 200、writer key `GET /` 403（只能寫） |
| 日常部署不跑 Job、不刪 Secret | `helm upgrade` 不帶 `enabled` | 成功；舊 Job 還在（Complete，hook 資源 helm 不管）、沒有新的；Secret 都在 |
| `skipKeys` 不發 key | `enabled=true skipKeys=true` | log「只更新結構」；`es-setup-keys` 的 writer 沒變；ES 上 `es-writer` key 數 25 → 25（這台測試 ES 歷次跑累積了 25 把） |
| Collector job 的 key 複製 | 測試 cluster 另開 namespace，照 job 的指令讀 `es-setup-keys` 寫 `es-writer-keys` | 複製過去跟原本一樣；用它寫入 ES 回 201 |
| `jq` 去 CR | 單獨跑函式 | 輸出沒有 CR；修之前對測試 ES `SKIP_KEYS=1` 失敗、修之後成功 |
| CI yaml | `yaml.safe_load`；檢查沒有殘留的 artifact／key 檔引用 | 都能解析、沒有殘留；文件檢查過、單元測試 532 過 |

**沒測到的**：GitLab 上實跑（`dtzar/helm-kubectl` 的 `base64`、`tar` 行為、Runner 用兩個 kubeconfig）；公司 Nexus 上的工具 image；`snapshot.settings` 用 `--set-file` 帶 JSON 沒實跑。測試用的 namespace（`es-setup`、`aiops`、`grafana`）與 release `es-setup` 還留在測試 cluster（刪除被權限擋下，等使用者決定）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 每次發 key 都是新的、舊的不作廢 | ES 的行為；測試 ES 累積到 25 把 | key 越積越多 | 只改結構用 `skipKeys`；換 key 後到 ES 手動作廢舊的 |
| 工具 image 要公司提供 | 測試用公開的 `alpine/k8s:1.30.4` | 內網拉不到公開 image | 放到 Nexus，`ES_SETUP_IMAGE` 指過去；要有 bash、curl、jq、kubectl、tar |
| `targets` 的 namespace 要先存在 | 設計（Role 建在那個 namespace） | 第一次部署要先建 `aiops`、`grafana` namespace，或先跑那兩個部署 job | 不處理；文件已寫 |
| hook Job 不會被 helm 刪 | 實測 | 舊 Job（Complete）留在 namespace 裡 | 下一次跑之前自動刪（`before-hook-creation`），不處理 |
