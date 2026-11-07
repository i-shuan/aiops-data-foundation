# 整合儀表板：metrics → trace → log → AI 根因（Elastic APM 資料）（2026-11）

**狀態**：已完成（已合進 master）　　**日期**：2026-11-06　　**相關**：分支 `claude/apm-dashboard`（從 master `8ed32ba` 開）；舊的示範儀表板（`e2e/k8s/rca-demo/dashboard.json`，2026-11-02 隨自建收值刪除，截圖 [rca-demo-dashboard.png](../phase3-locate/img/rca-demo-dashboard.png)）；[APM 來源](2026-11-apm-source.md)
**模組**：⑧ 出口（Grafana）

## Plan

- **需求**：以前有一頁把「metrics 哪裡異常 → trace 慢在哪一段 → log 那幾段發生什麼事 → AI 根因」放在同一頁，值班一眼看完。自建收值刪掉後這頁跟著沒了；現在的三個儀表板（事故與根因、學習迴圈、回饋）都只看 AIOps 自己寫的索引，看不到原始的 metrics、trace、log。
- **目的**：重做成讀公司 Elastic APM 的整合儀表板，上半是原始資料（APM 的 metrics、trace、log），下半接現有的事故與根因面板。
- **範圍**：
  - Grafana 多三個唯讀 data source：`apm-metrics`（`metrics-apm.transaction.1m-*`）、`apm-traces`（`traces-apm-*`）、`apm-logs`（`logs-apm.error-*`、`logs-apm.app.*`）；`elasticsearch/kibana-dev-tools.txt` 第 4 段 Grafana key 的讀取範圍同步加。
  - 新儀表板 `aiops-overview`：①延遲 p95、②請求數與錯誤率、③trace 每一段耗時、④error log（帶 `trace.id`）、⑤AIOps 事件與 AI 摘要、⑥根因報告全文。選一個服務與時間範圍，整頁跟著動。
  - 驗證用的合成 APM 資料 `tests/apm_synth.py`：欄位照 `configs/apm/README.md` 對照表（`transaction.duration.histogram`、`processor.event`、`span.destination.service.resource`…），一個「下游資料庫連線池滿」的事故。用 engine 自己跑 `rules`→`trace-merge`→`alert` 產生事件與報告，不手寫 AIOps 的文件。
- **不做**：不動 engine 的判斷邏輯；不接 K8s 狀態與 Pod CPU／記憶體（APM 沒送，見 `configs/apm/README.md`）；不取代現有三個儀表板。
- **風險**：沒有真的公司 APM 資料，欄位以官方 mapping 與 README 對照表為準，合成資料只能證明「面板語法對、欄位對得上我們的假設」，不能證明公司環境的欄位一模一樣——上線前照 README 第 4 步用 `GET <索引>/_search?size=1` 對一次。

## Do

- **Grafana**：`datasource.yaml` 多 `apm-metrics`（`metrics-apm.transaction.1m-*`）、`apm-traces`（`traces-apm-*`）、`apm-logs`（`logs-apm.*`，Log 面板每行顯示 `error.exception.message`）；兩個指標類的 `timeInterval: 1m`（APM 一分鐘一筆，自動間隔比它短會每隔一格是空的、線連不起來）。新儀表板 `aiops-overview.json`：① 每支 API 的 p95（`percentiles` 讀 `transaction.duration.histogram`）② 請求數依結果 ③ 每一段平均耗時（依 `span.name`）＋最慢請求表 ④ error log ⑤ AIOps 事件 ⑥ 摘要、相似事故、根因報告全文（⑤⑥ 從 `aiops-rca` 複製，同一個 `alert` 變數）；變數 `service`（多選，All）選服務。chart 的 ConfigMap 用 glob 載入，不用改範本。
- **ES 的 key**：`elasticsearch/kibana-dev-tools.txt` 第 4 段 Grafana key 加讀三個 APM pattern，並寫明不想讓 Grafana 讀 trace／log（含 SQL、例外訊息）就拿掉。
- **合成資料** `tests/apm_synth.py`：寫進 ES 8.x 內建的 APM data stream（`metrics-apm.transaction.1m-default`、`traces-apm-default`、`logs-apm.error-default`，mapping 跟真的 APM 一樣）；情境是 mes-api → wip-worker → Oracle，最後 25 分鐘連線池逾時。
- **踩到的**：①用點號的鍵（`"processor.event"`）寫入，ES 內建的 APM pipeline 以為 `processor.event` 沒填、補成 `span`，transaction 全被當成 span——apm-server 送的是巢狀物件，合成資料改成 `nest()` 後寫入；②pipeline 會把 `event.ingested` 蓋成寫入時間，trace-merge 要等 `settle_seconds`（30 秒）之後才讀得到，合成完要等一下；③`aiops-state` 記著上次合到哪，重灌資料要清掉。
- `tests/test_dashboards.py`：每個儀表板用到的 data source 都有定義、總覽上半讀 APM 下半讀 aiops-alerts、APM 的索引 pattern 跟 engine 設定一致，加合成資料的小檢查。

