# Troubleshooting

> **類型：操作 How-To**

只寫結論：症狀 → 原因 → 做法。依區塊分。

## Elastic APM 的資料（engine 讀不到、數字不對）

以下是依 APM 資料形狀推論的，還沒在真實 APM 資料上遇到過；欄位對照見 [aiops-engine/code/configs/apm/README.md](../aiops-engine/code/configs/apm/README.md)。

| 症狀 | 原因 | 做法 |
|---|---|---|
| `detect` 有序列一筆都沒查到 | 索引名或欄位名跟公司的 APM 不一樣 | 各拿一筆 `GET <索引>/_search?size=1`，對 README 的欄位表，改 `configs/apm/*.yaml` |
| 延遲數字差一百萬倍、永遠超過門檻 | APM 的直方圖是微秒，序列漏了 `scale: 1.0e-6` | 加 `scale`；`min_abs`、`threshold`、`gt` 一律寫秒 |
| 告警全進 `unassigned` | agent 沒設 `labels.dept`、`labels.group` | agent 加全域標籤，或 engine 加「服務 → 部門」對照 |
| 根因報告說沒有 trace、或段不完整 | APM 的 transaction sample rate 不是 1.0，trace-merge 合不出完整請求 | 設 1.0 |
| 根因報告的 log 區是空的 | 服務沒開 log shipping，只有 `logs-apm.error-*` 的例外 | 開 log shipping，或接受只看例外 |
| `trace-merge` 一個 trace 都沒合出來 | `traces-apm-*` 沒有 `event.ingested`（Fleet 的 pipeline 才蓋） | 確認資料流有蓋；沒有的話 `trace_merge.updated_trace_ids` 改用 `@timestamp` |

## Grafana 告警

| 症狀 | 原因 | 做法 |
|---|---|---|
| 規則狀態 Error，ES 回 400 | Grafana 的 terms 預設用 `_term` 排序，ES 不認得（實測 9.5；8.19 也已不支援 `_term`） | 每個 terms 都寫 `orderBy: _count`（`grafana/code/alert-rules.yaml` 已寫） |
| data source 測試回 403 | Grafana 會先打 `GET /` 看版本 | key 要有 cluster `monitor` 權限（`elasticsearch-setup.sh` 的 Grafana 唯讀 key 已給） |
| 同一件事每分鐘收到「舊的 resolved＋新的 firing」 | `summary` 是告警的 label，內容一變就是另一則 | engine 寫的 summary 不放「持續 N 分鐘」「等 N 項異常」這種每分鐘會變的字（`aiops-engine/code/investigator/alerts.py`） |
| 件還開著，Grafana 卻發 resolved，之後又 firing | 一件只有一筆、每輪覆蓋；Grafana 的查詢範圍到「排定的評估時間」為止，查詢晚幾秒才送出。覆蓋剛好落在這幾秒之間時，`@timestamp` 晚於範圍結束點，查到 0 筆 | engine 寫開著的件和心跳時，`@timestamp` 往前 1 分鐘（`cmd_alert.STAMP_LAG`）。驗證：`e2e/pipeline/run-alert-flap.py` |
| ES 連不上時收到 `DatasourceError`，不是「AIOps 停了」，而且一次兩則 | 規則 `execErrState: Error` 時 Grafana 發自己的 DatasourceError；兩條規則各發一則 | 要它也叫「AIOps 停了」就把心跳規則改 `execErrState: Alerting`；事件規則的 DatasourceError 用 notification policy 靜音（還沒改，要跟管 Grafana 的人定） |
| 儀表板查 `_exists_:report` 永遠沒資料 | `aiops-alerts` 的 `report` 是 `index: false` | 用 `category:service` 篩 |
| `aiops-rca` 儀表板「這件事故的異常」No data（規則觸發的事故一定空） | 原本查 `status:(warning OR anomaly)` 又只依時間範圍篩：規則觸發的異常沒有 `status`，而且撈到的不一定是這件的 | 已改：`investigator alert` 併件時把 `alert_id` 寫回 `aiops-anomalies`（`cmd_alert.tag_anomalies`），面板查 `alert_id:"$alert"`；改版前已結束的件撈不到（當時沒寫回） |

## AIOps engine

