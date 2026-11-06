# 資料品質檢查（2026-10）

**狀態**：已完成（已合進 master）；沒排程　　**日期**：2026-10-23　　**相關**：PR #6、[餵給 baseline 之前](../../../aiops-engine/configs/baseline/reference.md#餵給-baseline-之前擋了什麼還缺什麼)
**模組**：② 清資料（餵給 ④ 判斷之前）

## Plan：這一次要達到什麼

- **需求**：資料本身壞掉時，baseline 學出來的「平常」是錯的或學不到；中位數＋MAD 對這些壞法沒有防護：

| 壞法 | 後果 |
|---|---|
| 路由沒參數化（`/orders/123` 沒換成 `/orders/{id}`） | 每個訂單一條序列，序列數爆炸，每條都學不到 |
| 必填欄位缺（服務名、Pod） | 對不到服務，學不進任何序列 |
| 資料停了 | 收不到不是零流量 |
| 落到 `unassigned` 變多 | `dept` 清單漏同步，或 namespace 沒標 dept |

- **目的**：訓練前、偵測時，看得出資料本身怪不怪。驗收：故意放進壞資料會被抓到，正常的不報。
- **範圍**：只做規則檢查（不用學、不用歷史）。不做自動修復、不做通知。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/quality.py`（新） | `eval_cardinality`、`eval_missing`、`eval_stopped`、`eval_share`（純計算） | 計算與查詢分開，不連 ES 就能測 |
| 同上 | `fetch_*`：用 composite aggregation 查 ES | 同上 |
| 同上 | `looks_like_id` | 判斷路由是不是帶了 ID |
| `engine/configs/baseline/quality.yaml`（新） | 9 個檢查；看最近 1 小時，跟 24 小時前同一段比 | 避開上下班、夜間批次的週期；門檻調整不用改程式 |
| `engine/detector/__main__.py` | 新增 `quality` 指令（`quality_hits`、`cmd_quality`） | — |
| 同上 | 結果寫 `aiops-anomalies`（`level: quality`） | 跟 `rules`、`peer` 同一模式，收斂、根因只讀同一個 index |
| `engine/tests/test_quality.py`（新） | 各檢查的邊界案例 | — |
| `engine/configs/baseline/README.md` | 加一節說明檢查與實測 | — |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 小服務正常加幾條路由不報、暴增要報 | 單元測試，直接給數字 | 「倍數」與「增加條數」兩個條件同時成立才報 |
| 新服務沒有昨天的資料 | 單元測試 | 當成從 0 開始 |
| 文件太少 | 單元測試 | 量不夠的不看 |
| 路由樣板不被當成 ID | 單元測試 | `{id}`、`:id`、一般單字（`/health`）都不算 ID |
| 查詢在真實 mapping 下能跑（metrics 是時序資料流） | ES 9.5.5 跑過 `elasticsearch-setup.sh`；資料寫法見下 | 查得到，欄位名稱、型別對得上 |
| 壞資料抓得到 | 放 6 種壞資料（見下） | 6 種都抓到 |
| 正常的不誤報 | 1 個正常服務當對照 | 沒報 |
| 落到 unassigned 的檢查 | 放 13% 的 log 到 `k8s-unassigned-*` | **沒報**：沒有昨天的資料可比（見 Act） |

- 真實 mapping 測試：metrics 走 `/_otlp/v1/metrics`、log 直接寫 `k8s-*`。
- 抓到的壞資料：80 條像 ID 的路由、30% log 沒服務名、metrics 沒 Pod、metrics 停了、昨天九成 log 有 trace.id 今天兩成。
- 已修：沒有 `by` 的檢查（`share`）會因 `KeyError: 'by'` 當掉。
- 已修：`ref_offset: 1d` 在 pandas 有過時警告，改 `24h`。
- **沒測到**：正式環境的量下查詢要多久。
- **限制**：`cardinality` 是近似值（3,000 條以下才準）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 「unassigned 變多」「帶 trace.id 的 log 變少」只跟昨天比 | 實測放 13% unassigned 沒報 | 剛上線第一天抓不到 | 設 `max_share`（實測加上會報）或先用絕對上限 |
| 門檻（路由 500 條、缺 5% 等）是先估的 | 沒有真實資料量可校 | 可能天天誤報或漏報 | 正式環境只記錄跑 2～4 週再校 |
| unassigned 的 index 名稱（`k8s-unassigned-*`）是測試時直接寫的 | 實測用 bulk 直接寫，沒經過 Collector | 真實名稱若不同，這個檢查永遠查不到 | 在 k3s 用真資料確認 |
| 沒排程 | — | 不會自己跑 | 跟其他指令一起部署 |
| 沒測正式量下的查詢時間 | — | 可能太慢 | 上線前量 |
