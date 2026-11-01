# 示範儀表板：trace 與 log 用 trace_id 對得起來（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/demo-trace-id`；[e2e/k8s/rca-demo/dashboard.json](../../../e2e/k8s/rca-demo/dashboard.json)
**模組**：⑧ 出口

## Plan：這一次要達到什麼

- **需求**：使用者看示範儀表板，③ Trace 沒有 trace_id，跟 ④ Log、報告對不起來。
- **目的**：從慢請求找到同一個請求的 log；報告裡的 trace_id 能貼進來查。
- **範圍**：做：改示範儀表板。不做：`grafana/code/` 的正式儀表板。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/k8s/rca-demo/dashboard.json` | 新增面板 ③-2：`rca-mes-api` 的 `GET /lots/{id}` 依耗時列最慢的 15 個 trace_id | ③ 是每一段的統計，一列是很多請求，放不了 trace_id |
| 同上 | ④ 從 logs 面板改成表格，欄位 時間、服務、trace_id、訊息 | logs 面板要展開才看得到 trace_id |
| 同上 | 新增變數 `trace_id`（預設 `*`），③-2、④ 都用它過濾；兩個面板的 trace_id 都能點 | 點一個請求，或貼報告裡的 trace_id，兩邊只留那個請求 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| ③-2、④ 看得到 trace_id | 重新部署示範 Grafana，瀏覽器開儀表板 | ③-2 依耗時列出（最慢 1.58 秒）；④ 表格有 trace_id，同一請求的 `rca-mes-api` 與 `rca-wip-worker` 兩筆 log 是同一個 trace_id |
| 過濾後兩邊是同一個請求 | 網址帶最慢那筆的 `var-trace_id` | ③-2 剩 1 筆；④ 剩 1 筆：`rca-wip-worker` 的 `Connection pool timeout`，trace_id 相同 |
| 點 trace_id 的連結 | 讀畫面上連結的 href | 指回同一張儀表板，帶時間範圍與 `var-trace_id` |

**沒測到的**：沒有真的用滑鼠點；瀏覽器 console 有 `Invalid query sort type _term`，沒查是哪個查詢，畫面資料正常。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| ③-2 只列入口的 `GET /lots/{id}` | 設計 | 其他 API 的慢請求看不到 | 示範只有這條路，要用時加變數 |
| 正式儀表板沒有這個對應 | 範圍外 | 正式環境還要自己複製 trace_id | 要時再搬到 `grafana/code/` |