## Check

| 要驗什麼 | 怎麼驗 | 結果 |
|---|---|---|
| engine 在 APM 格式的資料上整條跑得通（重構後第一次） | 合成 APM 資料 → `detector rules`（`configs/apm/rules.yaml`）→ `detector trace-merge`（`trace.yaml`）→ `investigator alert`（`rca.yaml`） | ✅ 規則觸發 4 個（錯誤率 >5%：10～11%；p95 >2 秒：3.2～3.4 秒）；合了 540 個 trace；兩件合成一件「wip-worker，連帶上游 mes-api」（依 trace 的呼叫關係併件）；告警帶 `dept` D3120、`group` mes（來自 `labels.*`） |
| 報告有 trace 的瀑布與 log | 讀那件的 `report` | ✅ 最慢請求的瀑布：mes-api › GET /lots/{id} → wip-worker › GET /wip → `SELECT LOT_HISTORY → oracle`，自己花 2501 ms；依 `trace_id` 撈到「Connection pool timeout…」；「這些請求經過的服務」圖有 mes-api → wip-worker → oracle |
| 3 個 APM data source 在 Grafana 13.0.7 健康、儀表板載入 | `/api/datasources/uid/<uid>/health`、搜尋 | ✅ 三個都 healthy；`aiops-overview` 載入 |
| 整頁畫得出來 | 截圖（Playwright，Grafana 13.0.7，時間範圍涵蓋事故） | ✅ ① 三條線在 07:19 一起跳到 3～4 秒；② 失敗（黃）比例同時上升；③ 一段（SELECT LOT_HISTORY）從約 0 跳到 1.5～2 秒；最慢請求表、④ 每行是「Connection pool timeout…」、⑤⑥ 有事件與報告。第一版 ① 沒有線、④ 顯示 `_id`，是 data source 少 `timeInterval`、少 `logMessageField`，已補 |
| 單元測試 | `tests/test_dashboards.py` | ✅ 6 項 |

## Act

| 問題 | 在什麼情況下發現的 | 打算 |
|---|---|---|
| 沒有公司真的 APM 資料，欄位以 ES 內建 mapping 與 `configs/apm/README.md` 為準 | 做合成資料時 | 上線前照 README 第 4 步對一次欄位；`logMessageField` 在真資料上是 `error.exception[].message`（陣列），要看 Log 面板顯示是否正常 |
| 報告的「平常」比較不準，結論指到錯的那一段（「多花 0.1 ms」） | 合成資料只有 90 分鐘、出事前只有 65 分鐘，沒有 `compare_with: 7d` 的歷史 | 這是合成資料的限制，不是引擎的問題；有 7 天以上的真資料再看 |
| ② 的「請求數」是 trace 取樣到的 transaction，不是全部請求 | Grafana 的 ES data source 沒有「直方圖的 value_count」 | 若公司的 APM 有取樣，② 只看比例；真正的請求數在 engine 的 `http.requests` 序列 |
| 服務變數 `service` 對 log 面板用的是 `service.name`，一般 log 的欄位名可能不同 | 只測了例外 log | 有開 log shipping 的服務再對 |
| 沒有 K8s／Pod 的圖 | APM 沒送 system metrics（README） | 舊的示範頁也沒有 |