| 症狀 | 原因 | 做法 |
|---|---|---|
| 根因報告「最慢那個請求的 log」少了幾行 | log 的 `@timestamp` 是應用時間，時鐘偏或請求結束後才寫，離 span 十幾分鐘 | `logs_of_traces` 已前後多放 30 分鐘；更多的看 `quality.log_clock_skew` |
| 下游（DB 連線池）跟上游（前端 API）同時出事，`aiops-alerts` 開了兩件 | 這段時間的 trace 算不出「上游呼叫下游」：trace context 沒傳到下游、trace 的 `service.name` 跟異常的服務名不同、`alerts.chain` 關了；或兩件開始時間差超過 `close_after` | `investigator topology --window 2h` 看有沒有那條邊；alert 的輸出有「呼叫關係查不到」就是查 trace 失敗（[rca README](../aiops-engine/code/configs/rca/README.md#事件整合alert)） |
| 上游那件「恢復」、同時下游開了新的一件（連帶上游） | 上游先被偵測到、下游晚一輪：下游開件時把上游那件整件併進來（`merged_into`） | 正常；看下游那件就好。要避免就縮短偵測延遲（規則窗、排程間隔） |
| A→B→C 三個一起出事，開成兩件 | 事件整合只併一層（直接呼叫者）：C 連帶 B，A 自己一件 | 已知限制（[cross-service-aggregation](pdca/phase3-locate/2026-10-cross-service-aggregation.md) Act） |
| Kafka 落後在變少（消費者追上來），卻報「落後一直增加」 | `increase` 把數字變小當成計數器歸零重來，把新的值整個算成增加；落後是量測值不是計數器 | 會上下動的量測值用 `type: delta`（最後 − 最前）；`increase` 只用在計數器（`kafka.consumer_lag_growing` 已改） |
| 新建的 Pod 一直 Pending 才 2 分半就報（`for: 5m`）；新 Deployment 1 分半就報副本不夠 | 舊的 `for` 只要求 for 裡最早的點離現在超過一半；新對象從第一筆就成立時，一半就報 | 已改：對象要在 for 開頭之前就有資料（`detector/rules.py`） |
| 核准的規則草稿有一條壞掉（缺欄位、查詢錯），`rules` 整輪都沒跑 | 一條規則丟例外就整個指令結束，其他規則也沒寫 | 已改：只跳過那一條、其他照跑照寫，最後 exit 1、印「N 條失敗」 |
| 規則草稿設了 `expires_at`，過期後還在報 | `approved_rules` 只看 `status`，沒看 `expires_at` | 已改：跟 `--at` 的時間比，到期就不載入 |
| Pod 同儕橫向比對報剛起來的新 Pod CPU 高 | 啟動時 JIT、載入類別，前幾分鐘 CPU 高 | `peer.yaml` 的 `warmup`（5m）：起來不到 5 分鐘的 Pod 不比 |
| 擴容時舊 Pod 被報「記憶體比其他 Pod 高很多」 | 新 Pod 比舊 Pod 多、記憶體還在長，中位數被新 Pod 拉低 | 同上 `warmup`；heap 長得比 5 分鐘久的服務調長 `warmup`（要比 `window` 短） |
| 全部 Pod 都壞，同儕比對卻只報最嚴重的一個 | 只看「比其他 Pod 高多少」，沒看其他 Pod 本身壞了 | `max_peer_median`：其他 Pod 中位數到規則門檻（5%、2 秒）就不挑，交給規則 |
| 2 個副本的服務壞了一個，同儕比對不報 | `min_peers: 3`：2 個分不出誰怪 | 正常；看規則 `http.error_rate_floor`、baseline |
| 根因報告的「平常」數字跟出事時差不多（例如平常 p95 也是 1.5 秒） | 一週前沒資料時，「平常」改用出事前一段、中間空 `ref_gap`；偵測比事故晚約 4 分鐘，`ref_gap` 太短就包到事故 | `ref_gap` 要大於偵測延遲；正式設定 10 分鐘（`aiops-engine/code/configs/rca/rca.yaml`） |
| `quality` 中斷：`bulk 寫入 aiops-anomalies 失敗 ... Non-standard token 'NaN'` | 缺欄位那組（例如 log 沒有 namespace）的 key 被 pandas 3 變成 NaN | 已修（`quality._frame`）；舊版這一輪的結果全沒寫 |
| trace 有 N+1，`trace.segment.repeated` 沒報、反而報一個沒有「→」的新段 | DB span 只有新版 `db.system.name`，段名沒有對象 | 已補這個欄位；Client span 什麼對象欄位都沒有的仍看不到 |
| 程式 catch 掉 DB 例外、回 200，`trace.segment.swallowed` 沒報 | Java agent 會把丟例外的那段標成錯誤，舊定義只算沒標錯誤的 | 已改：入口沒標錯誤就算吞掉（已決定維持；重試後成功也會算，另外排除留到下一輪） |
| 某支 API 一直沒有 trace 基準線 | 每點抽到的請求 < `min_requests` 5 | 流量太少的 API 學不到（5 分鐘至少 5 個請求） |
| 只有一部分請求變慢，trace 段的 time_ms 沒報 | 每點抽的請求太少時，平常的慢請求長尾讓平均值飄，基準線很寬（每點 20 個時實測門檻約 430 ms） | `trace.yaml` 的 `sample_per_bucket` 已改成 100（合成資料上 10% 請求多 2 秒報得出來）；慢的比例更低時仍報不出來，看 p90 或慢請求比例還沒做 |
| Pod 沒有 `app.kubernetes.io/name`，`quality.log_missing_identity` 卻沒報 | log pipeline 退回用 container 名稱（例如 `app`），不算缺 | 已知缺口；看 `service.name` 是不是 container 名稱 |
| span 某欄位型別錯，failure store 卻沒有 | traces 是 logsdb、ignore_malformed：文件存進去，那個欄位丟掉 | 查 `_ignored`；`quality.ingest_rejected` 看不到這種 |
| 收集系統停了半小時以上，每個服務都被報成「流量掉到 0」 | 偵測時有 baseline、整段沒資料的服務一律補 0 | 已改成只在 index 有資料的時間窗補 0（`es.alive_times`），[baseline-verify](pdca/phase2-anomaly/2026-10-baseline-verify.md) |
| 稀少的 error log，baseline 學出來的平常次數偏高 | 用序列自己的 filter（只數 error）判斷收集系統有沒有在跑，沒人出錯的小時被當「不知道」 | 已改成看整個 index；實測 λ 1.68 → 1.21（實際 1.2） |
| `train`／`detect` 整個失敗：`KeyError: 'end'`、`PatternError` | `aiops-tuning` 裡有缺欄位、正規表示式錯的調整 | 已改成略過那一筆並印「⚠ 略過調整 <id>」；照提示 `adjust remove --id` |
| p95 一直報 anomaly，值在 0.175、0.375 之間跳 | 直方圖的 p95 落在桶中點，MAD＝0，跨一格 z 約 23 | p95 已設 `instant_z: null`（跨一格只算 warning，連續 2 窗才 anomaly；合成資料一週 anomaly 3.52% → 0.93%，warning 變多）；仍吵的對象 `tune --z`；根本修法待改程式（σ 至少半個桶寬，[threshold-tune](pdca/phase2-anomaly/2026-10-baseline-threshold-tune.md)） |
| 根因報告說「這段時間沒有這個服務的 trace」，Kibana 卻看得到 | 鑰匙對不上：路由沒參數化（`/items/123`）、舊版 Java agent 的 span 名稱沒有 method（`/stock/{id}`）；Pod 沒有 `app.kubernetes.io/name` label 時，trace 的 `service.name` 是應用自設的、可能跟 metrics 不同（推論） | 不給 `--route` 再跑；用 metrics 的 `k8s.pod.name` 查 trace 的 `service.name`；根本做法是應用修路由模板、Pod 加 label（[metrics-trace](pdca/phase1-collect/2026-10-metrics-trace.md)） |
| 開了台灣行事曆，假日那天工廠（24×7）服務的故障也沒報 | 台灣行事曆每一天沒有 `match`，變成所有服務都不學、不判斷 | `calendar.yaml` 的 taiwan 來源寫 `match` 限定週休的系統（來源層級的 `match` 會套到每一天） |
| `adjust add --start 2026-10-20T02:00` 排除的時段差 8 小時 | 舊版把沒寫時區的時間當 UTC | 現在當台北時間（`train.timezone`）；舊資料用 `adjust list` 看，差 8 小時的重寫一次 |
| 查 `aiops-chronic` 找不到天天觸發的規則 | 規則那類的 `keys` 是 `series`＋對象（`service`…），不是只有服務 | 用 `keys.series` 找 |

