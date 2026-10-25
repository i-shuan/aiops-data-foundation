# PDCA：現況與待辦、每一輪紀錄

整個專案的「做到哪、還要做什麼」放這一份。各資料夾 README 開頭有自己那一塊的細節，這裡只列項目和去哪看。

PDCA 的意思：**Plan** 定規則與門檻 → **Do** 收值、判斷、報告 → **Check** 值班判斷對錯、量測 → **Act** 依結果調整。目前 Do 最完整，**Check 是最大缺口**，沒補上就形成不了閉環（[feedback/README.md](../feedback/README.md#用-pdca-看整條線)）。

**編號只有一套：①～⑨ 資料模組**（① 收值、② 清資料、③ 存、④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback；定義見 [architecture.md](../architecture.md)）。每一項前面的數字是它屬於哪個模組；每一份紀錄開頭的「模組」也是。

狀態：✅ 已做、在 k3s 驗過 ／ ⚠️ 做了一部分 ／ 📝 設計好、還沒做 ／ ❌ 沒做、也還沒設計。

## Plan：定規則、定門檻

| 項目 | 狀態 | 細節 |
|---|---|---|
| ④ 判斷設定：序列、粒度、z、`min_abs`、規則門檻 | ✅ | [aiops/baseline](../../aiops/baseline/README.md)（`series.yaml`、`rules.yaml`、`peer.yaml`、`trace.yaml`） |
| ⑨ 上線前記下基準（每天告警幾筆、誤報多少、從告警到找到原因多久） | ❌ | [feedback/](../feedback/README.md) |
| ④ 用真實資料校正門檻 | 📝 要先排程跑 2～4 週 | [rca/](../rca/README.md) |
| ② 歸屬放哪裡（讀 namespace 的 `dept`，或改成 app → group 中央清單）、app 名稱是否全公司唯一 | 未定案 | [README「還沒定案的」](../../README.md#還沒定案的) |
| ③ 保留天數與容量（用正式環境的量重算） | ⚠️ 已合進 master；天數是暫定值，要用正式環境的量重算 | [retention.md](../retention.md) |
| ②④ 服務命名規則（`groups`）、交易 ID 命名、寫入 key 怎麼自動進新 cluster | 未定案，要別人配合 | 各資料夾 |

## Do：收值、判斷、報告

| 項目 | 狀態 | 細節 |
|---|---|---|
| ①②③ 收值（log、metrics、trace、尾端取樣）、清資料、存 | ✅ | [log](../log/README.md)、[metrics](../metrics/README.md)、[trace](../trace/README.md) |
| ④ 判斷：規則、跟歷史比、跟兄弟比、trace 每一段 | ✅ | [aiops/baseline](../../aiops/baseline/README.md) |
| ⑦ 根因分析：metrics → trace → log | ✅ | [aiops/rca](../../aiops/rca/README.md) |
| ④ **正式環境排程**（train 每天、detect 每 30 秒、rules 與 peer 每分鐘） | ⚠️ 程式好了，沒部署 | [rca/](../rca/README.md) |
| 已完成部分的小尾巴：② 統一指標名稱（metrics）、② 統一欄位路徑（log）（① trace-sampler 磁碟佇列已做） | ⚠️ | 各資料夾 README 開頭 |
| ①②④ 時間對齊：主機時鐘監控（metrics）、收資料延遲與時區（log）、時鐘差日報（trace） | 📝 | 同上 |
| ①② 業務 ID：交易 ID 命名、從 log 抽出；Kafka 驗證 Java | 📝 | [trace/](../trace/README.md#業務-id服務層) |
| ⑦ 根因分析改看一批慢 trace 找共同瓶頸（不用 exemplar，ES 不存） | 📝 | [feedback/](2026-10-metrics-to-trace.md) |
| ④ baseline 補強：p99、慢請求比例、往外呼叫耗時、變化點、Pod → trace 逐段比、趨勢 | 📝 | [rca/](../rca/README.md) |
| ⑥ 收斂：(1) 連續的點合成一段、(2) 同服務合成一件、(3) 沿呼叫關係找源頭 | 📝 | [rca/「雜訊壓制」](../rca/README.md#雜訊壓制) |
| ⑦ 常駐拓撲、交易時間線工具、根因報告帶業務 ID | 📝 | [rca/](../rca/README.md) |

## Check：看對不對

| 項目 | 狀態 | 細節 |
|---|---|---|
| ⑧ 告警出口（通知） | ❌ 現在只寫進 ES | [feedback/](../feedback/README.md) |
| ⑧ 事故紀錄 `aiops-incidents`（`incident` 指令） | ⚠️ 程式好了、實測過；沒排程、沒接告警 | [baseline README「事故紀錄與值班回饋」](../../aiops/baseline/README.md#事故紀錄與值班回饋) |
| ⑨ 值班回饋 `aiops-feedback`（`feedback` 指令：真的／誤報／根因錯／漏報；誤報率統計） | ⚠️ 程式好了、實測過；收集靠指令，不自動生效 | 同上 |
| ② 資料品質檢查：路由沒參數化、必填欄位缺、資料停了、落到 unassigned 變多（`quality`） | ⚠️ 程式好了、實測過，沒排程 | [baseline README](../../aiops/baseline/README.md#資料品質檢查資料本身怪不怪) |
| ② 排除已知干擾：事故、維護、壓測時段不學（`exclude_window`）；換版重新學（`reset_point`）；月底批次放寬（`recurring_override`） | ⚠️ 程式好了、實測過，沒排程 | [baseline README「調整」](../../aiops/baseline/README.md#調整哪些資料不學從哪裡重新學哪些時段放寬) |
| ② 慢性病清單（`chronic`，每週）、行事曆（`calendar.yaml`） | ⚠️ 程式好了、實測過，沒排程；假日已改成直接抓台灣行事曆（`taiwan` provider）；`owners` 目前是**假資料**（`fake-data/owners.json`），要換成真的來源 | [baseline README「慢性病清單、行事曆」](../../aiops/baseline/README.md#慢性病清單行事曆) |
| ⑨ 版本（每筆異常記哪一版產生）、回測、評測集（20～30 件過去事故） | ❌ | [feedback/](../feedback/README.md) |
| ①② REST API 的 log 樣本、重要 .NET Kafka 消費者加幾行 | 要別人配合 | 各資料夾 |

## Act：依結果調整

| 項目 | 狀態 | 細節 |
|---|---|---|
| ④ 手動調整：`series.yaml` 的 `overrides`、`tune` 指令（記原因、誰、何時） | ✅ | [baseline README「使用者、feedback 調整」](../../aiops/baseline/README.md#使用者feedback-調整) |
| ② 修慢性病（天天噴錯、沒人修的） | 要別人配合 | [rca/](../rca/README.md#髒資料排除) |
| ⑨ 同一個根因 ≥ 3 次 → 升級成規則 | 📝 | [feedback/](../feedback/README.md) |
| ⑨ 回饋累積夠且回測通過 → 自動調門檻、排除時段 | 📝 | 同上 |
| ② 資料品質檢查的門檻：先估的，正式環境只記錄跑 2～4 週再手動校；`quality` 還不能用 `tune` 調 | 📝 | [2026-10-data-cleaning.md](2026-10-data-cleaning.md#門檻怎麼校之後誰來調) |
| ⑨ LLM：根因報告摘要（只加註、不擋告警）、RAG 讀確認過的事故 | 📝 | [feedback/llm-mlops.md](../feedback/llm-mlops.md) |

**之後**：Elastic 付費 AIOps 功能評估（試用只能開一次，等資料累積夠再開）；工廠（[factory-later.md](../factory-later.md)）。

**建議順序**：正式排程（只寫入不通知）→ 收斂(1)(2)＋告警出口＋事故紀錄、回饋 → 用 2～4 週真實資料校正門檻 → LLM 摘要 → 回測、版本 → 收斂(3)、自動調門檻。

## 每一輪的紀錄

做完一件值得記的事（一個需求、一次改動、一次實測、一個決定），照 [TEMPLATE.md](TEMPLATE.md) 寫一份，檔名 `YYYY-MM-主題.md`，放在這個資料夾，並在下表加一行。**上面四張表是「現在還剩什麼」，這裡是「做過什麼、為什麼這樣決定」**，兩邊互相連結，不要抄一份。

四段各寫什麼：

| 段 | 寫什麼 |
|---|---|
| **Plan** | 這一次要達到什麼：需求（誰、什麼情況、什麼問題）、目的（做完變成什麼樣）、範圍（做什麼、不做什麼） |
| **Do** | 改動哪些 code、改了什麼、**為何要改**（每個檔案一列）；沒改 code 就寫「無 code 改動」並說明做了什麼 |
| **Check** | 測試的**目的**（為了確認什麼）、怎麼測、**結果**（觀察到什麼）。不寫「N 個通過」 |
| **Act** | Check 後**沒改善的、還沒解決的**，每項寫**在什麼情況下發現的**、影響、後續 |

| 檔案 | 主題 | 結論 |
|---|---|---|
| [2026-10-metrics-to-trace.md](2026-10-metrics-to-trace.md) | metrics 異常怎麼找到對的 trace：exemplar 做不做得到、方案 A（exemplar 轉 log）vs B（時間區間撈一批） | ES 9.5.5 不存 exemplar；採 B；根因分析的程式還沒改 |
| [2026-10-retention.md](2026-10-retention.md) | 保留天數（metrics 降解析度、log 統計、trace 分兩種） | 已合；trace baseline 改成只讀隨機留下的 `traces-generic`（沒用真實資料驗證） |
| [2026-10-data-cleaning.md](2026-10-data-cleaning.md) | 清資料盤點：餵給 baseline 前缺什麼 | 資料進來時已洗過；訓練前的壞資料挑除幾乎都沒有；排出三輪順序 |
| [2026-10-baseline-definition.md](2026-10-baseline-definition.md) | baseline 正式定義與限制 | 統計公式維持簡單；5 項限制，用調整資料解；`groups` 沒定義，借同組實際不會發生 |
| [2026-10-data-quality-check.md](2026-10-data-quality-check.md) | 資料品質檢查（路由沒參數化、欄位缺、資料停了、unassigned 變多） | 已合；第一天只靠昨天比的檢查不會報；門檻是估的 |
| [2026-10-exclude-adjust.md](2026-10-exclude-adjust.md) | 排除已知干擾（排除時段、重置點、週期放寬） | 已合；重置後暖機 24 小時內不判斷（估的）；只放寬不變嚴 |
| [2026-10-chronic-calendar.md](2026-10-chronic-calendar.md) | 慢性病清單與行事曆 | 已合；清單沒送到負責人手上；假日預設連當天故障也不報 |
| [2026-10-check-feedback.md](2026-10-check-feedback.md) | Check：事故紀錄與值班回饋 | 已合；沒有告警出口、回測；回饋靠指令 |
| [2026-10-data-sources.md](2026-10-data-sources.md) | 資料來源接口＋假資料分開；抓台灣行事曆 | 已合；行事曆來源非官方；負責人真來源還沒有 |
| [2026-10-time-alignment.md](2026-10-time-alignment.md) | ② 時間對齊：log 沒帶時區、收資料延遲、主機時鐘 | 沒帶時區的原本差 8 小時、空白分隔的默默換時間，已修；延遲與時鐘偏差、主機時鐘都抓得到；`NTP_SERVER` 要填 |
| [2026-10-trace-durability.md](2026-10-trace-durability.md) | ① trace 收值線要不要 message queue | 不用 Kafka；兩段佇列改寫磁碟（node 端要設在 loadbalancing 的子 exporter）；當掉仍掉約 20% |
| [2026-10-es-startup-401.md](2026-10-es-startup-401.md) | ① ES 剛起來回 401，Collector 直接丟 | 401 窗口約 1 秒（.security 還沒恢復）；修在 ES 入口：健康檢查用 API key 打 GET /（實測掉 0）；兩台 ES 滾動重啟也有窗口（投票名單只有一台）、新檢查都掉 0；不加 Kafka、不改 Collector |
| [2026-10-field-unify.md](2026-10-field-unify.md) | ② 欄位統一：log 身分欄位補 OTel 路徑、Java HTTP 指標在 Collector 改名；metrics ↔ trace 怎麼連 | 在 ingestion 處理、不在查詢層：log 加 alias、Java 指標在 Collector 改名；`variants` 拿掉；`quality.metric_name_legacy` 盯舊名字 |
| [2026-10-ingest-simplify.md](2026-10-ingest-simplify.md) | ①③④ 一把寫入 key、ES failure store、靜態保底規則 | 一把寬 pattern 的 key、一個出口，資料照樣依 group 分；log 的 `k8s-*` 開 failure store（OTel 的預設已開），`quality.ingest_rejected` 有一筆就報；rules 加 `window` 類型，錯誤率 ≥ 5%、p95 ≥ 2 秒保底 |
| [2026-10-alert-exit.md](2026-10-alert-exit.md) | ⑥⑧ 告警出口：一件事一則通知 | 只有 Plan：同服務合成一件、先 dry-run 記基準；通知管道、每個 group 送給誰、夜間要不要發待定 |
| [2026-10-product-loop.md](2026-10-product-loop.md) | ①～⑨ 產品主線 8 步：收值到 learning loop | 盤點：1～4 穩、5 一半；6 收斂、7 的 RAG、8 的回饋入口是斷點；照順序補：告警出口 → 老師傅回饋入口 → 相似事故檢索 → 回饋更新 RAG → 門檻接回饋＋回測 → LLM → 量化 |
