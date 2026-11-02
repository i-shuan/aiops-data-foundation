# 示範儀表板：③ Trace 每一列看得到 trace_id（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/demo-trace-id`；e2e/k8s/rca-demo/dashboard.json
**模組**：⑧ 出口

## Plan：這一次要達到什麼

- **需求**：示範儀表板的 ③ Trace 是每一段的統計，看不出數字是哪一個 trace_id 量到的。④ Log 維持原本的樣式，但要搜得到 trace_id。
- **目的**：③ 每一列都能對到一個請求，跟報告第 4 節的 trace_id 對得起來。
- **範圍**：做：改 ③。不做：`grafana/code/` 的正式儀表板。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/k8s/rca-demo/dashboard.json` | ③ 從聚合改成原始 span（最近 200 筆），欄位 trace_id、服務、哪一段、耗時，依 trace_id 排，同一請求連在一起 | 聚合把多個請求併成一列，放不了 trace_id |
| 同上 | ④ 加 `displayedFields: [trace.id, ___LOG_LINE_BODY___]`，每行顯示 trace_id＋訊息 | 面板的搜尋框只比對畫面上的文字，trace.id 只在展開的欄位裡時搜不到 |
| 同上 | ④ 拿掉 `log.level:error`，收全部層級，載入 200 → 500 行 | ③ 也列正常請求，正常請求只有 info log，只收 error 時搜到 0/0 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 依 trace_id 聚合可不可行 | 先做 terms trace_id（依最大耗時排序）→ 服務 → 段，用 `/api/ds/query` 看回傳 | 不可行：最慢的 `c53f0c30…` 只回 `rca-mes-api` 的段，報告裡同一個請求有 `rca-wip-worker`。依指標排序的 terms 是各 shard 各取前幾名，不同服務在不同 shard 就缺段 |
| 原始 span 每個請求的段是齊的 | 重新部署示範 Grafana，瀏覽器開儀表板讀 ③ | 同一 trace_id 連續 5 段：`GET /lots/{id}`、`GET`、`GET /wip`、`SELECT LOT_HISTORY`、`POST` |
| ④ 搜得到 trace_id | 瀏覽器在 ④ 按 ctrl+F，貼 ③ 也看得到的 trace_id | 比對 1/1；改之前 0/0。正常請求 `f101d7ed…` 查得到 2 行 info log。`displayedFields` 只寫 `message` 時訊息會不見，要用 `___LOG_LINE_BODY___` |

**沒測到的**：拿掉時間欄後 ③ 的耗時欄顯示。③ 只有最近 200 筆 span、④ 只載入 500 行，超出範圍的 trace_id 搜不到，要縮短時間範圍。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| ③ 不再是每一段的平均、最大統計 | 改成原始 span | 要看哪一段變慢得看個別請求；統計由報告第 3 節提供 | 要兩者都看時再加一張面板 |
| 正式儀表板沒有這個對應 | 範圍外 | 正式環境沒有依 trace_id 看每段的表 | 要時再搬到 `grafana/code/` |
