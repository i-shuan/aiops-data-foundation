# 刪本機 sh、Grafana 回饋改用 engine api 的網頁（2026-11）

**狀態**：已完成　　**日期**：2026-11-05　　**相關**：分支 `claude/remove-local-sh`（從 master 開）
**模組**：⑧⑨

## Plan：這一次要達到什麼

- **需求**：repo 裡要在本機跑的 sh（`grafana/helmchart/load-plugin.sh`）不要了，只留 Pod 裡會自己跑的；`grafana/` 檔案太多。回饋表單靠 Grafana 外掛（Business Forms），裝外掛要準備 zip、initContainer、本機切 ConfigMap，使用者決定**先用 HTML 當表單**。
- **目的**：Grafana 預設不裝外掛也能回饋、回報漏報、重跑根因分析；repo 裡沒有本機 sh；外掛路線保留成選用。
- **範圍**：做：刪 `load-plugin.sh`、repo 裡的外掛 zip、chart 的 ConfigMap 外掛路徑；刪社群 chart 的副本 `values-official.yaml` 與產生它的 `build-official-values.py`；api 的回饋網頁加「重跑根因分析」、新的漏報網頁；預設儀表板改成連結；外掛表單版搬到 `forms/`、只有 `plugin.enabled` 才載入。不做：把網頁內嵌進 Grafana（iframe 要改 Grafana 的安全設定）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `grafana/helmchart/load-plugin.sh`、`grafana/plugins/volkovlabs-form-panel-6.3.1.zip`、`.gitattributes` | 刪 | 本機 sh；zip 只有它在用；lfs 規則只為這個 zip |
| `grafana/values-official.yaml`、`grafana/code/build-official-values.py` | 刪 | `helmchart/files/` 的自動產生副本（社群 chart 用），同一份內容維護兩處 |
| `grafana/helmchart/files/install-plugin.sh` | 拿掉從 ConfigMap 分段接回 zip 的分支，只從 `PLUGIN_URL` 下載 | Pod 裡跑的、保留；分段是給已刪的 `load-plugin.sh` 用的 |
| `grafana/helmchart/templates/grafana.yaml`、`values.yaml` | 拿掉 `plugin.source`／`plugin.parts`、`plugin-zip` volume；`plugin.enabled` 預設 `false` | 外掛選用 |
| `grafana/helmchart/templates/configmaps.yaml` | `files/dashboards/forms/*.json` 只有 `plugin.enabled` 才載入 | 沒外掛時外掛面板會顯示錯誤 |
| `grafana/helmchart/files/dashboards/aiops-feedback.json`（新內容）、`forms/aiops-feedback-forms.json`（原檔搬家，uid `aiops-feedback-forms`） | 預設儀表板：一個 HTML 文字面板，三個連結（回饋表單 `&full=1`、報告全文、回報漏報），網址由 chart 的 `feedbackUrl` 取代 | uid 不變，通知裡的 `feedback` 連結不用改 |
| `aiops-engine/code/api/app.py` | `render_form` 開著的件最上面加「重跑根因分析」表單、`notice` 提示；新 `render_missed` | 外掛版有的三件事，網頁版都要有 |
| `aiops-engine/code/api/__main__.py` | `GET /missed`、`POST /missed`（form）、`POST /rca`（form → `request_rca`＋丟 alert → 303 轉回回饋頁帶提示） | 同上 |
| `aiops-engine/code/investigator/es.py` | `entry_stats`：回應沒有 `aggregations`（索引還不存在）就當作 0 個請求 | K8s 實測時發現：工廠版剛上線、`lot-paths-*` 還沒有，整份根因報告變「根因分析失敗：'aggregations'」 |
| 測試：`test_api_endpoints.py`（`HtmlPages` 5 個、`EntryStatsWithoutIndex` 1 個）、`test_deploy.py`（預設沒有 initContainer、只有連結版儀表板；外掛開才多 forms）、`test_api.py`、`test_grafana_form.py`（指到 forms/） | | |
| 文件：`grafana/README.md`、`docs/deploy-how-to.md`、`docs/feedback/README.md`、`docs/data-storage-reference.md`、`aiops-engine/README.md`、`docs/README.md`、`alert-rules.yaml` 註解、舊 PDCA 一處死連結 | 預設網頁、外掛選用、社群 chart 那條路拿掉 | |

## Check：測試的目的與結果

| 測試目的 | 怎麼測 | 結果 |
|---|---|---|
| 既有行為不變 | `python -m pytest -q` | 720 過 |
| 網頁版三件事 | `HtmlPages`：開著的件有重跑表單、結束的沒有；`POST /rca` 303 轉回帶提示、ES 有標記；結束的件提示「已經結束」不寫；`/missed` 送出寫一筆 `verdict: missed`；填錯 400 且保留已填的 | 過 |
| chart 預設不裝外掛 | `test_deploy`：預設沒有 initContainer、儀表板只有連結版、網址被 `feedbackUrl` 換掉；`plugin.enabled=true` 有 emptyDir、多載入 forms | 過 |
| 實裝 | 測試 cluster：engine image 重建、`aiops-api` 滾動；`grafana-new` 升級成 `plugin.enabled=false`。儀表板的三個連結帶出真的 alert id（`…/feedback?alert=a-20261105-1128-K8S-01&full=1`）；回饋頁最上面有重跑框，按下去綠色提示「已排進佇列」，alert worker log「人要求重跑根因分析」；漏報頁欄位齊全 | 過 |
| 工廠版沒有履歷時的報告 | 同一件重跑後讀 `/report` | 修之前「根因分析失敗：'aggregations'」；修之後：異常、履歷段寫「沒有履歷，略過」、log、事故前變更、結論都出 |
| 文件 | `tools/check-docs.py` | 0 問題 |

- **沒測到的**：漏報網頁沒在叢集裡實際送一筆（單元測試有）；舊的瀏覽器對 `datetime-local` 的支援。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 回饋表單不在 Grafana 裡面，是另開分頁 | 設計時：Grafana 的文字面板預設會把 iframe 洗掉，要開 `disable_sanitize_html`（安全設定） | 使用者多一次跳頁 | 要內嵌再評估 |
| [⑦] 工廠版的 LLM 範本摘要寫「還沒有 trace 可以看」 | K8s 上看工廠版報告 | 用語不對（工廠應該是履歷）；報告本文是對的 | 範本跟著 plugin 的用語，另案 |
| api 的網頁沒有登入 | 之前就有 | `by` 是自己填的名字 | 放內網；要鎖加 Ingress 驗證 |

- **決定**：預設用網頁，外掛選用（`forms/` 與 `install-plugin.sh` 留著）；社群 chart 那條路拿掉。
