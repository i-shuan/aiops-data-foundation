# metrics 異常怎麼找到對的 trace（2026-10）

**狀態**：決定採方案 B；Act 的程式改動還沒做　　**日期**：2026-10-23　　**相關**：[rca/README.md](../rca/README.md#metrics-異常怎麼串到-tracelog)、`aiops/aiops_rca/`

## Plan：要解決什麼

- **問題**：metrics 是統計數字，上面沒有 trace_id。metrics 發現「/checkout 變慢」之後，根因分析要自己去找對應的 trace，目前靠「服務＋路由＋時間」去撈，不保證撈到的是造成異常的那些請求。
- **想法**：OpenTelemetry 的 metrics 可以帶 **exemplar**：統計數字旁邊夾一個當時某個請求的 trace_id。如果 ES 存得下來，就能從異常的那一點直接跳到 trace。
- **驗收**：帶 exemplar 的 metrics 寫進 ES 之後，能在 ES 裡查到 trace_id。

## Do：測試怎麼做、結果

**環境**：ES 9.5.5（docker 單節點，跟 k3s 測試同版本）、OpenTelemetry Collector contrib 0.162.0（專案鎖定的版本）。

**做法**：自己組 OTLP metrics，四種型別各夾一個 exemplar（trace_id `4bf92f3577b34da6a3ce929d0e0e4736`、span_id、一個 filtered attribute）：exponential histogram（專案的請求延遲就是這種）、一般 histogram、counter、gauge。

**測兩條路**：

| 路線 | 說明 |
|---|---|
| ① ES 內建 OTLP 入口 `/_otlp/v1/metrics` | **正式環境現在用的**（見 [metrics/README.md](../metrics/README.md)） |
| ② Collector 的 `elasticsearch` 寫入元件（otel 模式） | 專案原本用過、因為逾時會掉資料而換掉的那個，當成對照 |

**怎麼判斷有沒有存**：資料寫進去後，三個地方都找：

1. 文件的 `_source`
2. index 的 mapping（找有沒有 exemplar 相關欄位）
3. 全文搜尋那個 trace_id

**結果**：

| | ① ES OTLP 入口 | ② elasticsearch 寫入元件 |
|---|---|---|
| 請求有沒有被接受 | 是（HTTP 200） | 是 |
| 數值有沒有存進去 | 有，4 種型別都在，數值正確 | 有 |
| `_source` 有 exemplar | 沒有 | 沒有 |
| mapping 有 exemplar 欄位 | 沒有 | — |
| 全文搜尋 trace_id | 0 筆 | 0 筆 |

另外在路線 ②，Collector 的 debug 輸出看得到 exemplar 和 trace_id，所以 **exemplar 有送到 ES 門口，是寫進 ES 的過程中沒有存下來**。

## Check：為什麼做不到

**結論**：在這個架構（ES 9.5.5 加 OTLP 寫入）下，metrics 帶不了 trace_id。

**為什麼**：
- 事實：兩條路線都是請求成功、數值存了、exemplar 沒存。這點是實測的。
- 原因：ES 把 OTLP metrics 轉成文件的時候，沒有保留 exemplar。**這是從結果推斷的，我沒有讀 ES 原始碼，也沒有找到官方說明這是設計如此**。
- 影響範圍：只測了 ES 9.5.5 和 Collector 0.162.0。其他版本（例如之後的 ES）可能不同，要用 `send.py` 這類腳本重測。

**沒測到的**：Java 那條路（agent 以 Prometheus 格式輸出、Collector 來抓、換算成差值）能不能把 exemplar 帶到 Collector，沒有驗證。不過因為 ES 這端不存，這條路測了也沒有用。

**就算 ES 存得下來，exemplar 也不理想**：
- 它只是 SDK 在那段時間留下的**一個樣本**，不是「造成異常的那個請求」。p95 變高是幾千個請求的結果，本來就沒有單一的「那一個」。
- 正常的 trace 只留 5%（尾端取樣），exemplar 指到正常請求時，那個 trace 大約 95% 已經被丟掉，點過去是空的。

## 方案比較

| | **A：exemplar 轉成 log 文件** | **B：用時間區間撈一批 trace** |
|---|---|---|
| 做法 | 在進 ES 之前把 exemplar 抽出來，另外寫成一筆筆 log 文件（trace_id、metric 名、數值、服務、時間）。根因分析查這個 index 拿 trace_id | metrics 異常時，用「服務＋路由＋時間」撈那段時間的一批慢的、錯的 trace，統計大家共同卡在哪一段 |
| 要不要動基礎架構 | 要。**依我的了解**，Collector 內建的轉換功能（OTTL）不能把 metrics 變成 log，要自己寫一支程式（**未驗證**）；還要部署、監控、斷線重送 | 不用。Collector、ES、應用端都不動 |
| 要不要應用端配合 | 要開 exemplar 設定（每個服務） | 不用 |
| Java 那條路 | 要另外驗證 exemplar 能不能經過「Prometheus 格式來抓 → 換算成差值」，**沒測過** | 不相關 |
| 新增的維運 | 多一個元件、多一個 index（保留天數、各 group 的寫入權限） | 無 |

### 方案 A 優缺點

| 優點 | 缺點 |
|---|---|
| 在圖上看到某個點，可以直接拿到一個確定存在的請求的 trace_id | 拿到的只是**一個樣本**，不是造成異常的那個請求，可能被 GC 停頓、網路抖動這類特例帶偏 |
| 不用靠時間區間猜 | 正常請求的 trace 95% 被取樣丟掉，常常查不到 trace |
| 寫成一筆筆文件，不會讓 metrics 的維度爆炸 | 要自己寫、自己養一個元件，多一個可能掉資料的地方；Java 路線沒驗證 |

### 方案 B 優缺點

| 優點 | 缺點 |
|---|---|
| 不加元件，只改 Python（`aiops/aiops_rca`） | 是「同時間、同路由」的關聯，不是綁定同一個請求；同時有兩種問題時要再分群 |
| 看**多數請求共同卡在哪**（例如 20 筆裡 18 筆卡同一句 SQL），特例不會主導 | 出事時挑最慢的、平常隨機挑，兩邊樣本本來就不同，差距會被放大：排名可信，絕對值要保守看 |
| 慢（超過 1 秒）、錯的、有例外的 trace 全留，撈得到 | 比 1 秒快的變慢（例如 200ms 變 800ms），只剩隨機 5% 可用，量少時結論弱 |
| 分得出兩種情況：多數卡同一段是那一段的問題；每筆卡的地方都不同是整體資源問題 | 目前每一段用平均，一兩筆特別慢的會拉高平均（要改，見 Act） |

## Act：決定與後續

**決定：採 B，不做 A。** 理由：A 花一個新元件和跨團隊配合，換到的是一個不保證是原案、而且常常查不到的樣本；B 不動基礎架構，看的是多數請求的共同點，對自動化根因分析更有用。

**先講清楚 B 現在的實際狀況**：根因分析**已經**在做一部分。它撈最慢的 200 個請求加最多 100 個出錯的，跟一週前同時段比較每一段平均花多少時間（`aiops_rca/es.py` 的 `sample_trace_ids`、`analyze.py` 的 `compare_segments`）。但**瀑布圖和用 trace_id 撈 log 只用最慢的 1 筆**（`__main__.py`）。

**要改的**（還沒做）：
1. 每一段除了平均，加「這批慢請求裡有幾成最卡的是這一段」和中位數，避免一兩筆特例拉高平均。
2. 瀑布圖、撈 log 從 1 筆改成幾筆有代表性的（卡在主要瓶頸那一段的）。
3. 補單元測試。

**前提與風險**：
- 慢的、錯的 trace 全留（尾端取樣，已做）。
- 保留 30 天、單獨放 `traces-problem` 的做法在 `claude/retention` 分支，**還沒合進 master**；現在留下的 trace 都在同一個 index，不會刪。

**下一輪要驗證**：
- 用 `send.py` 這類腳本在新版 ES 重測 exemplar 有沒有被存下來（ES 升級時順便做）。
- B 改完後，用 k3s 的故障案例（`test/local-k3s/run-rca-e2e.sh`）確認「幾成卡在這段」比只看平均更準。
