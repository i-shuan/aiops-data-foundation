# Grafana：code/ 的 provisioning 檔合成社群 chart 的 values（2026-11）

**狀態**：已完成（測試 cluster 實裝驗過）　　**日期**：2026-11-01　　**相關**：分支 `claude/grafana-official-values`（第一版）、`claude/grafana-community-chart`（改用社群 chart、實裝後的修正）；[grafana/README.md](../../../grafana/README.md)
**模組**：⑧ 出口

## Plan：這一次要達到什麼

- **需求**：公司用 Grafana 的 helm chart 部署，data source、告警、儀表板都寫在 chart 的 values 裡（`datasources`、`alerting`、`dashboardProviders`、`dashboards`）。`grafana/code/` 是分開的 yaml／json 檔，要合成那個 values 格式。
- **目的**：一份 `grafana/values-official.yaml`，`helm upgrade -f` 就能帶進 chart；`code/` 仍是唯一的來源，這份由腳本產生、不手改。
- **範圍**：做：`grafana/code/build-official-values.py`（產生與 `--check`）、`grafana/values-official.yaml`、CI 的 Grafana job、在測試 cluster 實裝驗證。不做：外掛 SHA-256 核對（chart 的 `plugins` 做不到，要核對用 `grafana/helmchart`）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `grafana/code/build-official-values.py`（新） | 讀 `code/` 的 `datasource.yaml`、`alert-rules.yaml`、`contact-points.yaml`、`policies.yaml`、`dashboards/*.json`，產生 `datasources`、`alerting.{rules,contactpoints,policies}.yaml`、`dashboardProviders`（provider `aiops`、資料夾 AIOps）、`dashboards.aiops.<名字>.json`、`envFromSecret: grafana-env`、`admin.existingSecret: grafana-env`；`--check` 檢查檔案跟 `code/` 一致 | `code/` 單一來源；`--check` 讓 CI 擋「改了 code/ 忘記重新產生」 |
| 用 **`grafana-community/grafana`**（原 `grafana/grafana`） | `grafana/grafana` 已標 deprecated（最後 10.5.15、Grafana 12.3），搬到 `grafana-community/helm-charts`（14.x、Grafana 13.x，跟我們用的 13.0.7 同一代）；格式相同 | 實裝時 `helm template` 就出現棄用警告 |
| `alerting` 的值是 map、`{{ $labels.x }}` 用反引號跳脫 | chart 把 `datasources`、`alerting` 先 `toYaml` 再 `tpl` 渲染：`alerting` 要給 map（第一版給字串，渲染直接報 `wrong type for value; expected map`）；字串裡每一段 `{{ ... }}` 換成 `{{ \`{{ ... }}\` }}`（Go template 的原始字串，裡面沒有引號，`toYaml` 之後不會壞） | 不讓 Helm 吃掉 Grafana 自己的範本變數 |
| `admin.existingSecret` 指到 `grafana-env`（`GF_SECURITY_ADMIN_USER`、`GF_SECURITY_ADMIN_PASSWORD`） | chart 預設自己產生隨機 admin 密碼，並用明確的 `env` 蓋掉 `grafana-env` 裡的同名變數 | `grafana-env` 的密碼原本不會生效（實測登入失敗） |
| `grafana/.gitlab-ci.yml` | chart 改 `grafana-community/grafana`、`GRAFANA_HELM_REPO` 預設 `https://grafana-community.github.io/helm-charts`；Secret 多 `GF_SECURITY_ADMIN_USER=admin`；部署後 `kubectl rollout restart`（env 不會熱更新）；`check-grafana-values` | 跟著 chart 與實測的發現 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 產生的 values 在實際 chart 上渲染得出來 | `helm template grafana-community/grafana --version 14.3.0 -f values-official.yaml`；先用舊的 `grafana/grafana` 10.5.15 試 | 舊 chart 報棄用警告＋型別錯誤（修正後 map 格式可用）；社群 chart 渲染成功：ConfigMap `grafana`（`datasources.yaml`、`rules.yaml`、`contactpoints.yaml`、`policies.yaml`）、`grafana-dashboards-aiops`（3 張儀表板 JSON）；渲染後 `rules.yaml`、`contactpoints.yaml` 跟 `code/` 原檔解析結果一致 |
| 真的裝進 cluster、Grafana 載入 | 測試 cluster namespace `grafana`：`grafana-env` 先照 CI 建好，`helm upgrade --install`（image `docker.io/grafana/grafana:13.0.7`） | Pod Running、`/api/health` 是 13.0.7、provisioning 沒有錯誤；4 個 data source、3 條告警規則（`AIOps 事件`、`AIOps 停了`、`AIOps 基準線不見`）、3 個通知管道（webhook 指到 notify-sink）、分送規則（預設 `platform`、`group_by: [alert_id]`、依 `dept` 兩條子路由）、3 張儀表板（AIOps 資料夾）都在；`annotations.summary` 是 `{{ $labels.summary }}`，沒被 Helm 吃掉 |
| data source 連線 | `/api/datasources/uid/<uid>/health` | `aiops-alerts` 健康；另外三個帶 `Warning: Error validating index … unauthorized`——**Grafana 的 key 只能讀 `aiops-alerts`**（`elasticsearch-setup.sh` 的限制，原本就有的缺口），修了 setup 後四個都乾淨（見 [es-setup-helm-job](../archive/phase1-collect/2026-11-es-setup-helm-job.md)） |
| admin 密碼 | 用 `grafana-env` 的密碼登入 | 第一次用 `grafana-env` 的密碼失敗、也試過 chart 產生的（連續錯誤觸發暴力破解鎖定，log 寫 `too many consecutive incorrect login attempts`，重啟才解）；加 `admin.existingSecret` 後 `grafana-env` 的密碼登入成功 |
| 換 key 後 Grafana 要不要重啟 | es-setup 發新 key 寫進 `grafana-env`，不重啟看 data source | 不重啟還是舊 key（警告還在）、重啟後四個 data source 乾淨 → CI job 加 `rollout restart` |
| `--check` | 產生後立刻 `--check` | 一致 |

