# Grafana 的 provisioning 檔搬進 helm chart（2026-11）

**狀態**：已完成　　**日期**：2026-11-02　　**相關**：分支 `claude/grafana-helm-files`；接在 [remove-collector](2026-11-remove-collector.md) 之後
**模組**：⑧

## Plan：這一次要達到什麼

- **需求**：自建的 `grafana/helmchart/` 要靠 `--set-file` 把 `grafana/code/` 的 7 個檔一個一個帶進去，指令很長；示範 Grafana 沒用 chart 裝，是用 API 放儀表板，重啟就全沒了。另外「事故與根因」儀表板的「事故」下拉在 Grafana 13 是空的，下面全部 No data；事故清單也沒說一列是一件事故。
- **目的**：`helm upgrade --install grafana grafana/helmchart -n <ns>` 一行就裝好 data source、3 條告警規則、contact point、policy、3 個儀表板，Pod 重啟後還在。
- **範圍**：做 provisioning 檔搬進 chart、儀表板的兩個修正；不做把 `grafana/code/` 其餘檔（外掛說明、官方 chart values 的產生腳本）搬走、不碰公司已有 Grafana 那條路（檔照樣能交給管 Grafana 的人）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `grafana/helmchart/files/`（從 `grafana/code/` 搬來：`datasource.yaml`、`alert-rules.yaml`、`contact-points.yaml`、`policies.yaml`、`dashboards/*.json`） | 搬家，唯一來源 | Helm 的模板只讀得到 chart 資料夾內的檔，所以不能留在 `code/` 再用模板讀 |
| `grafana/helmchart/templates/configmaps.yaml` | data source、告警改用 `.Files.Get`，儀表板用 `.Files.Glob`，渲染時把 `http://localhost:18080` 換成 `feedbackUrl` | 不再靠 `--set-file`；回饋網頁網址不用改檔 |
| `grafana/helmchart/values.yaml` | 拿掉 `provisioning.*`，加 `feedbackUrl` | 同上 |
| `grafana/helmchart/files/dashboards/aiops-rca.json` | 「事故」變數的查詢從物件改成字串；事故清單標題寫明「一列 = 一件事故」、欄位改中文、「合併的異常筆數」 | Grafana 13 不吃物件型的變數查詢（console：missing metrics and aggregations）；讓人看得出一列是多筆異常合成的一件 |
| `grafana/code/build-official-values.py`、`grafana/values-official.yaml`、`grafana/.gitlab-ci.yml` | 來源改讀 `helmchart/files/`，重新產生 values；CI 的觸發路徑加上 `helmchart/files/` | 官方 chart 那條路也要吃同一份檔 |
| `aiops-engine/tests/test_grafana_form.py` 與三處註解、`sonar-project.properties` | 路徑改成 `helmchart/files/` | 檔搬了 |
| `grafana/README.md`、`docs/deploy-how-to.md`、`docs/architecture.md`、`docs/data-storage-reference.md`、`grafana/code/INSTALL-form-plugin.md`、`aiops-engine/README.md` | 路徑與部署指令改成一行 helm；回饋網址改寫 `--set feedbackUrl` | 文件跟著現況 |
| `docs/pdca/meta/2026-10-open-issues.md` | 一個指向已搬檔案的連結改純文字 | 紀錄是當時的措辭，只修連結 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| chart 能渲染，三份檔都進了 ConfigMap | `helm lint`、`helm template --set feedbackUrl=…` 後解析 | lint 沒錯；4 個 yaml、3 個 json 都在，json 都能解析；Grafana 自己的 `{{ $labels }}` 沒被 Helm 吃掉 |
| `feedbackUrl` 真的換掉全部 | 數渲染結果裡的 `localhost:18080` | 0 個殘留，新網址 5 處（檔裡共 5 處，原先文件只寫 2 處） |
| 單一來源：官方 chart 的 values 跟 `files/` 一致 | `build-official-values.py --check` | 一致 |
| 實際裝得起來、重啟不會掉 | 用 `plugin.enabled=false` 裝進新的 namespace（沿用示範的 Secret），打 Grafana API；再 `rollout restart` | 3 個儀表板、3 條規則、3 個 contact point 都在；4 個 data source 都讀得到 ES（85／362／42／7 筆）；重啟後儀表板自己回來 |
| 修好的「事故」下拉與學習儀表板 | 瀏覽器開示範 Grafana（手動放的同一份檔） | 下拉列出事故，LLM 摘要、相似事故、報告全文有內容；學習儀表板 42 筆回饋、7 筆調整 |
| 沒弄壞既有的 | 引擎單元測試、`tools/check-docs.py` | 562 個測試過；連結、索引、舊說法、類型 0 個問題 |

- 過程中**發現的問題**：示範 Grafana 的 4 個 data source 共用一把只能讀 `aiops-alerts` 的 key，讀另外三個索引被 ES 擋，Grafana 13 卻顯示成 No data 而不是錯誤；換成部署文件 3-1 那把讀 4 個索引的 key 就好。
- **沒測到的**：外掛 initContainer 這次關掉（沒連 Nexus），沒驗；GitLab 上沒跑過；公司已有 Grafana 那條路只改了文件，沒實際放檔。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [⑧] 學習儀表板左上「mes-report 月底批次」是舊 OTel demo 寫死的查詢 | 示範時永遠是 0 | 新部署的人會看到一個無意義的 0 | 換成通用的指標，或拿掉 |
| [⑧] 回饋網頁網址只有用我們的 chart 才能用 `feedbackUrl` | 改寫部署文件時 | 公司已有 Grafana、官方 chart 兩條路仍要改檔 | 官方 chart 的 values 產生腳本也吃同一個值 |
| 事故清單和收值儀表板沒有連結 | 示範時使用者問兩者是否分開 | 要手動換儀表板看原始曲線 | 事故清單加連結，帶服務與時間 |

- **決定**：檔搬進 chart（單一來源，`code/` 只留外掛說明與腳本），不採「打包前複製」，因為 repo 少一份複製、少一個要記得跑的步驟。
- **下一輪**：真實環境跑 initContainer 外掛；GitLab CI 驗 `check-grafana-values`。
