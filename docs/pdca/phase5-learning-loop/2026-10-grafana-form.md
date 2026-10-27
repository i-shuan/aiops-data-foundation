# 回饋表單放進 Grafana（Business Forms 外掛）（2026-10）

**狀態**：完成（Grafana 12.2 實測：預填、填寫、送出寫回 ES、誤報路徑、服務掛掉）　　**日期**：2026-10-26　　**相關**：分支 `claude/grafana-form`；前置 [expert-feedback](2026-10-expert-feedback.md)（小網頁）
**模組**：⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：老師傅平常開著 Grafana；回饋要跳到另一個網址（`feedback_web` 的小網頁），多一個地方、畫面也不一樣。
- **前提**：Grafana 的 ES 資料來源只能讀，不能寫。收回饋、寫 ES 還是 `feedback_web` 的 API，只把**表單畫面**搬進 Grafana。
- **目的**：Grafana 儀表板裡一個表單：從通知點進來就預填那件告警（標題、摘要、AI 摘要、相似事故），送出寫回 `aiops-feedback`、`aiops-incidents`，跟小網頁寫的一樣。
- **範圍**：用社群外掛 Business Forms（`volkovlabs-form-panel`，Apache 2.0、Grafana 簽章）；`feedback_web` 加預填用的 JSON 和 CORS；小網頁留著（沒有 Grafana、不能裝外掛時用）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/feedback_web/__main__.py` | `GET /api/alert?alert=<id>`：一層的 JSON，key 對到表單元素 id | 外掛的預填只認一層的 key |
| 同上 | CORS：`FEEDBACK_CORS_ORIGIN` 列的網址才回 `Access-Control-Allow-Origin`，只限 `/api/*` | 表單從瀏覽器直接打回饋服務，跟 Grafana 不同網域 |
| 同上 | `OPTIONS` 回 204 | 瀏覽器送 JSON 前的 preflight |
| `engine/feedback_web/app.py` | `form_json`：預填欄位，相似事故攤成幾行文字 | 同上，一層的 key |
| `engine/configs/grafana/dashboards/aiops-feedback.json`、`dashboards.yaml`（新） | 儀表板 `aiops-feedback`，變數 `alert`（網址帶 `var-alert=<id>`）；表單面板見下 | 選項跟小網頁同一份（測試比對） |
| `engine/configs/grafana/alert-rules.yaml` | annotation 多 `feedback`：Grafana 表單的網址 | 通知裡的連結 |
| `engine/deploy/feedback-web.yaml` | `FEEDBACK_CORS_ORIGIN` | — |
| `engine/tests/test_grafana_form.py`（新） | 儀表板與 CORS 的檢查，見下 | 改壞會被抓到 |

- 表單面板 16 個欄位：4 個唯讀顯示、判斷三選一、名字、備註。
- 真的時顯示：根因分類、根因、處置、怎麼看出來的、查了哪裡、下次怎麼更快。
- 誤報時顯示：原因、補充。
- 預填 `GET /api/alert`、送出 `POST /api/feedback`（JSON）；送出後有成功／失敗提示。
- 面板掛一個 Grafana 內建資料來源的查詢（觸發預填，見下方外掛的限制）。
- 測試：欄位 id 都是服務認得的、選項跟服務一樣、網址裡除了 `${alert}` 沒有變數、面板有查詢。
- 測試（CORS）：允許的網址有標頭、別的網址和沒設時沒有、網頁本身永遠沒有、`OPTIONS` 204、找不到告警回 404 JSON。

## Check：測試的目的與結果

docker 起 Grafana 12.2.0，掛外掛 6.3.1 和這份儀表板；`feedback_web` 對著 `loop_e2e` 跑完的測試 ES；Playwright 開真的瀏覽器操作。

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 外掛裝得上 | 掛外掛目錄啟動 | 註冊成功、簽章 `valid` |
| 版本相容 | — | **6.3.5 要 Grafana ≥ 12.3；12.2 只能用 6.3.1** |
| 從通知點進來就預填 | 開 `/d/aiops-feedback?var-alert=a-20261006-0705-mes-api` | id、標題、摘要、AI 摘要、相似事故都填好；選「真的」前細節欄位藏著 |
| 填完送出寫回 ES | 選真的、DB、根因、處置、怎麼看出來的、勾兩項、名字，送出 | 右上角「已記錄，謝謝」 |
| 寫進去的內容 | 查 `aiops-incidents` 那件 | confirmed、根因「Oracle 統計資訊又過期（從 Grafana 填）」、`checked` 兩項 |
| 填的人 | 同上 | `confirmed_by: grafana-測試` |
| 誤報路徑 | mes-report 選誤報、原因「月底批次」、補充 | 細節欄位藏起、原因欄位出現 |
| 誤報寫進去的內容 | 查 ES | 事故 `false_positive`；回饋 `fp_reason: batch`、note 是補充、series 對 |
| 回饋服務掛掉 | 停掉 `feedback_web` 再開 | 表單空的，紅框「Initial Error: Failed to fetch」 |
| 邏輯 | pytest 311、ruff | 過 |

### 外掛的限制（已處理，記在儀表板和測試裡）

| 限制 | 做法 |
|---|---|
| 面板的資料查詢完成才發預填請求；沒有查詢就永遠不預填 | 掛一個 Grafana 內建資料來源的查詢當觸發 |
| 替換變數時做 URL 編碼：`${feedback_api}` 變成 `http%3A%2F%2F…`、打到 Grafana 自己 | 網址寫死，部署時改 |
| 瀏覽器語系是 `en-US@posix` 時 Grafana 前端起不來（測試環境問題，跟外掛無關） | 指定語系 |

### K8s 上放外掛的做法

都是把外掛資料夾放進 Grafana 的外掛資料夾：

| 做法 | 怎麼做 | 連外網 | 適合 |
|---|---|---|---|
| A. 啟動時自己下載 | 官方 Helm chart values 加 `plugins: ["volkovlabs-form-panel 6.3.1"]` | 要（Pod 連 grafana.com） | 連得到外網的環境 |
| **B. initContainer 先放好（採用）** | initContainer 把 zip 解到共用 volume，Grafana 掛同一個 volume；zip 放公司 Nexus | 不用 | 正式環境 |
| C. k3d 掛本機資料夾 | `k3d cluster create -v <本機路徑>:/plugins@all`，Grafana 用 hostPath 掛 `/plugins` | 不用 | 本機測試 |

- 這輪只驗過「docker 起 Grafana、掛資料夾」，跟 C 同原理。
- B 的腳本、manifest 與驗證見 [grafana-plugin-init](2026-10-grafana-plugin-init.md)。

**決定（2026-10-26）**：

- 回饋入口**用 Grafana 表單**；回饋服務的小網頁留著當備案。
- 外掛用「資料夾放進現有 Grafana 的外掛資料夾、重啟」的方式裝，不重做 Grafana image。
- 給管 Grafana 的人的步驟：[INSTALL-form-plugin.md](../../../engine/configs/grafana/INSTALL-form-plugin.md)（下載網址、SHA-256 `1eece14d…98d0`、確認方法）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 要管 Grafana 的人同意、照步驟裝 | Plan 時 | 公司 Grafana 沒裝過，只在測試的 12.2 驗過 | 問：版本是不是 12.2、外掛資料夾在哪、第三方外掛要不要審查 |
| 回饋服務網址寫死在儀表板（`http://localhost:18080`，兩處） | 外掛的變數限制 | 部署時要改 | 改成瀏覽器連得到的網址（Ingress）、開 `FEEDBACK_CORS_ORIGIN` |
| 通知裡 Grafana 的網址是範例 | — | 部署時要改 | 同上 |
| 身分還是自己填名字 | 這輪沒帶 Grafana 使用者 | Grafana 知道誰登入，表單沒用上 | 外掛拿得到 `context.grafana.user`：預填 `by`、設唯讀 |
| 服務掛掉時的訊息是外掛的英文（Failed to fetch） | 服務掛掉的測試 | 送出失敗有中文提示，預填失敗時看不到 | 待排 |
| 小網頁和 Grafana 表單兩份畫面 | — | 要一起維護 | 選項由測試比對，改一邊沒改另一邊會紅 |
