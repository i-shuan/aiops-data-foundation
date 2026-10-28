# metrics 異常 → 用服務＋路由＋時間窗撈 trace：邊緣情況（2026-10）

**狀態**：已完成（只驗證，沒改 RCA）　　**日期**：2026-10-28　　**相關**：分支 `claude/p1-metrics-trace-6f8a23`；[rca/README「metrics 異常怎麼串到 trace、log」](../../rca/README.md#metrics-異常怎麼串到-tracelog)、[field-unify](2026-10-field-unify.md)、[rca-demo](../phase3-locate/2026-10-rca-demo.md)
**模組**：① 收值、② 清資料、⑦ 根因分析

## Plan：這一次要達到什麼

- **需求**：metrics 是聚合的、沒有 trace_id，根因分析用「服務＋路由＋時間窗」去撈 trace（`investigator/es.py` 的 `entry_filter`、`sample_trace_ids`）。不知道哪些情況會撈錯、撈不到。
- **目的**：每種邊緣情況都有可重跑的測試、預期和實測數字；撈不到的情況能被發現。
- **範圍**：只驗證。路由名稱、指標名稱、counter 歸零、抓取間隔與時鐘差、部分變慢、不到 1 秒的變慢、`service.name` 不一致。不改 RCA 演算法。

## Do：改動哪些 code、改了什麼、為何要改

無 RCA code 改動，只加測試：

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/local-k3s/metrics-trace-apps.yaml` | 模擬服務 mt-edge：7 條路由，各測一種情況 | 同一個 Pod 同時產生 metrics（Prometheus 直方圖）和 trace，正解寫在 span 上（`test.slow`） |
| `e2e/local-k3s/metrics_trace_check.py` | 讀 ES、每項印 PASS／FAIL 和數字 | 直接呼叫 RCA 的 `entry_filter`、`entry_stats`、`sample_trace_ids`、`_route_of`，不另寫一套 |
| `e2e/local-k3s/run-metrics-trace-e2e.sh` | 部署 → 正常 → 事故 11 分鐘 → 容器重啟 → Pod 重建 → 檢查，約 25 分鐘 | 可重跑；`HOLD_LOCK=1` 整段拿 cluster 鎖；trace 有一分鐘斷掉就不報結果 |

- 量測環境：rancher-desktop、ES 9.5.5、Collector 設定 `6c59f67`（p1-durability 修過 loadbalancing 佇列的版本）、trace-sampler `RANDOM_PCT=100`。
- 「正式只隨機留 5%」用 trace_id 前兩碼 < `0d`（13/256）模擬；慢（≥ 1 秒）、錯的照正式規則全留。
- mt-edge 每條路由約 5.3 個請求／秒（5 分鐘約 1,600 個）。

## Check：測試的目的與結果

`HOLD_LOCK=1 ES_URL=http://127.0.0.1:31200 e2e/local-k3s/run-metrics-trace-e2e.sh`，2026-10-28 00:28～00:50Z，全部 PASS。

| 情境 | 預期 | 實際 |
|---|---|---|
| metrics 路由沒參數化（`/items/123`）、trace 有 | `_route_of` 組出 `GET /items/123`，撈到 0 | 0 筆；trace 實際有 1,594 筆 |
| trace 沒參數化（`GET /orders/42`）、metrics 有 | 撈到 0 | 0 筆；實際 1,594 筆 |
| 舊版 Java agent：span 名稱只有 `/stock/{id}` | 撈到 0（`entry_filter` 比對 span `name`） | 0 筆；實際 1,594 筆 |
| 退回只用服務、不限路由 | 撈得到，但混了別的路由 | 9,563 筆（7 條路由） |
| Java `http_server_request_duration_seconds` | 改成 OTel 名、`http.route`、狀態碼數字 | 6 條路由都改了；舊名 0 筆、`http_route` 0 筆 |
| Spring Micrometer `http_server_requests_seconds`（label `uri`） | 不改名，規則、RCA 看不到 | 原名 43 筆；在 OTel 名底下 0 筆 |
| 容器重啟（同一個 Pod），直方圖從 0 累加 | 沒負值、沒尖峰，掉一次抓取 | 少第一次抓取（30 秒）：177 個請求；其他每筆跟 trace 數差 ≤ 1 |
| Pod 刪掉重建（新名稱） | 同上 | 少 88 個（舊 Pod 最後一次抓取後、新 Pod 第一次抓取） |
| 每筆 metrics 涵蓋的時間 | 30 秒 | 32 筆全部 30.0 秒 |
| 慢的請求在 metrics、trace 落在同一個 5 分鐘 | 大多數同一個 | 361 個裡 7 個（1.9%）不同 |
| trace 主機時鐘快 1／3／5 秒 | 差不多 | 1.4%／1.1%／0.6% 不同 |
| trace 主機時鐘慢 5 秒 | 變多 | 4.2% 不同 |
| 事故開始：metrics 比 trace 晚 | ≤ 30 秒＋請求耗時 | 16.6 秒 |
| 10% 請求慢 2 秒：慢的有沒有被挑到 | 全部挑到 | 164／164 |
| 同上：樣本（最多 200）裡慢的比例 | 多數 | 82%（164／200），另 36 個是正常的 |
| 200ms → 800ms（不到 1 秒），正式取樣 | 約 5% | 1,599 個請求剩 74 個（4.6%）；p95 835ms（metrics 875ms） |
| metrics 與 trace 的 `service.name` 不一致 | 撈到 0 | 0 筆 |
| 同上：用 metrics 的 Pod 名反查 trace | 找得出 trace 用的名字 | `mt-edge` 9,563、`mt-edge-otel` 1,594 |

結論：

- **名稱對不上是唯一會整個撈不到的情況**：路由沒參數化、舊 agent 的 span 名稱、`service.name` 不一致，三種都 0 筆。報告只寫「沒有這個服務的 trace（服務沒埋點、取樣太低、或名稱不對）」，分不出是哪一種。
- **時間對齊不是問題**：metrics 記在「請求結束後下一次抓取」，trace 記開始時間；跨 5 分鐘邊界的只有 1.9%。時鐘差 ±5 秒內最多 4.2%。trace 時鐘快反而更準（抵掉抓取延遲）。
- **counter 歸零不會產生負值或尖峰**：`cumulativetodelta` 把重啟後第一次抓取丟掉，少 30 秒的請求；換 Pod 也一樣。
- **部分變慢挑得到**：慢的 100% 在樣本裡；樣本固定 200 個，慢的不到 200 個時，剩下的用正常請求補（這次 18%），`compare_segments` 的平均會被稀釋。
- **不到 1 秒的變慢**：只剩約 5%。每條路由每 5 分鐘 1,600 個請求時有 74 個可比；流量小的路由（每 5 分鐘 150 個）約 7 個（推論）。
- ES 把明確分格的直方圖存成指數直方圖，metrics 的 p95 是近似值（200～220ms 的請求，p95 算成 175ms）。

**沒測到的**：真的 Java agent、.NET（用 busybox 模擬它們吐的名字）；多副本同時重啟；慢的請求超過 200 個時漏掉多少（推論：只挑最慢的 200 個，其餘漏掉）；Pod 不同節點的時鐘差（這次只在 trace 時間上平移模擬）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [⑦] `entry_filter` 用 span `name` 比路由 | 舊 agent 的 span 名稱 `/stock/{id}` 撈到 0 | 舊版 Java agent 的服務撈不到 trace | 改比 `attributes.http.route`＋`http.request.method` |
| [⑦] 撈到 0 筆時不說原因 | 三種名稱不一致都只印同一句 | 要人自己猜是路由、服務名還是取樣 | 0 筆時：不限路由再數一次；用 metrics 的 Pod 反查 trace 的 `service.name` |
| [②] Micrometer 的 `http_server_requests_seconds` 不改名 | Spring 的 Micrometer 指標，label 是 `uri` | 用 Micrometer 的服務，規則、baseline、RCA 都看不到 | node Collector 加一組對照；`quality.metric_name_legacy` 也盯這個名字 |
| [②] 路由沒參數化 | metrics 是 `/items/123` | 對不到 trace，序列也會爆量 | `quality` 已有路由沒參數化的檢查；要應用修 |
| [⑦] 樣本固定 200 個 | 10% 慢時 18% 是正常請求 | 段落平均被稀釋 | 已在 rca/README 限制表（加「卡在這段的比例」、中位數） |

- **決定**：「服務＋路由＋時間窗」的關聯在時間、counter 歸零、部分變慢上都夠準；要改的是名稱對不上時撈不到、也不說原因。
- **下一輪**：改 `entry_filter` 用 `attributes.http.route`、撈到 0 筆時印原因；Collector 加 Micrometer 對照。
