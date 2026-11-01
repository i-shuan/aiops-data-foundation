# trace 每一段的統計改讀合成後的 traces-merged-*（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/trace-segments-merged`；來源 [open-issues 第 1 節「讀端兩套」](../meta/2026-10-open-issues.md)、[trace-merge 的 Act](../phase1-collect/2026-10-trace-merge.md)
**模組**：④ 判斷（trace 每一段的動態基準線）

## Plan：這一次要達到什麼

- **需求**：trace 改成 100% 收、engine 每分鐘合成一個請求一筆之後，根因分析讀 `traces-merged-*`，但 trace 每一段的統計（`trace-collect` → `aiops-trace-segments`）仍從 raw（`traces-generic.otel-*`）抽樣。讀端兩套：raw 只留 2 天、沒有合成的摘要，而且「哪些是入口」在兩邊各算一次。
- **目的**：`trace-collect`、`trace-detect` 讀 merged；入口直接用合成時算好的 `entries`，抽樣結果與讀 raw 一致（同一個時段、同一批 API 的每段統計對得上）。
- **範圍**：
  - 做：`sample_entries` 依 index 分兩條路（merged 用 `nested` → `composite` → `top_hits`；raw 照舊，測試用的合成資料仍寫 raw）；`trace.yaml` 的 `traces_index` 改 merged；文件。
  - 不做：改 `summarize`、`train`、`detect` 的算法；trace-quality 的合成資料改寫 merged（它寫 raw 再跑一次 `trace-merge` 也可以，這次不動）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/trace.py` | `sample_entries` 看 index：`traces-merged-*` 走新的 `_sample_entries_merged`（`nested: entries` → `composite`（`entries.service` × `entries.name` × `entries.start_ms` 的 `histogram`，一格 = interval）→ `top_hits` 依 `entries.span_id` 排序取 `per_bucket` 個；hit 的 `_id` 就是 trace_id、`_source` 是入口的 span_id）；raw 的路照舊 | 合成時已經算好哪些是入口（`entries`），不用再用 kind、parent 判一次；`span_id` 是隨機 16 進位，排序取前幾個等於隨機抽。nested 裡拿不到整筆的 `@timestamp`，時段改用入口自己的 `start_ms` 切，查詢範圍兩邊放寬 5 分鐘再只留 [start, end) 的時段 |
| `configs/baseline/trace.yaml` | `traces_index: traces-merged-*` | 根因分析、「誰呼叫誰」、每一段的統計都讀同一份 |
| `tests/test_trace.py` | 新增 merged 抽樣（查詢形狀、hit 轉 `Sampled`、窗口外的時段丟掉、翻頁）與 raw 路不變的測試 | |
| `docs/trace/README.md`、open-issues 第 1 節「讀端兩套」 | 改成讀 merged | 跟著程式 |

`spans_of`、`spans_by_id` 本來就依 index 分 raw／merged，`collect` 其餘不用改。寫 raw 的測試（`tests/trace_edge.yaml`、`trace_k3s.yaml`、trace-quality 的合成資料）仍走 raw 那條路。

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 讀 merged 算出來的每段統計跟讀 raw 一樣 | 測試 ES 上同一個 10 分鐘窗口（04:00～04:10Z，三個服務、7 個 API），raw、merged 各抽 100 個請求算 `summarize`，不寫 ES，逐列比 | 兩邊都抽到 600 個請求、14 列，**沒有只在其中一邊的列**；`error_rate` 完全一樣；`time_ms` 相對差中位數 1.9%；`calls` 只有 `rca-mes-api › SELECT LOT → oracle` 差 0.07（5% 快取沒命中的那段，兩邊各抽 100 個的抽樣差異），其餘一樣 |
| 查詢形狀、hit 轉換、窗口邊界、翻頁；raw 路不變 | `tests/test_trace.py` 兩個新測試 | 通過；全部單元測試通過 |
| ES 8.19 允許 `composite` 放在 `nested` 底下 | 對測試 ES 打一次：最近 3 小時 290 個桶，112 毫秒 | 可以（以前的紀錄寫「要把 composite 改成 nested，這次不碰」，實測不用改算法，只是多包一層） |

**沒測到的**：`trace-quality` 的合成資料仍寫 raw（它用 `tests/trace_edge.yaml`，index 是 raw），沒有拿 merged 跑過那套情境；正式排程器上 `trace-collect` 讀 merged 沒跑過（merged 晚 1～2 分鐘合成，`trace-detect` 的窗口是 10 分鐘、每 5 分鐘重算覆蓋，應該趕得上，沒量）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| merged 晚 1～2 分鐘合成，`trace-detect` 看的最後一格可能還沒合完 | 設計時 | 最後一格抽到的請求偏少；下一輪（5 分鐘後）重算同一格會覆蓋 | 排程器上跑一天看 `aiops-trace-segments` 最後一格的 `requests` 有沒有明顯偏低 |
| 資料品質檢查仍讀 raw | 範圍外 | 讀端還是兩套，但品質檢查看的是 span 的欄位（路由名稱、身分），merged 沒有那些欄位 | 不改 |
