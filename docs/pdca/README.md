# PDCA：現況與待辦、每一輪紀錄

整個專案的「做到哪、還要做什麼」放這一份。各資料夾 README 開頭有自己那一塊的細節，這裡只列項目和去哪看。

PDCA 的意思：**Plan** 定規則與門檻 → **Do** 收值、判斷、報告 → **Check** 值班判斷對錯、量測 → **Act** 依結果調整。目前 Do 最完整，**Check 是最大缺口**，沒補上就形成不了閉環（[feedback/README.md](../feedback/README.md#用-pdca-看整條線)）。

**編號只有一套：①～⑨ 資料模組**（① 收值、② 清資料、③ 存、④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback；定義見 [architecture.md](../architecture.md)）。每一項前面的數字是它屬於哪個模組；每一份紀錄開頭的「模組」也是。

狀態：✅ 已做、在 k3s 驗過 ／ ⚠️ 做了一部分 ／ 📝 設計好、還沒做 ／ ❌ 沒做、也還沒設計。

## Plan：定規則、定門檻

| 項目 | 狀態 | 細節 |
|---|---|---|
| ④ 判斷設定：序列、粒度、z、`min_abs`、規則門檻 | ✅ | [engine/configs/baseline](../../engine/configs/baseline/README.md)（`series.yaml`、`rules.yaml`、`peer.yaml`、`trace.yaml`） |
| ⑨ 上線前記下基準（每天告警幾筆、誤報多少、從告警到回饋多久） | ⚠️ `metrics` 指令有了，數字要等試點 | [phase5-learning-loop/2026-10-metrics-kpi.md](phase5-learning-loop/2026-10-metrics-kpi.md) |
| ④ 用真實資料校正門檻 | 📝 要先排程跑 2～4 週 | [rca/](../rca/README.md) |
| ② 歸屬放哪裡（讀 namespace 的 `dept`，或改成 app → group 中央清單）、app 名稱是否全公司唯一 | 未定案 | [README「還沒定案的」](../../README.md#還沒定案的) |
| ③ 保留天數與容量（用正式環境的量重算） | ⚠️ 已合進 master；天數是暫定值，要用正式環境的量重算 | [retention.md](../retention.md) |
| ②④ 服務命名規則（`groups`）、交易 ID 命名、寫入 key 怎麼自動進新 cluster | 未定案，要別人配合 | 各資料夾 |

## Do：收值、判斷、報告

| 項目 | 狀態 | 細節 |
|---|---|---|
| ①②③ 收值（log、metrics、trace、尾端取樣）、清資料、存 | ✅ | [log](../log/README.md)、[metrics](../metrics/README.md)、[trace](../trace/README.md) |
| ④ 判斷：規則、跟歷史比、跟兄弟比、trace 每一段 | ✅ | [engine/configs/baseline](../../engine/configs/baseline/README.md) |
| ⑦ 根因分析：metrics → trace → log | ✅ | [engine/configs/rca](../../engine/configs/rca/README.md) |
| ④ **正式環境排程**（train 每天、detect 每 30 秒、rules 與 peer 每分鐘） | ⚠️ 程式好了，沒部署 | [rca/](../rca/README.md) |
| 已完成部分的小尾巴：② 統一指標名稱（metrics）、② 統一欄位路徑（log）（① trace-sampler 磁碟佇列已做） | ⚠️ | 各資料夾 README 開頭 |
| ①②④ 時間對齊：主機時鐘監控（metrics）、收資料延遲與時區（log）、時鐘差日報（trace） | 📝 | 同上 |
| ①② 業務 ID：交易 ID 命名、從 log 抽出；Kafka 驗證 Java | 📝 | [trace/](../trace/README.md#業務-id服務層) |
| ⑦ 根因分析改看一批慢 trace 找共同瓶頸（不用 exemplar，ES 不存） | 📝 | [rca/「為什麼不用 exemplar」](../rca/README.md#為什麼不用-exemplar用服務路由時間撈一批) |
| ④ baseline 補強：p99、慢請求比例、往外呼叫耗時、變化點、Pod → trace 逐段比、趨勢 | 📝 | [rca/](../rca/README.md) |
| ⑥ 收斂：(1) 連續的點合成一段、(2) 同服務合成一件、(3) 沿呼叫關係找源頭 | 📝 | [rca/「雜訊壓制」](../rca/README.md#雜訊壓制) |
| ⑦ 常駐拓撲、交易時間線工具、根因報告帶業務 ID | 📝 | [rca/](../rca/README.md) |

## Check：看對不對

| 項目 | 狀態 | 細節 |
|---|---|---|
| ⑧ 告警出口（通知） | ✅ 合成一件寫 `aiops-alerts`，Grafana 分送；排程 yaml 有了、K8s 沒跑過 | [phase3-locate/2026-10-alert-exit.md](phase3-locate/2026-10-alert-exit.md)、[phase3-locate/2026-10-schedule.md](phase3-locate/2026-10-schedule.md) |
| ⑧ 事故紀錄 `aiops-incidents`（`incident` 指令、alert 自動開 `auto`、回饋網頁確認） | ✅ 接上告警與回饋；存根因分類、怎麼看出來的、指紋 | [phase5-learning-loop/2026-10-expert-feedback.md](phase5-learning-loop/2026-10-expert-feedback.md) |
| ⑨ 值班回饋 `aiops-feedback`（回饋網頁＋`feedback` 指令） | ✅ 網頁入口（沒登入）；漏報仍靠指令 | 同上 |
| ② 資料品質檢查：路由沒參數化、必填欄位缺、資料停了、落到 unassigned 變多（`quality`） | ⚠️ 程式好了、實測過，沒排程 | [baseline README](../../engine/configs/baseline/README.md#資料品質檢查資料本身怪不怪) |
| ② 排除已知干擾：事故、維護、壓測時段不學（`exclude_window`）；換版重新學（`reset_point`）；月底批次放寬（`recurring_override`） | ⚠️ 程式好了、實測過，沒排程 | [baseline README「調整」](../../engine/configs/baseline/README.md#調整哪些資料不學從哪裡重新學哪些時段放寬) |
| ② 慢性病清單（`chronic`，每週）、行事曆（`calendar.yaml`） | ⚠️ 程式好了、實測過，沒排程；假日已改成直接抓台灣行事曆（`taiwan` provider）；`owners` 目前是**假資料**（`fake-data/owners.json`），要換成真的來源 | [baseline README「慢性病清單、行事曆」](../../engine/configs/baseline/README.md#慢性病清單行事曆) |
| ⑨ 回測（`backtest`：改門檻前重跑過去 N 週） | ✅ 沒回測不能核准 | [phase5-learning-loop/2026-10-loop-update.md](phase5-learning-loop/2026-10-loop-update.md) |
| ⑨ 版本（每筆異常記哪一版產生）、評測集（20～30 件過去事故） | ❌ 要真實事故 | [feedback/](../feedback/README.md) |
| ①② REST API 的 log 樣本、重要 .NET Kafka 消費者加幾行 | 要別人配合 | 各資料夾 |

## Act：依結果調整

| 項目 | 狀態 | 細節 |
|---|---|---|
| ④ 手動調整：`series.yaml` 的 `overrides`、`tune` 指令（記原因、誰、何時） | ✅ | [baseline README「使用者、feedback 調整」](../../engine/configs/baseline/README.md#使用者feedback-調整) |
| ② 修慢性病（天天噴錯、沒人修的） | 要別人配合 | [rca/](../rca/README.md#髒資料排除) |
| ⑨ 同一個根因 ≥ 3 次 → 升級成規則 | ✅ 門檻算好、核准後 `rules` 自動載入；只處理延遲類、沒有過期 | [phase5-learning-loop/2026-10-rule-threshold.md](phase5-learning-loop/2026-10-rule-threshold.md) |
| ⑨ 回饋累積夠且回測通過 → 調門檻、排除時段 | ⚠️ 誤報 3 次 → 放寬 z → 回測 → 人核准；還不會提排除時段 | 同上 |
| ② 資料品質檢查的門檻：先估的，正式環境只記錄跑 2～4 週再手動校；`quality` 還不能用 `tune` 調 | 📝 | [baseline README「門檻怎麼校、誰來調」](../../engine/configs/baseline/README.md#門檻怎麼校誰來調) |
| ⑦⑨ 相似事故檢索（RAG 的檢索，欄位比對） | ✅ 模擬資料驗過；門檻、權重是估的 | [phase4-rca-llm/2026-10-similar-incidents.md](phase4-rca-llm/2026-10-similar-incidents.md) |
| ⑦⑨ LLM：根因報告摘要（只加註、每句附依據） | ✅ template 與真的模型（OpenAI）都跑過，每句附依據；評測集 ❌ | [phase4-rca-llm/2026-10-llm-summary.md](phase4-rca-llm/2026-10-llm-summary.md)、[phase5-learning-loop/2026-10-fix-feedback-ux.md](phase5-learning-loop/2026-10-fix-feedback-ux.md) |

**之後**：Elastic 付費 AIOps 功能評估（試用只能開一次，等資料累積夠再開）；工廠（[factory-later.md](../factory-later.md)）。

**建議順序**：正式排程（只寫入不通知）→ 收斂(1)(2)＋告警出口＋事故紀錄、回饋 → 用 2～4 週真實資料校正門檻 → LLM 摘要 → 回測、版本 → 收斂(3)、自動調門檻。

## 每一輪的紀錄

做完一件值得記的事（一個需求、一次改動、一次實測、一個決定），照 [TEMPLATE.md](TEMPLATE.md) 寫一份，檔名 `YYYY-MM-主題.md`，放進對應 phase 的資料夾，並在那個 phase 的表加一行。**上面四張表是「現在還剩什麼」，這裡是「做過什麼、為什麼這樣決定」**，兩邊互相連結，不要抄一份。

四段各寫什麼（只寫結論，不寫調查過程）：

| 段 | 寫什麼 |
|---|---|
| **Plan** | 這一次要達到什麼：需求（誰、什麼情況、什麼問題）、目的（做完變成什麼樣）、範圍（做什麼、不做什麼） |
| **Do** | 改動哪些 code、改了什麼、**為何要改**（每個檔案一列）；沒改 code 就寫「無 code 改動」並說明做了什麼 |
| **Check** | 測試的**目的**（為了確認什麼）、怎麼測、**結果**（觀察到什麼）。不寫「N 個通過」 |
| **Act** | Check 後**沒改善的、還沒解決的**，每項寫**在什麼情況下發現的**、影響、後續 |

排錯的結論不寫在這裡，集中在 [troubleshooting.md](../troubleshooting.md)。

### Phase 1 收值（`phase1-collect/`）

metrics、trace、log 怎麼收、怎麼存、三種怎麼關聯（欄位、時間、key）

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase1-collect/2026-10-durability.md](phase1-collect/2026-10-durability.md) | ① 收值線在各種故障下會不會掉、會不會重複（k3s 實測 12 個情境） | 修了兩個：node 端 trace 佇列共用一個檔（多台 trace-sampler 時只有一台收得到）、batch processor 當掉掉 1 秒；修正後 Collector 換版、ES 停 30 分鐘、ES 回 429 都掉 0，kill -9 最多掉 0.2 秒；trace-sampler 當掉掉「decision_wait＋1 秒」那台的、PVC 滿了掉、刪 hostPath 掉停機那段 log |
| [phase1-collect/2026-10-es-startup-401.md](phase1-collect/2026-10-es-startup-401.md) | ① ES 剛起來回 401，Collector 直接丟 | 401 窗口約 1 秒（.security 還沒恢復）；修在 ES 入口：健康檢查用 API key 打 GET /（實測掉 0）；兩台 ES 滾動重啟也有窗口（投票名單只有一台）、新檢查都掉 0；不加 Kafka、不改 Collector |
| [phase1-collect/2026-10-field-unify.md](phase1-collect/2026-10-field-unify.md) | ② 欄位統一：log 身分欄位補 OTel 路徑、Java HTTP 指標在 Collector 改名；metrics ↔ trace 怎麼連 | 在 ingestion 處理、不在查詢層：log 加 alias、Java 指標在 Collector 改名；`variants` 拿掉；`quality.metric_name_legacy` 盯舊名字 |
| [phase1-collect/2026-10-ingest-keys-check.md](phase1-collect/2026-10-ingest-keys-check.md) | ①②③ 收值、共同鑰匙圈、log↔trace 綁定、metrics 時間窗：docker 重驗 | 鑰匙圈交叉查得到；log↔trace 要 log 格式有印 trace_id，純文字的要開 `otel.logs.exporter: otlp`（新加的路，pipeline 原本會丟 trace_id，已修；真的 Java agent、.NET 自動埋點各驗一次）；metrics 三層窗對得上（276／分、13%、p95 3.75 秒）；.NET Framework：trace／metrics 可、ILogger 直送不行 |
| [phase1-collect/2026-10-ingest-simplify.md](phase1-collect/2026-10-ingest-simplify.md) | ①③④ 一把寫入 key、ES failure store、靜態保底規則 | 一把寬 pattern 的 key、一個出口，資料照樣依 group 分；log 的 `k8s-*` 開 failure store（OTel 的預設已開），`quality.ingest_rejected` 有一筆就報；rules 加 `window` 類型，錯誤率 ≥ 5%、p95 ≥ 2 秒保底 |
| [phase1-collect/2026-10-k3d-e2e.md](phase1-collect/2026-10-k3d-e2e.md) | ①②③⑧ k3d 上的 K8s 實測：Grafana initContainer、Collector 依 Pod 補身分、OTLP log、chart | 都跑過：initContainer 裝得起來、SHA 錯停 Init:Error 且舊 Pod 繼續服務；K8s 上補身分對；OTLP log 帶 trace_id 對得到 trace；確認開 otlp 有兩份 log；k3d 在這個環境要特別處理（腳本在 e2e/local-k3d） |
| [phase1-collect/2026-10-load-test.md](phase1-collect/2026-10-load-test.md) | ①③ 收值壓測：加量、突發、大行、50 Pod、高基數、千段 trace、ES 慢／拒收 | 正式設定下都掉 0：log 10,000 行、metrics 50,000 點、trace 3,000 span／秒內延遲 < 10 秒，再大只是晚到；佇列縮小時 trace-sampler 丟 45%；log 寫入 index 大過 ES 記憶體會停住（要定 ILM 大小） |
| [phase1-collect/2026-10-retention.md](phase1-collect/2026-10-retention.md) | 保留天數（metrics 降解析度、log 統計、trace 分兩種） | 已合；trace baseline 改成只讀隨機留下的 `traces-generic`（沒用真實資料驗證） |
| [phase1-collect/2026-10-time-alignment.md](phase1-collect/2026-10-time-alignment.md) | ② 時間對齊：log 沒帶時區、收資料延遲、主機時鐘 | 沒帶時區的原本差 8 小時、空白分隔的默默換時間，已修；延遲與時鐘偏差、主機時鐘都抓得到；`NTP_SERVER` 要填 |
| [phase1-collect/2026-10-trace-durability.md](phase1-collect/2026-10-trace-durability.md) | ① trace 收值線要不要 message queue | 不用 Kafka；兩段佇列改寫磁碟；node 端放子 exporter 的做法後來發現有錯、當掉掉多少重新量過（[durability](phase1-collect/2026-10-durability.md)） |

### Phase 2 異常分析（`phase2-anomaly/`）

baseline（中位數＋MAD）、規則、資料品質、餵給 baseline 前的清資料與排除

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase2-anomaly/2026-10-anomaly-owner.md](phase2-anomaly/2026-10-anomaly-owner.md) | ⑤ 異常總表帶 `dept`、`group` | 資料本來就帶，異常寫回時補；namespace 優先、其次服務、對不到 unassigned；告警依 group 分送的前置 |
| [phase2-anomaly/2026-10-chronic-calendar.md](phase2-anomaly/2026-10-chronic-calendar.md) | 慢性病清單與行事曆 | 已合；清單沒送到負責人手上；假日預設連當天故障也不報 |
| [phase2-anomaly/2026-10-data-quality-check.md](phase2-anomaly/2026-10-data-quality-check.md) | 資料品質檢查（路由沒參數化、欄位缺、資料停了、unassigned 變多） | 已合；第一天只靠昨天比的檢查不會報；門檻是估的 |
| [phase2-anomaly/2026-10-data-sources.md](phase2-anomaly/2026-10-data-sources.md) | 資料來源接口＋假資料分開；抓台灣行事曆 | 已合；行事曆來源非官方；負責人真來源還沒有 |
| [phase2-anomaly/2026-10-exclude-adjust.md](phase2-anomaly/2026-10-exclude-adjust.md) | 排除已知干擾（排除時段、重置點、週期放寬） | 已合；重置後暖機 24 小時內不判斷（估的）；只放寬不變嚴 |
| [phase2-anomaly/2026-10-instant-anomaly.md](phase2-anomaly/2026-10-instant-anomaly.md) | ④ 單次就報：依序列設 `confirm_points`、偏太多（`instant_z`）不用等；`tune` 能調 | `http.error_rate` 一次就算、全域 `instant_z: 15`；模擬資料實測 z 20～49 的單點變 anomaly、z 5.2 的誤報仍是 warning |
| [phase2-anomaly/2026-10-split-detector-cli.md](phase2-anomaly/2026-10-split-detector-cli.md) | ④⑤⑨ detector 的 `__main__.py`（1,100 行）依指令拆檔 | 1,100 行 → `__main__.py` 160 行＋ 10 個檔；只搬不改，200 個測試、ruff、9 個指令實跑都過 |

### Phase 3 定位問題（`phase3-locate/`）

metrics → trace → log 找到哪一段、哪行 log；事件整合、排程、通知出口

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase3-locate/2026-10-alert-exit.md](phase3-locate/2026-10-alert-exit.md) | ⑥ 事件整合，通知交給 Grafana | 同服務合成一件寫 `aiops-alerts`（帶 dept、摘要、根因報告）；Grafana 12.2 實測一件一則、依 dept 分送、資料品質送平台、結束發恢復；ES 9 要寫 `orderBy: _count` |
| [phase3-locate/2026-10-alert-flap.md](phase3-locate/2026-10-alert-flap.md) | ⑥⑧ 件還開著，Grafana 卻發恢復通知 | 一件只有一筆、每輪覆蓋；覆蓋落在 Grafana 排定評估與送出查詢之間時查到 0 筆、當成恢復；engine 寫 `@timestamp` 往前 1 分鐘 |
| [phase3-locate/2026-10-rca-demo.md](phase3-locate/2026-10-rca-demo.md) | ①④⑥⑦⑧ 整條線在 K8s 上跑：Pod 連續送 metrics、trace、log → 規則真的偵測 → 一件 → 根因＋LLM → Grafana | Rancher Desktop 實測：事故 3 分 40 秒後開成一件、根因指到 Oracle 那段與那行 log、LLM 摘要帶依據、`/it` 收到通知、儀表板六格都有資料；抓到 Grafana 每分鐘重發（summary 是 label）→ summary 不放會變的字；metrics→trace 是「服務＋API＋時間」的統計對應、不是 trace_id |
| [phase3-locate/2026-10-schedule.md](phase3-locate/2026-10-schedule.md) | ④⑤⑥⑧ 排程上線：CronJob、「AIOps 停了」告警、先只寫入不通知 | Dockerfile、10 個 CronJob、回饋網頁 Deployment、心跳＋Grafana 規則；image 建好跑過，K8s 沒跑過 |

### Phase 4 RCA＋LLM＋graph RAG（`phase4-rca-llm/`）

相似事故檢索、LLM 摘要；graph RAG 構想在 [rca/graph-rag.md](../rca/graph-rag.md)

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase4-rca-llm/2026-10-llm-summary.md](phase4-rca-llm/2026-10-llm-summary.md) | ⑦⑨ LLM 摘要（RAG 的生成） | 輸入包＋每句附依據的檢查＋template 退路；捏造 id 整段不顯示實測過；真的模型（閘道 401）沒跑過、沒評測集 |
| [phase4-rca-llm/2026-10-similar-incidents.md](phase4-rca-llm/2026-10-similar-incidents.md) | ⑦⑨ 相似事故檢索（RAG 的檢索） | 指紋＋欄位比對、自己算分寫出為什麼像、只檢索已確認；第 2 件附上第 1 件實測過；門檻權重是估的、模擬沒 trace、舊事故匯入格式未寫 |

### Phase 5 learning loop＋動態門檻（`phase5-learning-loop/`）

值班回饋、事故紀錄、回測、門檻調整、量化

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase5-learning-loop/2026-10-check-feedback.md](phase5-learning-loop/2026-10-check-feedback.md) | Check：事故紀錄與值班回饋 | 已合；沒有告警出口、回測；回饋靠指令 |
| [phase5-learning-loop/2026-10-expert-feedback.md](phase5-learning-loop/2026-10-expert-feedback.md) | ⑧⑨ 老師傅的回饋入口 | 小網頁（`feedback_web`）：預填告警、一鍵判斷、存根因分類、處置、怎麼看出來的、查了哪裡；每筆異常一筆回饋、事故變 confirmed；沒登入、漏報沒入口 |
| [phase5-learning-loop/2026-10-fix-critical.md](phase5-learning-loop/2026-10-fix-critical.md) | ⑥⑧⑨ 修四個會讓整輪失敗或蓋掉資料的問題：LLM 不擋告警、回測時區、事故不被蓋（scripted upsert）、alert 時限 | 各補測試、修前重現過；docker ES 實測確認過的事故不被蓋；LLM 時限仍是估的 |
| [phase5-learning-loop/2026-10-fix-feedback-ux.md](phase5-learning-loop/2026-10-fix-feedback-ux.md) | ⑨⑦ 漏報建議被吃掉、網頁送出失敗沒回應、摘要變空白；**真的 LLM 第一次跑** | 三個修了各有測試；OpenAI 實跑 12 次：預算要 2000、告警本身要有 id 才有依據可引，修後 6/6 通過、5～8 秒一件 |
| [phase5-learning-loop/2026-10-grafana-form.md](phase5-learning-loop/2026-10-grafana-form.md) | ⑧⑨ 回饋表單放進 Grafana（Business Forms 外掛） | **定案用這個**（外掛放資料夾、不換 image，小網頁當備案）；Grafana 12.2 實測：預填、送出寫回 ES、誤報路徑都對；12.2 只能用外掛 6.3.1；回饋服務網址寫死、要開 CORS；身分還是自己填 |
| [phase5-learning-loop/2026-10-grafana-plugin-init.md](phase5-learning-loop/2026-10-grafana-plugin-init.md) | ⑧⑨ Grafana 在 K8s 上用 initContainer 裝回饋表單外掛 | 從 Nexus 下載、核對 SHA-256、解到 emptyDir；docker 模擬驗過（裝、跳過、SHA 不符不裝、簽章 valid）；之後在 k3d 上跑過（[k3d-e2e](phase1-collect/2026-10-k3d-e2e.md)） |
| [phase5-learning-loop/2026-10-loop-update.md](phase5-learning-loop/2026-10-loop-update.md) | ⑨④ 回饋回到系統：更新知識庫、門檻加回測 | `backtest`（現行 vs 建議重跑）、沒回測不能核准、漏報降 z、同根因 3 次規則草稿；誤報 3 次 → 回測 → 核准 → 不再報實測過；月底批次放寬 z 是治標 |
| [phase5-learning-loop/2026-10-metrics-kpi.md](phase5-learning-loop/2026-10-metrics-kpi.md) | ⑨ 量化：學之前、學之後的數字 | `metrics` 指令兩段並排；模擬資料驗算法，真實數字等試點 |
| [phase5-learning-loop/2026-10-rule-threshold.md](phase5-learning-loop/2026-10-rule-threshold.md) | ⑨④②⑧ 規則草稿把門檻算出來、核准就自動載入；行事曆拿掉國定假日、歲修一筆一筆；第一階段只開 4 個 CronJob | 門檻 = 各件最低異常值的最小值（峰值實測不觸發）；核准後 `rules` 載入、第 2 次事故時間觸發；沒有過期、只處理延遲類 |

### 跨階段（這個資料夾）

整條線的盤點、文件對程式、目錄改名

| 檔案 | 主題 | 結論 |
|---|---|---|
| [2026-10-docs-vs-code.md](2026-10-docs-vs-code.md) | ①～⑨ 文件對程式：整條線一份看完（[end-to-end-flow.md](../end-to-end-flow.md)）、總覽狀態對齊 | 只改文件；6 處對不上改掉 5 處；code-review 的 10 項對過程式，4 項要修、記在流程文件的「不足」 |
| [2026-10-restructure.md](2026-10-restructure.md) | 全部 目錄改名：專案名 it-aiops | `platform/`→`telemetry/`、`aiops/`→`engine/`、`apps/`→`sdk/`、`test/`→`e2e/`；Python 套件改名；行為不變，200 個單元測試與連結檢查過 |