**沒測到的**：`policies.yaml` 渲染後我用 Python 檢查會失敗（沒加引號的 `=` 被 PyYAML 當特殊標籤），Grafana 實際載入沒有問題（上面的分送規則都對）；回饋表單外掛沒裝（儀表板 `aiops-feedback` 的表單面板會顯示找不到外掛）；公司的 Nexus 上有沒有這個 chart 的 helm proxy；`kubectl apply --server-side` 套 Secret 時如果只列部分欄位，同一個 field manager 先前擁有、這次沒列的欄位會被移除（測試時踩到：只套 `GF_SECURITY_ADMIN_USER` 把其他欄位刪光、Pod 起不來），CI 的 Grafana job 每次都套完整欄位組所以不會發生，但手動改單一欄位要小心。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 儀表板 JSON 整份放進 values，約 74KB | 設計 | `helm` release 變大（仍遠小於上限）、diff 難讀 | 可改成 `dashboardsConfigMaps`（CI 另外 `kubectl create configmap --from-file`），沒做 |
| `aiops-feedback` 儀表板的兩個 `http://localhost:18080` 還是原樣 | 沿用 `code/` | 部署前要換成使用者瀏覽器連得到的回饋服務網址 | 同 [grafana/README.md](../../../grafana/README.md) |
| 回饋表單外掛：chart 的 `plugins` 不核對 SHA-256 | 設計 | 比我們自己的 chart 少一道檢查 | 公司已經有外掛就不用；沒有且要核對就用 `grafana/helmchart` |
| 社群 chart 的預設 image 是 `-distroless` | 看 chart 的 `values.yaml` | 沒有 shell；我們固定用 `13.0.7`（非 distroless），測試用 | 要改回 distroless 時留意外掛 initContainer 之類需要 shell 的做法 |
