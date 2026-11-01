# Grafana：code/ 的 provisioning 檔合成官方 chart 的 values（2026-11）

**狀態**：已完成（沒有拿官方 chart 實裝）　　**日期**：2026-11-01　　**相關**：分支 `claude/grafana-official-values`；[grafana/README.md](../../../grafana/README.md)
**模組**：⑧ 出口

## Plan：這一次要達到什麼

- **需求**：公司用 Grafana 官方 chart（`grafana/grafana`）部署，data source、告警、儀表板都寫在 chart 的 values 裡（`datasources`、`dashboardProviders`…）。`grafana/code/` 是分開的 yaml／json 檔，要合成官方 chart 的 values 格式。
- **目的**：一份 `grafana/values-official.yaml`，`helm upgrade -f` 就能帶進官方 chart；`code/` 仍是唯一的來源，這份由腳本產生、不手改。
- **範圍**：
  - 做：`grafana/code/build-official-values.py`（產生與 `--check`）、`grafana/values-official.yaml`、Helm template 跳脫、用真的 `helm` 驗跳脫。
  - 不做：從網路下載官方 chart 實裝（沒有拿來渲染）；外掛 SHA-256 核對（官方 chart 的 `plugins` 做不到，要核對用 `grafana/helmchart`）；改 `.gitlab-ci.yml`。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `grafana/code/build-official-values.py`（新） | 讀 `code/` 的 `datasource.yaml`、`alert-rules.yaml`、`contact-points.yaml`、`policies.yaml`、`dashboards/*.json`，產生 `datasources.datasources.yaml`、`alerting.{rules,contactpoints,policies}.yaml`、`dashboardProviders`（provider `aiops`、資料夾 AIOps、路徑 `/var/lib/grafana/dashboards/aiops`）、`dashboards.aiops.<名字>.json`；`envFromSecret: grafana-env`（檔裡的 `${ES_URL}`、`${GRAFANA_ES_API_KEY}`、`${NOTIFY_*_URL}` 由 Grafana 從這個 Secret 的環境變數展開）；`--check` 檢查檔案跟 `code/` 一致 | `code/` 單一來源，不兩份手抄；`--check` 讓 CI 能擋「改了 code/ 忘記重新產生」 |
| `grafana/values-official.yaml`（新，產生的） | 上面的結果；`image`（換成 Nexus）、`testFramework: false`；回饋表單外掛用註解的 `plugins` 備著 | 官方 chart 的格式 |
| 告警檔的跳脫 | `{{` → `{{ "{{" }}`、`}}` → `{{ "}}" }}` | 官方 chart 會把 `alerting` 的內容再當 Helm template（`tpl`）處理，`alert-rules.yaml` 裡 Grafana 自己的 `{{ $labels.summary }}` 會被 Helm 吃掉、渲染失敗或變空；YAML flow mapping 結尾的 `}}` 也會一起跳脫，Helm 渲染回來就是原樣 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 產生的結構對、內容沒走樣 | 載回 yaml 比對 | 頂層是 `image`、`testFramework`、`envFromSecret`、`datasources`、`alerting`、`dashboardProviders`、`dashboards`；4 個 data source、3 份告警檔（`groups`、`contactPoints`、`policies`）；3 張儀表板解析後跟原檔一樣（面板數 2、5、5） |
| 跳脫後 Helm 渲染回來跟原檔一樣 | 暫時的小 chart 用 `tpl` 渲染 `alerting` 三個字串，再解析比對（`helm template` 實跑） | 三份都跟 `code/` 的原檔解析結果一致；`{{ $labels` 保留；第一次測試模板格式寫錯（輸出不是合法 YAML）、改成 ConfigMap 格式後通過 |
| `--check` | 產生後立刻 `--check` | 一致 |

**沒測到的**：官方 chart 本身沒下載、沒渲染，`envFromSecret`、`datasources`、`alerting`、`dashboards` 這些欄位名稱是照官方 chart 的文件寫的，沒有跟實際版本對過（chart 版本不同欄位可能有差）；Grafana 實際載入沒驗。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 官方 chart 欄位名稱沒對過實際版本 | 沒有下載 chart | 第一次 `helm template`／部署可能要調 | 部署前先 `helm template grafana/grafana -f grafana/values-official.yaml` 看一次，對不上就改腳本 |
| 儀表板 JSON 整份放進 values，約 74KB | 設計 | `helm` release 變大（仍遠小於上限）、diff 難讀 | 可改成 `dashboardsConfigMaps`（CI 另外 `kubectl create configmap --from-file`），沒做 |
| `aiops-feedback` 儀表板的兩個 `http://localhost:18080` 還是原樣 | 沿用 `code/` | 部署前要換成使用者瀏覽器連得到的回饋服務網址 | 同 [grafana/README.md](../../../grafana/README.md) |
| 回饋表單外掛：官方 chart 的 `plugins` 不核對 SHA-256 | 設計 | 比我們自己的 chart 少一道檢查 | 公司已經有外掛就不用；沒有且要核對就用 `grafana/helmchart` |
