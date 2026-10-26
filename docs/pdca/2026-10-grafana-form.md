# 回饋表單放進 Grafana（Business Forms 外掛）（2026-10）

**狀態**：完成（Grafana 12.2 實測：預填、填寫、送出寫回 ES、誤報路徑、服務掛掉）　　**日期**：2026-10-26　　**相關**：分支 `claude/grafana-form`；前置 [expert-feedback](2026-10-expert-feedback.md)（小網頁）
**模組**：⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：老師傅平常開著 Grafana。回饋要跳到另一個網址（`feedback_web` 的小網頁），多一個地方、畫面也不一樣。問題是：Grafana 能不能直接當回饋入口？
- **前提**：Grafana 本身不能寫 ES（ES 資料來源只能讀）。所以「收回饋、寫 ES」還是要 `feedback_web` 的 API，只是把**表單畫面**搬進 Grafana。
- **目的**：Grafana 儀表板裡一個表單：從通知點進來就預填那件告警（標題、摘要、AI 摘要、相似事故），老師傅填完按送出，寫回 `aiops-feedback`、`aiops-incidents`，跟小網頁寫的一模一樣。
- **範圍**：用 Grafana 社群外掛 Business Forms（`volkovlabs-form-panel`，Apache 2.0、Grafana 簽章）。`feedback_web` 加預填用的 JSON 和 CORS。小網頁留著（沒有 Grafana、或不能裝外掛時用）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/feedback_web/__main__.py` | `GET /api/alert?alert=<id>`：一層的 JSON（key 對到表單元素的 id）；CORS：`FEEDBACK_CORS_ORIGIN` 列的網址才回 `Access-Control-Allow-Origin`，只有 `/api/*`；`OPTIONS` 回 204（瀏覽器送 JSON 前的 preflight） | 表單是從使用者瀏覽器直接打回饋服務，跟 Grafana 不同網域 |
| `engine/feedback_web/app.py` | `form_json`：預填的欄位，相似事故攤成幾行文字 | 外掛的預填只認一層的 key |
| `engine/configs/grafana/dashboards/aiops-feedback.json`（新）、`dashboards.yaml`（新） | 儀表板 `aiops-feedback`：變數 `alert`（網址帶 `var-alert=<id>`）；表單面板 16 個欄位（4 個唯讀顯示、判斷三選一、真的時顯示根因分類／根因／處置／怎麼看出來的／查了哪裡／下次怎麼更快、誤報時顯示原因／補充、名字、備註）；預填 `GET /api/alert`、送出 `POST /api/feedback`（JSON）；送出後成功／失敗的提示；面板掛一個 Grafana 內建資料來源的查詢 | 選項跟小網頁同一份（測試比對） |
| `engine/configs/grafana/alert-rules.yaml` | annotation 多 `feedback`：Grafana 表單的網址 | 通知裡的連結 |
| `engine/deploy/feedback-web.yaml` | `FEEDBACK_CORS_ORIGIN` | |
| `engine/tests/test_grafana_form.py`（新） | 表單欄位 id 都是服務認得的、選項跟服務的一樣、網址裡除了 `${alert}` 沒有變數、面板有查詢；CORS：允許的網址有標頭、別的網址沒有、沒設就沒有、網頁本身永遠沒有、`OPTIONS` 204、找不到告警回 404 JSON | |

## Check：測試的目的與結果

用 docker 起 Grafana 12.2.0，掛上外掛 6.3.1 和這份儀表板，`feedback_web` 對著測試 ES（`loop_e2e` 跑完的資料），Playwright 開真的瀏覽器操作。

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 外掛裝得上 | 掛外掛目錄啟動 | 註冊成功、簽章 `valid`；**最新的 6.3.5 要 Grafana ≥ 12.3，12.2 只能用 6.3.1** |
| 從通知點進來就預填 | 開 `/d/aiops-feedback?var-alert=a-20261006-0705-mes-api` | 告警 id、標題、摘要、AI 摘要、相似事故都填好；選「真的」之前細節欄位藏著 |
| 填完送出寫回 ES | 選真的、根因分類 DB、根因、處置、怎麼看出來的、勾兩項、名字，按送出 | 右上角「已記錄，謝謝」；`aiops-incidents` 那件：confirmed、根因「Oracle 統計資訊又過期（從 Grafana 填）」、`checked` 兩項、`confirmed_by: grafana-測試` |
| 誤報路徑 | 另一件 mes-report 選誤報、原因「月底批次」、補充 | 細節欄位藏起來、原因欄位出現；ES：事故 `false_positive`、回饋 `fp_reason: batch`、note 是補充文字、series 對 |
| 回饋服務掛掉 | 停掉 `feedback_web` 再開 | 表單空的，紅框「Initial Error: Failed to fetch」，看得出有問題 |
| 邏輯 | pytest 311、ruff | 過 |

**過程中踩到的三個坑**（都已處理，記在儀表板和測試裡）：
1. 外掛要等面板的「資料查詢」完成才發預填請求；面板沒有查詢就**永遠不預填**。掛一個 Grafana 內建資料來源的查詢當觸發。
2. 外掛替換變數時會做 URL 編碼：網址用變數 `${feedback_api}` 會變成 `http%3A%2F%2F…`、打到 Grafana 自己。網址只能寫死，部署時改。
3. 瀏覽器語系字串是 `en-US@posix` 時 Grafana 前端起不來（跟外掛無關，是測試環境的問題，指定語系就好）。

## Act：Check 後沒改善的、還沒解決的

- **要能裝外掛**（在什麼情況下發現：Plan 時）：公司 Grafana 管理者要同意裝 `volkovlabs-form-panel` 6.3.1；內網沒辦法連 grafana.com 就把外掛目錄放進 `/var/lib/grafana/plugins`（這次就是這樣測的）。Grafana 升到 12.3 以上可以用新版。
- **回饋服務的網址寫死在儀表板裡**（`http://localhost:18080`，兩處）：部署時要改成使用者瀏覽器連得到的網址（Ingress），還要開 `FEEDBACK_CORS_ORIGIN`。通知裡 Grafana 的網址也是範例。
- **身分還是自己填名字**：Grafana 知道是誰登入，但這次沒有把 Grafana 的使用者帶進表單（外掛的程式碼裡拿得到 `context.grafana.user`，下一輪可以預填 `by`、設成唯讀）。
- **服務掛掉時的訊息是外掛的英文**（Failed to fetch），送出失敗的中文提示有寫，但預填就失敗時看不到。
- 小網頁和 Grafana 表單兩份畫面要一起維護；選項由測試比對，改一邊沒改另一邊會紅。
