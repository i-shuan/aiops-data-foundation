# POC 驗收：KPI、故障情境、Go/No-Go（2026-10）

**狀態**：進行中（計畫；使用者看過後開任務）　　**日期**：2026-10-29　　**相關**：分支 `claude/poc-acceptance-plan`；現況評估見 [architecture.md](../../architecture.md) 第二部分
**模組**：①～⑨（跨階段；驗收的是整條線）

> 這份的「POC」是對外驗收，涵蓋本 repo 的 Phase 1～5；**不是** [README.md](../README.md) 的「Phase 1 收值」。

## Plan：這一次要達到什麼

- **需求**：四項能力（異常分析、metrics→trace→log、RCA＋LLM、learning loop）都跑得通，但沒有對外可驗收的指標與情境，向高層只能說「demo 能跑」。
- **目的**：照使用者給的 6 個 KPI、4 個故障情境、3 個 Go/No-Go 條件驗收。每個 KPI 有量法、有腳本、有現況數字；缺的功能補齊；在 1 組真實 Java 或 .NET 服務上把 4 個情境跑完。
- **範圍**：
  - 做：補 4 個缺口（下表）、每個 KPI 的量測腳本、4 個情境的注入腳本（本機先用 `e2e/k8s/rca-demo` 的模擬服務跑通，驗收時換真實服務）。
  - 不做：動態拓撲圖、自動分群、graph database（使用者文件的 Phase 2）。

### KPI 對照：目標、現況、差距

| KPI | 目標 | 現況（依據） | 差距 |
|---|---|---|---|
| 連鎖警報聚合率 | ≥ 80% | 事件整合只併「同服務、同時間」（`investigator/alerts.py` 的 `entity_of`）。情境 1 的前端 API 與 DB 連線池是兩個服務，會開成 2 件 | **沒做**：用 trace 的父子關係（一層）把下游引發的上游異常併進同一件 |
| 回饋標記解析率 | ≥ 90% | 回饋表單是結構化欄位（真的／誤報、根因分類、處置、怎麼看出來的），沒有從自然語言抽取的步驟 | **沒做**：LLM 把自由文字抽成 `feedback_type`、`identified_cause`、`target_service`、`suggested_action`，寫進 `aiops-tuning`（`status: proposed`，審核機制已有） |
| 端到端串查成功率 | 100% | 有：metrics 異常 → 慢在哪一段 → 最慢請求的 span → 以 `trace_id` 撈 log（[rca-demo](../phase3-locate/2026-10-rca-demo.md)；K8s 上 2026-10-29 再跑一次，六段齊全） | 量法要寫清楚：metrics 沒有 trace ID，是用「服務＋路由＋時間窗」對到 trace，再用 `trace_id` 對 log（[metrics-trace](../phase1-collect/2026-10-metrics-trace.md)） |
| 根因初篩時間（MTTI） | < 3 分鐘 | 實測注入到開成一件＋報告 **3 分 40 秒**（[rca-demo](../phase3-locate/2026-10-rca-demo.md)）：規則要 5 分鐘窗內超標、排程每分鐘一次 | 差 40 秒：調規則時間窗或排程間隔；動態基準線學習的 p95 連續 2 窗約 1～2 分鐘 |
| 相似事故 Top-3 召回率 | ≥ 70% | 檢索（欄位比對、有權重）與評測指令 `investigator similar-eval`（留一法 recall@3）都有；匯入格式＝`aiops-incidents` 欄位（[similar-incidents](../phase4-rca-llm/2026-10-similar-incidents.md)） | **缺資料**：知識庫只有模擬的 2 筆；要 15～30 筆真實事故單，再補一支匯入指令 |
| RCA 建議採納率 | ≥ 60% | 表單有「真的／誤報／根因報錯」，`detector metrics` 算得出根因命中率 | 缺「建議的排查動作有參考價值」這一題 |

### 情境對照：現在能到哪、要補什麼

| 情境 | 現在能到哪 | 要補 |
|---|---|---|
| 1 DB 連線池耗盡（下游引發連鎖） | demo 就是它：wip-worker 查 Oracle 變慢、連線池逾時 → 報告指到那段 SQL 與那行 log | 跨服務併成一件；真實 Java/.NET 服務上重跑 |
| 2 已知歷史故障再現 | 相似事故附在報告最前面，LLM 摘要引用 [I1]、含當時處置 | 歷史事故單匯入；「排查動作與歷史處置吻合」的評分方式 |
| 3 週期性離峰高負載 | `tests/loop_e2e.py` 已驗：月底批次誤報 3 次 → 建議放寬 → 回測 → 人核准才生效 | LLM 自然語言抽取（KPI 第 2 項） |
| 4 第三方 API 中斷 | 報告指到最慢的那一段；demo 有 `POST → sms-gateway` 這段 | 實際注入 10 秒延遲＋504，確認歸因到外部端點（`server.address`）而不是自己的程式；目前只驗過 DB 那段 |

