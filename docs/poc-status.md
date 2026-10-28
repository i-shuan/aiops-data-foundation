# AIOps PoC：現況

每個 phase、每個模組做到哪（程式、驗證、還沒做的），以及每份 PDCA 紀錄的結論。狀態依實測結果，不用完成比例。模組編號 ①～⑨ 的定義與整條線見 [architecture.md](architecture.md)；待辦依 PDCA 分在 [pdca/README.md](pdca/README.md)。

**副標**：從資料打通、異常辨識到 RCA 說明，並以回饋閉環持續校正，逐步驗證端到端分析能力。

## 各 phase 一句話

| Phase | 現況 |
|---|---|
| 1 收值 | ✅ 三種資料收進 ES、分部門、鑰匙對得上；k3s、k3d 跑過 |
| 2 異常分析 | ✅ 靜態臨界值規則、動態基準線、Pod 同儕橫向比對、trace 段都有；門檻是估的 |
| 3 定位問題 | ✅ K8s 上整條線跑通；engine 的 CronJob 還沒跑過 |
| 4 RCA＋LLM | ✅ 相似事故、LLM 摘要（真的模型跑過）；沒有評測集 |
| 5 learning loop | ✅ 回饋 → 回測 → 核准用模擬資料跑通；真實數字等試點 |

## 各模組：程式、驗證、狀態

模組職責見 [architecture.md](architecture.md) 第 4 節；K8s 排程頻率見 [engine/deploy](../engine/deploy/README.md)。

| # | 程式／設定 | 多久跑一次 | 怎麼驗過 | 狀態 |
|---|---|---|---|---|
| ① 收值 | `sdk/`（[app-common chart](../sdk/charts/app-common)、[tracing.md](../sdk/standards/tracing.md)）、`telemetry/collectors/*.yaml` | 常駐；metrics 30 秒 | k3s `run-e2e.sh`、`run-dotnet-e2e.sh`、`run-trace-e2e.sh`；[trace-durability](pdca/phase1-collect/2026-10-trace-durability.md)、[es-startup-401](pdca/phase1-collect/2026-10-es-startup-401.md) | ✅ |
| ② 清資料 | `telemetry/collectors/common.yaml`、`telemetry/storage/` | 進來就做 | `e2e/pipeline/`；[field-unify](pdca/phase1-collect/2026-10-field-unify.md)、[time-alignment](pdca/phase1-collect/2026-10-time-alignment.md) | ✅ |
| ③ 存 | `telemetry/scripts/elasticsearch-setup.sh`、`retention.conf` | — | `run-key-rotation.sh`、`run-es-2node.sh` | ✅ |
| ④ 判斷 | `engine/detector/`、`engine/configs/baseline/*.yaml` | 每 1 分鐘（train 每天） | 單元測試；`synth_e2e`、`adaptive_e2e`、`fine_e2e`；k3s `run-rules-e2e.sh`、`run-peer-e2e.sh`、`run-trace-baseline-e2e.sh` | ✅；K8s CronJob 沒跑過 |
| ⑤ 異常總表 | `aiops-anomalies` | — | — | ✅ |
| ⑥ 收斂 | `engine/investigator/alerts.py`、`cmd_alert.py`；`rca.yaml` 的 `alerts:` | 每 1 分鐘 | `test_alerts.py`；[alert-exit](pdca/phase3-locate/2026-10-alert-exit.md) | ✅ 同服務合成一件；沿呼叫關係 📝 |
| ⑦ 根因分析 | `engine/investigator/`（`__main__.py`、`similar.py`）、`engine/core/llm.py` | 跟 ⑥ 同一次 | k3s `run-rca-e2e.sh`；`loop_e2e`；`test_similar.py`、`test_llm.py` | ✅；沒有評測集 |
| ⑧ 出口 | `engine/configs/grafana/*.yaml` | Grafana 每 1 分鐘查 | Grafana 12.2 實測；[rca-demo](pdca/phase3-locate/2026-10-rca-demo.md) | ✅；K8s CronJob 沒跑過 |
| ⑨ feedback | `engine/feedback_web/`、`cmd_feedback.py`、`cmd_backtest.py`、`cmd_metrics.py` | 網頁常駐；suggest、backtest 人跑 | `loop_e2e`；`test_feedback_web.py`、`test_feedback.py`、`test_metrics.py` | ✅（模擬資料） |

