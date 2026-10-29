# 跨服務事件聚合：一層父子關係（2026-10）

**狀態**：已完成（合成資料 e2e；K8s 上的驗證由主 session 合併後做）　　**日期**：2026-10-29　　**相關**：[POC 驗收計畫](../meta/2026-10-poc-acceptance.md) 任務 1；分支 `claude/poc-acceptance-plan` 之上
**模組**：⑥ 收斂（主）、⑦ 根因分析（以下游跑）

## Plan：這一次要達到什麼

- **需求**：情境 1「DB 連線池耗盡」時，下游 B（wip-worker）出事、上游 A（前端 API）跟著變慢。事件整合只看服務（`entity_of`），A、B 各開一件，值班收到兩則、要自己猜誰是源頭。POC 的 KPI「連鎖警報聚合率 ≥ 80%」要求只開一件、指到下游。
- **目的**：下游有件開著、直接呼叫它的上游同時段異常 → 併進下游那件（新欄位 `upstream_services`），標題看得出「B 出事，連帶 A」，根因分析以 B 跑；沒有呼叫關係、時間不重疊的照舊各開一件。
- **範圍**：
  - 做：從這段時間的 trace 算「誰呼叫誰」（重用根因分析的 `topology`）；`merge` 加跨服務併件（一層）；上游先開的件也收攏；單元測試；自己的 ES 上量聚合率與誤併率。
  - 不做：多層（A→B→C 只併直接呼叫者）、常駐拓撲（每輪從 trace 現算）、Collector、K8s cluster 驗證。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/investigator/alerts.py` | `merge` 多收 `calls={(上游, 下游)}`；上游異常併進下游開著的件、下游開件時收攏上游已開的件（`_fold`）；件多 `upstream_services`、被收攏的多 `merged_into`；`headline`／`summary` 加「；連帶上游 A」；`owner`、標題的「最嚴重那筆」只看下游自己的異常（`_own`）；新 `rca_route` | 併件規則集中在一個純函式，單元測試不用 ES。dept 跟標題若混進上游的異常，件會送錯部門、標題寫到上游的指標。上游的 API 若拿去查下游的 trace 會查空 |
| `engine/investigator/cmd_alert.py` | 新 `call_edges`：有件開著或這次有異常的服務，每個取最近 `chain_sample` 個請求的 trace，`analyze.topology` 算邊、只留兩端都是候選服務的；查失敗印一行、這輪不併跨服務。`ALERT_MAPPING` 加 `upstream_services`、`merged_into`。被收攏的件：不再把異常標成它、它的事故改 `status: merged`。根因分析的 API 改用 `AL.rca_route` | 重用現有的 span 解析與拓撲，不另寫一套；trace 查不到不能擋告警。被收攏的件若照舊寫回 `alert_id`，同一筆異常會被兩件搶；它開的 `auto` 事故留著會被「同根因 3 次」算進去 |
| `engine/investigator/es.py` | 新 `service_trace_ids`：某服務最近 n 個 Server span 的 `trace_id` | `recent_trace_ids` 只取 root span、不分服務；兩個服務同時出事時只取最新的 N 筆可能全是其中一個的 |
| `engine/configs/rca/rca.yaml` | `alerts.chain: true`、`chain_sample: 100` | 可以關；每輪多查幾個服務的 trace，量要能調 |
| `engine/tests/test_alerts.py`、`test_cmd_alert.py` | 跨服務 13 個情境（併、反序收攏、跨輪、不併、方向、多層只一層、`rca_route`、dept／標題、重跑、資料品質）；`call_edges` 用假 span 驗邊、`_incident_doc` 的 `services` 只有下游 | 見 Check |
| `engine/tests/chain_e2e.py`、`tests/rca_chain.yaml` | 自己的 ES：合成 trace（cs-portal → cs-mes-api → cs-wip-worker → oracle；cs-report 獨立）＋異常，跑 `investigator alert --at`，量聚合率與誤併率 | KPI 要有量法與腳本；預期先寫在檔頭 |
| `engine/configs/rca/README.md`、`docs/troubleshooting.md`、`docs/rca/README.md`、`docs/architecture-reference.md` | 併件條件與欄位的表、前提多一列（trace context 要傳到下游）、三條症狀、收斂第 3 層狀態 | 規格跟程式放一起；原理文件只改狀態 |

### 併件條件（規格在 [rca README](../../../engine/configs/rca/README.md#事件整合alert)）

| 情況 | 結果 |
|---|---|
| 上游 A 的異常、A 沒開著的件、A 直接呼叫的 B 有開著的件、離 B 那件開始 ≤ `close_after` | 併進 B；B 的 `upstream_services` 加 A |
| 已連帶過的 A，之後的異常 | 照同服務規則（離最後一筆 ≤ `close_after`） |
| B 開新件時 A 已有一件、開始時間差 ≤ `close_after`、A 那件沒帶上游 | A 整件併進 B；A 已在 ES 的標 `resolved`＋`merged_into` |
| 沒有邊、時間不重疊、不是直接呼叫、A 那件已帶上游 | 不併 |

呼叫方向由 trace 決定（父 span 的服務呼叫子 span 的服務），所以「留哪一件」不看誰先被偵測到。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 既有行為不變：同服務併件、weak 要 2 筆、warning 只附掛、resolved、重跑不重複、資料品質另一件、摘要穩定 | `test_alerts.py` 原 18 個、`test_cmd_alert.py` 原 12 個不改 | 全過 |
| 有呼叫關係就併、留下游 | `calls={(mes-api, wip-worker)}`，wip 先／mes 先各一次 | 都 1 件、service=wip-worker、`upstream_services=[mes-api]`、異常 id 兩筆都在；標題 `[critical] wip-worker：連線池逾時 等 2 項異常；連帶上游 mes-api` |
| 上游先寫進 ES 再來下游 | 第 1 輪只有 mes-api → 第 2 輪 wip-worker | mes-api 那件 `resolved`、`merged_into` = wip 那件；wip 那件 `started_at` 退到 mes 的第一筆 |
| 已連帶的上游跨輪繼續併；沒連帶過的上游太晚不併 | 下游開了 40 分、上游在 40 分異常（close_after 30） | 連帶過的併（4 筆一件）；沒連帶過的另開一件 |
| 沒有呼叫關係不併 | `calls` 空、或邊指到別的服務 | 2 件、都沒有 `upstream_services` |
| 方向決定留哪件 | 邊反過來（wip 呼叫 mes） | 留 mes-api、連帶 wip-worker |
| 時間不重疊不併 | 下游 0 分、上游 40 分（close_after 30） | 下游 resolved、上游另開、沒有連帶 |
| 多層只併一層 | portal → mes-api → wip-worker 三個同時；最下游先到／最上游先到 | 都 2 件：wip（連帶 mes）＋portal；或 mes（連帶 portal）＋wip |
| 上游的 API 不拿去查下游 | 下游是規則（沒 API）、上游有 API | `routes` 照舊兩支都有；`rca_route` 回 None；下游自己有一支時回那支 |
| dept、標題看下游 | 上游異常比較多、dept 不同 | `owner` 回下游的 dept；標題是下游那筆 |
| `call_edges` 只算候選服務之間的邊、同一個 trace 只讀一次 | 假 span：mes→wip→oracle、report→oracle | 邊只有 (mes-api, wip-worker)；`spans_of` 收到去重後的 trace id |
| 事故的 `services` 只有下游 | `_incident_doc` | `services=[wip]`、`upstream_services=[mes]` |
| **聚合率**（KPI ≥ 80%） | `chain_e2e`（ES 9.5.3 容器、合成 trace 29,760 個 span、異常 47 筆）：5 次連鎖情境（同 5 分鐘下游先、上游先、上游晚一個 5 分鐘、分兩輪下游先進 ES、分兩輪上游先進 ES） | **5/5 = 100%**：每次開著的只有 1 件、是 cs-wip-worker、`upstream_services=[cs-mes-api]`。第 5 次多一筆 cs-mes-api `resolved`＋`merged_into`（上游先一輪進 ES），事故改 `merged` |
| **誤併率**（0） | `chain_e2e`：5 次 cs-wip-worker 與 cs-report 同時出事（trace 裡沒有邊；下游先、report 先、晚 5 分、分兩輪、兩個都是上游端） | **0/5**：每次 2 件、都沒有 `upstream_services`、沒有 `merged_into` |
| 多層、不重疊（e2e） | portal/mes/wip 同時；wip 0 分、mes 35 分（close_after 15m） | 多層 2 件：wip（連帶 mes）＋portal；不重疊：wip `resolved`、mes 另開、沒有連帶 |
| 報告指到下游、上游異常寫回下游的 `alert_id`、Grafana 一件一筆、心跳 | `chain_e2e` 綜合檢查 | 標題 `[critical] cs-wip-worker：連線池逾時 等 4 項異常；連帶上游 cs-mes-api`；`findings.service=cs-wip-worker`、最慢的那段 `cs-wip-worker › SELECT LOT_HISTORY → oracle`；dept 是下游的 D-WIP；cs-mes-api 的 2 筆異常 `alert_id` 都是 wip 那件；事故 `services=[cs-wip-worker]`；沒有重複的 `alert_id`；心跳有寫 |
| 全部單元測試 | `cd engine && pytest -q` | 366 個全過（含新加的 13＋3 個） |

- **沒測到的**：K8s cluster 上的真實 Collector 資料（主 session 合併後做）；上游有多個下游同時開著（取開始時間最近的那件，只有單元層面的路徑、沒有情境）；Kafka 經 link 連起來的生產者／消費者（`topology` 只看 parent，不看 `links`）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 多層只併一層 | 單元測試 `test_only_one_level`：A→B→C 三個同時出事 | 開 2 件；最上游先被偵測到時，帶上游的那件指到中間的 B、不是源頭 C | 下一輪：沿邊往下游走到沒有開著的件為止，或用常駐拓撲 |
| 上游先一輪被偵測到時，Grafana 先收到上游那件、下一輪收到「上游恢復＋下游開」 | 單元測試 `test_persisted_upstream_is_merged_into_downstream`、e2e 連鎖 5 | 多一則通知；最後狀態還是一件、指到下游 | 偵測延遲縮短（規則窗、排程）；或下游開件時沿用上游那件的 `alert_id`（會讓 id 的服務名跟 `service` 不同，先不做） |
| 呼叫關係每輪從 trace 現算 | 設計 | 每輪多 N 個服務 × 1 次查詢＋讀 trace；沒有 trace 的服務（取樣掉、沒埋點）併不起來 | 常駐拓撲（每天算一次存下來）留給 POC 的 Phase 2 |
| `topology` 不看 `links` | 看程式 | .NET Kafka 消費者是新 trace、只用 link 指回生產者：生產者出事連帶消費者併不起來 | 等有 Kafka 的真實情境再做 |

- **決定**：採用「每輪從 trace 現算、只併一層、留下游」：改動只在 `merge` 與 `cmd_alert`，既有測試全過；KPI 見 Check。
- **下一輪**：主 session 在 K8s 的 `rca-demo` 上跑情境 1（真實 Collector 資料）；[README.md](../README.md) 的 POC 任務 2～4。