### 量法（每個 KPI 怎麼算出一個數字）

| KPI | 怎麼量 |
|---|---|
| 聚合率 | 情境 1 注入 ≥ 5 次；每次看 `aiops-alerts` 開了幾件。併成 1 件算成功。另記**誤併率**：不相關的兩件被併成一件的次數 |
| 解析率 | ≥ 20 句值班常用的回饋語料，人先標標準答案；LLM 抽取的四個欄位全對才算對 |
| 串查成功率 | 每次事故的報告要同時有：慢在哪一段、最慢請求的 `trace_id`、依 `trace_id` 撈到的 log 行；缺一項算失敗 |
| MTTI | 腳本（`e2e/k8s/rca-demo/measure-mtti.sh`）記注入時間；`aiops-alerts` 那件第一次帶 `report` 的 `report_at` 減注入時間（`@timestamp` 故意往前 1 分鐘寫，不能用，見 [scenario4-mtti](../phase3-locate/2026-10-scenario4-mtti.md)）；4 個情境各量 ≥ 3 次取中位數 |
| 召回率 | 匯入事故單後跑 `similar-eval`（留一法 recall@3） |
| 採納率 | 表單加兩題：「可能根因有參考價值」「建議的排查動作有參考價值」；正面數 ÷ 總回饋數 |

### 任務拆分（各自開 PDCA）

| # | 任務 | 完成的樣子 | 大小 |
|---|---|---|---|
| 1 | 跨服務事件聚合（一層父子關係） | 從這段時間的 trace 算出「誰呼叫誰」；下游服務的件開著時，上游同時段的異常併進同一件，報告指到下游；有誤併的防護（只併有呼叫關係、時間重疊的） | 大；Go/No-Go 條件 |
| 2 | 回饋的 LLM 抽取＋採納率欄位 | 表單多一格自由文字、兩題評分；`feedback add` 把文字抽成四欄寫 `aiops-tuning`（proposed）；20 句語料的解析率 | 中 |
| 3 | 情境 4 注入＋MTTI 量測 | `rca-demo` 加 `incident-external`（sms-gateway 10 秒＋504）；`measure-mtti.sh` 量 4 個情境 | 小 |
| 4 | 歷史事故單匯入＋召回率 | `investigator import-incidents <csv/json>`；匯入後 `similar-eval` 的 recall@3 | 小；資料要使用者給 |

順序：1 先開（最大、是 Go/No-Go）；2、3、4 可並行。

### 前提（使用者提供）

- 1 組真實 Java 或 .NET 服務，可接上 Collector（自動埋點只在測試應用驗過：[sdk-inject](../phase1-collect/2026-10-sdk-inject.md)）。
- 15～30 筆歷史事故單，欄位：`title`、`start`、`end`、`services`、`root_cause_category`、`root_cause`、`action`、`how_detected`、`checked`。

### 時程與判定

| 週 | 做什麼 |
|---|---|
| 1 | 匯入事故單；接真實服務；任務 1～4 完成並在模擬服務上跑通 |
| 2 | 真實服務上依序注入情境 1～4，記 6 個 KPI 與值班評分 |

Go：串查成功率 100%、聚合率 ≥ 80%、召回率 ≥ 70% 三項都到。任一沒到就是 No-Go，差距寫進 Act。

## Do：改動哪些 code、改了什麼、為何要改

無 code 改動：這一輪只寫計畫。任務 1～4 各自的 PDCA 記改動。

## Check：測試的目的與結果

待執行。結果照「量法」那張表填：

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 6 個 KPI 各一列 | 見「量法」 | 待填 |
| 4 個情境各一列 | 見「情境對照」的預期行為 | 待填 |

## Act：Check 後沒改善的、還沒解決的

待執行。已知風險：

| 風險 | 在什麼情況下會發生 | 影響 | 處理 |
|---|---|---|---|
| 跨服務聚合誤併 | 兩個無關的服務剛好同時出事 | 一件裡混了兩個根因 | 只併有呼叫關係且時間重疊的；量誤併率 |
| 真實服務接不上 Collector | 應用版本、網路、憑證 | 情境只能在模擬服務跑，說服力打折 | 週 1 先接，接不上就提早講 |
| 事故單不足 15 筆或欄位不齊 | 歷史紀錄散在工單系統 | 召回率算不出來 | 匯入指令容許缺欄位；不足就在報告寫明樣本數 |

- **決定**：照這份計畫開 4 個任務，順序 1 → 2、3、4 並行。
- **下一輪**：任務 1 的 PDCA。
