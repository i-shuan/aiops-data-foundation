# 示範儀表板：每件點開看自己的根因全文、帶老師傅回饋連結（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/demo-alert-drilldown-feedback`；`e2e/k8s/rca-demo/`
**模組**：⑧ 出口、⑨ feedback（只動示範環境，不動正式程式）

## Plan：這一次要達到什麼

- **需求**（使用者看示範 Grafana 時提出）：
  - ⑤ 有好幾件事件，⑥「根因分析報告全文」卻把時間窗內所有件的報告堆在一起，看不出哪份是哪件。
  - 想要點 ⑤ 的某一件就看到**那一件**的根因全文。
  - 示範裡找不到資深維運專家（老師傅）回饋的入口。
- **目的**：
  - ⑤ 每列一件（`aiops-alerts` 本來就一件一筆，`_id` = `alert_id`）；點標題跳出選單：「在下面看這件的根因全文」（設儀表板變數 `alert_id`）、「新分頁開報告全文」、「老師傅回饋」。
  - ⑥ 只顯示變數 `alert_id` 選的那一件；儀表板上方也有下拉選單可以選。
  - 示範跑回饋網頁（`feedback_web`，本機 `localhost:18080`），`investigator alert` 寫進每件的 `report_url`、`feedback_url` 指過去；⑤ 的連結用每件自己的這兩個欄位（跟正式環境通知帶的同一個）。
- **範圍**：
  - 做：`e2e/k8s/rca-demo/dashboard.json`（⑤ 連結、⑥ 篩選、變數）；`run-rca-demo.sh` 加 `feedback` 子指令（本機起回饋網頁）、`tick` 用帶網址的 rca 設定（寫在 `RCA_DEMO_STATE`，不改 repo 的 `rca.yaml`）；實跑一次截圖。
  - 不做：正式的 Grafana 儀表板、`rca.yaml` 預設值、回饋網頁本身；把回饋網頁部署進 cluster（示範的 engine 本來就在本機跑）；清測試 ES 裡的舊告警。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/k8s/rca-demo/dashboard.json` | 變數 `alert_id`（「看哪一件」，`aiops-alerts` 裡 `category:service` 的 `alert_id`，字串倒序＝最新的在前）；⑤ 只列服務的件（不含心跳、資料品質）、`title` 欄加三個連結：`var-alert_id=${__data.fields.alert_id}`（同一張儀表板、時間範圍不變）、`${__data.fields.report_url}`、`${__data.fields.feedback_url}`（新分頁）；`alert_id`、`report_url`、`feedback_url` 三欄帶進來但隱藏；⑥ 篩 `alert_id:"$alert_id"`、只取 1 筆 | ⑥ 原本是「時間窗內所有件的報告」各取最新 5 筆疊在一起；`aiops-alerts` 一件一筆，用 `alert_id` 篩就是那一件。連結用每件自己的欄位，跟正式通知帶的網址同一個 |
| `e2e/k8s/rca-demo/run-rca-demo.sh` | `feedback` 子指令：本機 `FEEDBACK_PORT`（從 `FEEDBACK_BASE` 取，預設 18080）起 `feedback_web`，用 engine 的 key；`tick` 改用 `rca_cfg` 產生的 `RCA_DEMO_STATE/rca-demo.yaml`（`rca.yaml`＋`alerts.report_url`、`feedback_url`） | 示範的 engine 本來就在本機跑，回饋網頁也在本機，值班的瀏覽器連得到；不改 repo 的 `rca.yaml` 預設值 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 每件寫進回饋網址 | 情境 1 注入後跑 `loop 4`，查 `aiops-alerts` | 新開的 `a-20261101-0736-rca-mes-api` 帶 `report_url`、`feedback_url`（`http://localhost:18080/…?alert=<alert_id>`）；之後再寫到的舊件也補上 |
| ⑤ 一列一件、點標題有選單 | Grafana 實開、點 grafana 那件的標題 | 跳出三項：「在下面（⑥）看這件的根因全文」「新分頁開這件的報告全文」「老師傅回饋」 |
| ⑥ 只顯示選的那件 | 點第一項 | 網址變成 `var-alert_id=a-20261101-0524-grafana`，⑥ 標題與內容換成 grafana 那件（`rule.container.memory_leak`）；預設選最新的 `rca-mes-api` 那件 |
| 回饋頁可開 | 開 `feedback_url` | 表單預填那件的摘要、AI 摘要、過去相似事故、「根因報告全文」連結、兩題評分、真的／誤報、一句話；**沒有替使用者送出** |

**沒測到的**：在回饋頁送出（寫 `aiops-feedback`）這次沒做，表單本身之前驗過；第一次部署時變數的 `orderBy` 寫成 `_term`，Grafana 不認（變數出現警告、⑥ 空白），改成 `term` 後才正常。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 示範的件在 loop 停了之後不再寫入，時間窗選太短 ⑤⑥ 會是 No data | 使用者看 14:52～14:57 的窗 | 看不到件 | 示範時選「最近 3 小時」或邊看邊跑 `loop`；正式環境排程器每分鐘寫，沒有這個問題 |
| `run-rca-demo.sh up` 會重啟模擬服務 Pod，注入的故障跟著消失 | 這次重新部署 Grafana | 要重新 `incident` | 只改儀表板時直接 `helm upgrade` Grafana（腳本沒拆出來） |
| 新開的那件根因報告是「trace 也沒看到哪一段變慢」 | 這次 `up` 重啟服務後才注入、1 分鐘就開件 | 報告比之前（同樣情境）少了慢在哪一段 | 推測是 trace 合成晚 1～2 分鐘、開件時還沒合到；之前的 MTTI 量測不受影響（報告都有那一段），沒查 |
| 測試 ES 裡還有規則合成 e2e 留下的 `p2-*` 告警 | 這次示範 | ⑤ 列很多跟示範無關的件 | 清除要刪資料，等使用者決定 |