- ⑤ 欄位見 [baseline README「異常帶的欄位」](../engine/configs/baseline/README.md#異常帶的欄位)；⑥ 哪些異常會開一件、怎麼不重複開見 [rca README「事件整合」](../engine/configs/rca/README.md#事件整合alert)。
- ⑧ `aiops-alerts` 帶 `status`、`service`、`dept`、`group`、`severity`、`summary`、`report`、`feedback_url`；`summary` 是 Grafana 的 label，不放會變的字；心跳「AIOps 停了」docker 實測過。
- ⑨ 回饋表單定案用 Grafana Business Forms 外掛，回饋服務的小網頁當備案（[grafana-form](pdca/phase5-learning-loop/2026-10-grafana-form.md)）。
- 第一階段只開 rules、detect、alert、train 四個 CronJob，其他 6 個在第二階段。

## 收斂：很多筆怎麼變成一件

設計見 [rca/README.md「雜訊壓制」](rca/README.md#雜訊壓制)。

| 層 | 狀態 |
|---|---|
| ① 同一條序列連續 → 一段 | ✅ `status`、`streak`（單點 warning、連續才 anomaly）；📝 一段記成一筆 |
| ② 同一個服務、時間重疊 → 一件 | ✅ `investigator alert`；同 dept 的兩個服務是兩件 |
| ③ 沿呼叫關係 → 找源頭 | 📝 呼叫關係已有（根因分析的 `topology`） |
| ④ 排優先、靜音、限頻 | 📝 |

## 產品主線 8 步

產品的說法是 8 步（賣點：learning loop、資深維運專家（老師傅）回饋、RCA 結合 RAG）；編號仍以 ①～⑨ 為準。

| 步 | 內容 | 模組 | 現況 |
|---|---|---|---|
| 1 | 收值，三種資料共用同一套鑰匙 | ①（鑰匙名字統一屬 ②） | ✅ |
| 2 | 清資料：進來時＋餵 baseline 前（`quality`、`adjust`、`chronic`、行事曆） | ② | ✅ 程式；⚠️ 後者沒排程 |
| 3 | 異常分析：動態基準線、trace 每一段＋靜態臨界值規則、Pod 同儕橫向比對 | ④（結果寫 ⑤） | ✅ 程式；門檻是估的 |
| 4 | 串 metrics → trace → log，抓出問題在哪 | ⑦ | ✅ |
| 5 | 輔助工具：呼叫關係、瀑布圖、log 樣式、業務 ID 查交易、LLM 查詢工具 | ⑦ | ⚠️ 前三樣有 |
| 6 | 整合事件：很多筆異常合成一件 | ⑥（送出去屬 ⑧） | ⚠️ 只有同服務合成一件 |
| 7 | 根因＋RAG：附相似事故、老師傅當時怎麼判斷與處置；LLM 摘要 | ⑦＋⑨ | ✅ 模擬資料 |
| 8 | learning loop：知識庫更新、門檻調整（建議 → 回測 → 核准） | ⑨（回饋入口屬 ⑧） | ✅ 模擬資料 |

- 8 步沒寫到、但一定要的是 ⑧ 出口：沒有它，第 6 步之後沒人看到，第 8 步也沒有回饋可學。
- 定案：不再加新的偵測機制，先把這一圈在真實環境跑起來。

整合驗證（`tests/loop_e2e.py`，docker 的 ES 9.5.5、模擬資料 `aiops-synth`：3 個服務 4 週歷史＋2 天，37 項全過）：

| 故事 | 結果 |
|---|---|
| 第 1 次 mes-api 變慢 → 一件＋報告 → 老師傅回饋 | `aiops-incidents` 先 `auto` 再 `confirmed`；每筆異常一筆回饋 |
| 第 2 次同樣症狀 | 報告最前面附第 1 件；LLM 摘要（template）每句附依據 |
| 第 3 次 → 同根因 3 次 | 附 2 件相似；`similar-eval` recall@3 = 3/3；提規則草稿 |
| 月底批次誤報 3 次 | 建議 z 5 → 7；沒回測不能核准；`backtest` 告警 3 → 1；核准後不再報 |
| 漏報 | 一筆降 z 的建議 |
| 量化（`metrics`） | 6 件、有回饋 100%、誤報率 50%、確認的事故 3 |

一件事從頭到尾（`loop_e2e` 跑出來的）：

```
10:00  mes-api 延遲 120ms → 480ms（4 個 5 分鐘）
10:01  detect：synth.latency z 45，第 1 點 warning、第 2 點 anomaly → aiops-anomalies
10:02  alert：開一件 a-20261005-0205-mes-api（D3120／it）
       → 根因分析（有 trace 時：慢在 wip-worker › Oracle SELECT lots）
       → 相似事故：知識庫空的，不附
       → aiops-alerts status open、summary「[warning] mes-api：synth.latency 526（平常 121.3）…」
       → aiops-incidents 一筆 status auto（帶指紋）
       → 心跳
10:02  Grafana：status:open 查到 → it-oncall 收到一則，附摘要、報告連結、回饋連結
10:10  老師傅點連結：真的、db、「Oracle 統計資訊過期」、處置「重跑 analyze」、
       怎麼看出來的「只有 lots 那張表的查詢慢，AWR 看到執行計畫變了」
       → aiops-feedback 3 筆、aiops-incidents 變 confirmed
10:35  30 分鐘沒新異常 → resolved → Grafana 發恢復
隔天   同樣症狀 → 新的一件，報告最前面：
       「跟 10-05 的事故相似（同服務；同樣的指標異常）；當時根因 [db] …；處置 …」
       「建議先照上次的方式確認：只有 lots 那張表的查詢慢… [I1]」
```

## Phase 1 收值

| 紀錄 | 結論 |
|---|---|
| [es-startup-401](pdca/phase1-collect/2026-10-es-startup-401.md) | ES 剛起來的 401 窗口約 1 秒；健康檢查改用 API key，掉 0 |
| [field-unify](pdca/phase1-collect/2026-10-field-unify.md) | 在 ingestion 統一：log 加 alias、Java 指標在 Collector 改名 |
| [ingest-keys-check](pdca/phase1-collect/2026-10-ingest-keys-check.md) | 鑰匙圈交叉查得到；純文字 log 要開 OTLP 才帶 trace_id |
| [ingest-simplify](pdca/phase1-collect/2026-10-ingest-simplify.md) | 集中式資料寫入憑證 (Shared Ingest Token)；`k8s-*` 開 failure store；錯誤率、p95 靜態臨界值規則 |
| [k3d-e2e](pdca/phase1-collect/2026-10-k3d-e2e.md) | k3d 上 initContainer、邊緣身份鑑別與標籤注入 (Edge Identity Sanitization & Injection)、OTLP log 帶 trace_id 都跑過 |
| [retention](pdca/phase1-collect/2026-10-retention.md) | 保留天數已套用；trace baseline 只讀隨機留下的 trace |
| [time-alignment](pdca/phase1-collect/2026-10-time-alignment.md) | 沒帶時區差 8 小時已修；時鐘偏差抓得到；`NTP_SERVER` 要填 |
| [trace-durability](pdca/phase1-collect/2026-10-trace-durability.md) | 不用 Kafka；兩段佇列改寫磁碟；當掉仍掉約 20% |

## Phase 2 異常分析

| 紀錄 | 結論 |
|---|---|
| [anomaly-owner](pdca/phase2-anomaly/2026-10-anomaly-owner.md) | 異常寫回時補 `dept`、`group`；對不到歸 unassigned |
| [chronic-calendar](pdca/phase2-anomaly/2026-10-chronic-calendar.md) | 慢性病清單、行事曆已合；清單沒送到負責人；沒排程 |
| [data-quality-check](pdca/phase2-anomaly/2026-10-data-quality-check.md) | 資料品質檢查已合；門檻是估的；沒排程 |
| [data-sources](pdca/phase2-anomaly/2026-10-data-sources.md) | 資料來源改接口＋假資料分開；負責人的真來源還沒有 |
| [exclude-adjust](pdca/phase2-anomaly/2026-10-exclude-adjust.md) | 排除時段、重置點、週期放寬已合；只放寬不變嚴；沒排程 |
| [instant-anomaly](pdca/phase2-anomaly/2026-10-instant-anomaly.md) | 偏太多（`instant_z` 15）單點即判定 (Instant Anomaly)；z 5.2 的誤報仍是 warning |
| [split-detector-cli](pdca/phase2-anomaly/2026-10-split-detector-cli.md) | `__main__.py` 1,100 行 → 160 行＋10 個檔，只搬不改 |

## Phase 3 定位問題

| 紀錄 | 結論 |
|---|---|
| [alert-exit](pdca/phase3-locate/2026-10-alert-exit.md) | 同服務合成一件寫 `aiops-alerts`；Grafana 12.2 依 dept 一件一則 |
| [alert-flap](pdca/phase3-locate/2026-10-alert-flap.md) | 假恢復來自每輪覆蓋；engine 寫 `@timestamp` 往前 1 分鐘 |
| [rca-demo](pdca/phase3-locate/2026-10-rca-demo.md) | K8s 上整條線跑通：3 分半開件、根因指到 Oracle、通知送到 |
| [schedule](pdca/phase3-locate/2026-10-schedule.md) | Dockerfile、CronJob、心跳規則完成；K8s 上還沒跑過 |

## Phase 4 RCA＋LLM

| 紀錄 | 結論 |
|---|---|
| [llm-summary](pdca/phase4-rca-llm/2026-10-llm-summary.md) | 每句附依據＋template 退路；捏造 id 整段不顯示；沒評測集 |
| [similar-incidents](pdca/phase4-rca-llm/2026-10-similar-incidents.md) | 指紋＋欄位比對、只檢索已確認；門檻權重是估的 |

## Phase 5 learning loop

| 紀錄 | 結論 |
|---|---|
| [check-feedback](pdca/phase5-learning-loop/2026-10-check-feedback.md) | 事故紀錄、值班回饋的指令與 index 已合 |
| [expert-feedback](pdca/phase5-learning-loop/2026-10-expert-feedback.md) | 回饋小網頁：預填、一鍵判斷、事故變 confirmed；沒登入 |
| [fix-critical](pdca/phase5-learning-loop/2026-10-fix-critical.md) | 四個會讓整輪失敗或蓋掉資料的問題已修，各有測試 |
| [fix-feedback-ux](pdca/phase5-learning-loop/2026-10-fix-feedback-ux.md) | 三個操作問題已修；真的 LLM 實跑，修後 6/6 每句附依據 |
| [grafana-form](pdca/phase5-learning-loop/2026-10-grafana-form.md) | 定案用 Grafana Business Forms 外掛；身分還是自己填 |
| [grafana-plugin-init](pdca/phase5-learning-loop/2026-10-grafana-plugin-init.md) | initContainer 從 Nexus 裝外掛、核對 SHA-256；k3d 跑過 |
| [loop-update](pdca/phase5-learning-loop/2026-10-loop-update.md) | 誤報 3 次 → 回測 → 核准 → 不再報實測過；放寬 z 是治標 |
| [metrics-kpi](pdca/phase5-learning-loop/2026-10-metrics-kpi.md) | `metrics` 指令學之前、學之後並排；真實數字等試點 |
| [rule-threshold](pdca/phase5-learning-loop/2026-10-rule-threshold.md) | 規則門檻自動算、核准即載入；第一階段只開 4 個 CronJob |

## 跨階段

| 紀錄 | 結論 |
|---|---|
| [docs-vs-code](pdca/2026-10-docs-vs-code.md) | 只改文件；6 處對不上改 5 處；code-review 4 項要修 |
| [restructure](pdca/2026-10-restructure.md) | 目錄改名（telemetry、engine、sdk、e2e），行為不變 |

## 還沒做、還不足的

會影響能不能用、能不能賣的：

1. **全部是模擬資料**：門檻（z 5、`instant_z` 15、相似分數 4、誤報 3 次、權重）全是估的，要 2～4 週真實資料。
2. **K8s 排程沒跑過**：rca-demo 在 K8s 上用 `loop` 跑整條線，engine 的 CronJob 還沒跑過。
3. **沒有評測集**：真的 LLM 跑過（OpenAI 12 次、rca-demo 一次），相似事故在沒有 trace 時只靠「同服務＋同指標」剛好壓在門檻上。
4. **回饋沒有身分**：回饋表單、小網頁都沒登入；回饋 3 次可以是同一個人（沒有第二人確認）。
5. **漏報沒有網頁入口**，仍靠 `feedback` 指令。
6. 月底批次只會提「放寬 z」，不會提排除時段。
7. 舊事故單匯入 `aiops-incidents` 的格式沒寫。
8. code-review 剩三項沒動：metrics 回饋跨段、回測重複查 ES、相似事故 ES 前 20 筆截斷。

各模組還沒做的：

| 模組 | 還沒做 |
|---|---|
| ① 收值 | .NET SQL Server、Serilog／NLog 驗證 |
| ③ 存 | 副本、正式環境容量 |
| ④ 判斷 | p99、慢請求比例、往外呼叫耗時；趨勢；`groups` 依公司命名；移動式／變化點偵測當輔助（新服務前幾天、跳一階後停在新位置） |
| ⑥ 收斂 | 沿呼叫關係合併、排優先；一段記成一筆；靜音、限頻 |
| ⑦ 串接 | 單一 Pod 異常 → 拿它的 trace 逐段比；撈 log 帶 `span.id` |
| ⑨ feedback | 版本、評測集、第二人確認、排除時段的建議 |

## 哪裡過度設計

以「維運可靠度、AIOps 基本盤」來看：

| 東西 | 為什麼算過度 | 建議 |
|---|---|---|
| 動態基準線三個自動機制同時存在：自選粒度、卜瓦松、借同組 | 沒有真實資料就先做三種退路，設定、文件、測試都變厚 | 留著但不再擴；真實資料跑過再決定砍哪個 |
| trace 每一段的 baseline＋N+1、例外吞噬檢測規則 | trace 有取樣、只留 7 天，學出來的「平常」本來就偏 | 先只跑根因分析那條；trace baseline 等真資料 |
| 回饋表單 10 個「查了哪裡」、6 種誤報原因、9 類根因 | 選項是估的，沒人填過 | 第一週看真的填了什麼再縮 |
| `metrics` 的「最吵的服務」、依 dept | Grafana 讀 `aiops-alerts` 畫更合適 | 留表格，不再加欄 |

**不是過度、一定要的**：Collector 的邊緣身份鑑別與標籤注入、分部門，可輪替的集中式資料寫入憑證、failure store、靜態臨界值規則的保底門檻、連續才算、心跳、「已確認不覆蓋」、每句附依據的檢查、沒回測不能核准。這些是可靠度，不是功能。

## 想確認什麼、跑哪個

| 想確認什麼 | 跑哪個 | 需要 |
|---|---|---|
| 程式邏輯對（不連 ES） | `cd engine && python3 -m pytest -q`（318 個） | 無 |
| 收值線通（Collector → ES → 分部門） | `e2e/local-k3s/run-e2e.sh` | k3s |
| K8s 上的標籤注入、OTLP log、Grafana initContainer | [e2e/local-k3d/](../e2e/local-k3d/README.md) | k3d |
| 根因分析找到那一段、那行 log | `e2e/local-k3s/run-rca-e2e.sh` | k3s，約 14 分鐘 |
| 整條線在 K8s 上：送資料 → 規則偵測 → 一件 → 根因＋LLM → Grafana | `e2e/local-k3s/rca-demo/run-rca-demo.sh`（up → loop → incident）；約 15 分鐘看到根因，恢復通知再等 30 分鐘 | k3s、`run-e2e.sh` 跑過；真的 LLM 要 `LLM_ENV_FILE` |
| 動態基準線抓得到造的異常、誤報多少 | `cd engine && python3 -m tests.synth_e2e` | 一個 ES |
| 整個 learning loop 的故事 | `cd engine && python3 -m tests.loop_e2e` | 一個 ES，約 2 分鐘 |
| Grafana 一件一則、依 dept | 照 [configs/grafana/README.md](../engine/configs/grafana/README.md) | Grafana 12 |
| 上 K8s 排程 | `kubectl apply -k engine/deploy/` | 還沒跑過 |

## 下一步

1. ④⑥⑧ 上 K8s 排程，試點產線先只寫入、不通知 2～4 週。
2. ④ 用真實資料校正門檻。
3. ⑦⑨ 用有 trace 的事故驗相似事故與 LLM；建評測集（20～30 件過去事故）。
4. ⑥ 沿呼叫關係合併、排優先。

**底部說明**：目前以 PoC 驗證端到端可行性；重點不是完成比例，而是確認每個模組所需的資料、分析方法與輸出結果，並以回饋閉環讓結果持續變準。
