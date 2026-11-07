# PDCA：現況與待辦

整個專案的「做到哪、還要做什麼」放這一份。各資料夾 README 開頭有自己那一塊的細節，這裡只列項目和去哪看。**每一輪做了什麼的紀錄**不在這裡，在 [records.md](records.md)。

PDCA 的意思：**Plan** 定規則與門檻 → **Do** 收值、判斷、報告 → **Check** 值班判斷對錯、量測 → **Act** 依結果調整。目前 Do 最完整，**Check 是最大缺口**，沒補上就形成不了閉環（[feedback/README.md](../feedback/README.md)）。

**編號只有一套：①～⑨ 資料模組**（① 收值、② 清資料、③ 存、④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback；定義見 [architecture.md](../architecture.md)）。每一項前面的數字是它屬於哪個模組；每一份紀錄開頭的「模組」也是。

狀態：✅ 已做、在 k3s 驗過 ／ ⚠️ 做了一部分 ／ 📝 設計好、還沒做 ／ ❌ 沒做、也還沒設計。

## Plan：定規則、定門檻

| 項目 | 狀態 | 細節 |
|---|---|---|
| ④ 判斷設定：序列、粒度、z、`min_abs`、規則門檻 | ✅ | [engine/configs/baseline](../../aiops-engine/configs/baseline/README.md)（`series.yaml`、`rules.yaml`、`peer.yaml`、`trace.yaml`） |
| ⑨ 上線前記下基準（每天告警幾筆、誤報多少、從告警到回饋多久） | ⚠️ `metrics` 指令有了，數字要等試點 | [phase5-learning-loop/2026-10-metrics-kpi.md](phase5-learning-loop/2026-10-metrics-kpi.md) |
| ④ 用真實資料校正門檻 | 📝 要先排程跑 2～4 週 | [rca/](../rca/README.md) |
| ② 歸屬放哪裡（讀 namespace 的 `dept`，或改成 app → group 中央清單）、app 名稱是否全公司唯一 | 未定案 | [README「還沒定案的」](../../README.md#還沒定案的) |
| ③ 保留天數與容量（用正式環境的量重算） | ⚠️ 已合進 master；天數是暫定值，要用正式環境的量重算 | retention.md |
| ②④ 服務命名規則（`groups`）、交易 ID 命名、寫入 key 怎麼自動進新 cluster | 未定案，要別人配合 | 各資料夾 |

## Do：收值、判斷、報告

| 項目 | 狀態 | 細節 |
|---|---|---|
| ①②③ 收值（log、metrics、trace、尾端取樣）、清資料、存 | ✅ | log、metrics、trace |
| ④ 判斷：靜態臨界值規則 (Static Threshold Rules)、動態基準線學習 (Dynamic Baseline Learning)、Pod 同儕橫向比對 (Peer Comparison)、trace 每一段 | ✅ | [engine/configs/baseline](../../aiops-engine/configs/baseline/README.md) |
| ⑦ 根因分析：metrics → trace → log | ✅ | [engine/configs/rca](../../aiops-engine/configs/rca/README.md) |
| ④ **正式環境排程**（train 每天、detect 每 30 秒、rules 與 peer 每分鐘） | ⚠️ 程式好了，沒部署 | [rca/](../rca/README.md) |
| 已完成部分的小尾巴：② 統一指標名稱（metrics）、② 統一欄位路徑（log）（① trace-sampler 磁碟佇列已做） | ⚠️ | 各資料夾 README 開頭 |
| ①②④ 時間對齊：主機時鐘監控（metrics）、收資料延遲與時區（log）、時鐘差日報（trace） | 📝 | 同上 |
| ①② 業務 ID：交易 ID 命名、從 log 抽出；Kafka 驗證 Java | 📝 | trace/ |
| ⑦ 根因分析改看一批慢 trace 找共同瓶頸（不用 exemplar，ES 不存） | 📝 | [rca/「為什麼不用 exemplar」](../rca/README.md) |
| ④ baseline 補強：p99、慢請求比例、往外呼叫耗時、變化點、Pod → trace 逐段比、趨勢 | 📝 | [rca/](../rca/README.md) |
| ⑥ 收斂：(1) 連續的點合成一段、(2) 同服務合成一件、(3) 沿呼叫關係找源頭 | 📝 | [rca/「雜訊壓制」](../rca/README.md) |
| ⑦ 常駐拓撲、交易時間線工具、根因報告帶業務 ID | 📝 | [rca/](../rca/README.md) |

## Check：看對不對

| 項目 | 狀態 | 細節 |
|---|---|---|
| ⑧ 告警出口（通知） | ✅ 合成一件寫 `aiops-alerts`，Grafana 分送；排程改成一隻 pod、K8s 上跑過 | [phase3-locate/2026-10-alert-exit.md](phase3-locate/2026-10-alert-exit.md)、[phase3-locate/2026-10-schedule.md](phase3-locate/2026-10-schedule.md) |
| ⑧ 事故紀錄 `aiops-incidents`（`incident` 指令、alert 自動開 `auto`、回饋網頁確認） | ✅ 接上告警與回饋；存根因分類、怎麼看出來的、指紋 | [phase5-learning-loop/2026-10-expert-feedback.md](phase5-learning-loop/2026-10-expert-feedback.md) |
| ⑨ 值班回饋 `aiops-feedback`（回饋網頁＋`feedback` 指令） | ✅ 網頁入口（沒登入）；漏報仍靠指令；一句話 LLM 抽成四欄、提 proposed 調整；採納率兩題 | 同上、[phase5-learning-loop/2026-10-feedback-nl-extract.md](phase5-learning-loop/2026-10-feedback-nl-extract.md) |
| ② 資料品質檢查：路由沒參數化、必填欄位缺、資料停了、落到 unassigned 變多（`quality`） | ⚠️ 程式好了、實測過，沒排程 | [baseline README](../../aiops-engine/configs/baseline/reference.md#資料品質檢查資料本身怪不怪) |
| ② 排除已知干擾：事故、維護、壓測時段不學（`exclude_window`）；換版重新學（`reset_point`）；月底批次放寬（`recurring_override`） | ⚠️ 程式好了、實測過，沒排程 | [baseline README「調整」](../../aiops-engine/configs/baseline/reference.md#調整哪些資料不學從哪裡重新學哪些時段放寬) |
| ② 慢性病清單（`chronic`，每週）、行事曆（`calendar.yaml`） | ⚠️ 程式好了、實測過，沒排程；假日已改成直接抓台灣行事曆（`taiwan` provider）；`owners` 目前是**假資料**（`fake-data/owners.json`），要換成真的來源 | [baseline README「慢性病清單、行事曆」](../../aiops-engine/configs/baseline/reference.md#慢性病清單行事曆) |
| ⑨ 回測（`backtest`：改門檻前重跑過去 N 週） | ✅ 沒回測不能核准 | [phase5-learning-loop/2026-10-loop-update.md](phase5-learning-loop/2026-10-loop-update.md) |
| ⑨ 版本（每筆異常記哪一版產生）、評測集（20～30 件過去事故） | ❌ 要真實事故 | [feedback/](../feedback/README.md) |
| ①② REST API 的 log 樣本、重要 .NET Kafka 消費者加幾行 | 要別人配合 | 各資料夾 |

## Act：依結果調整

| 項目 | 狀態 | 細節 |
|---|---|---|
| ④ 手動調整：`series.yaml` 的 `overrides`、`tune` 指令（記原因、誰、何時） | ✅ | [baseline README「使用者、feedback 調整」](../../aiops-engine/configs/baseline/reference.md#使用者feedback-調整) |
| ② 修慢性病（天天噴錯、沒人修的） | 要別人配合 | [rca/](../rca/README.md) |
| ⑨ 同一個根因 ≥ 3 次 → 升級成規則 | ✅ 門檻算好、核准後 `rules` 自動載入；只處理延遲類；`expires_at` 到期不載入，但核准時還設不了 | [phase5-learning-loop/2026-10-rule-threshold.md](phase5-learning-loop/2026-10-rule-threshold.md)、[phase2-anomaly/2026-10-rules-verify.md](phase2-anomaly/2026-10-rules-verify.md) |
| [phase5-learning-loop/2026-11-open-issues-batch.md](phase5-learning-loop/2026-11-open-issues-batch.md) | ④⑧⑨ open-issues 可平行做的七項：B6a 檢查、`tune --show`、異常記版本、基準線不見的告警、漏報進 Grafana 表單、code-review 三殘項、重建 ES 文件 | 七項都做了，404 個單元測試過；Painless、Grafana 規則與面板沒在真環境跑，e2e 等叢集空了再跑 |
| ⑨ 回饋累積夠且回測通過 → 調門檻、排除時段 | ⚠️ 誤報 3 次 → 放寬 z → 回測 → 人核准；還不會提排除時段 | 同上 |
| ② 資料品質檢查的門檻：先估的，正式環境只記錄跑 2～4 週再手動校；`quality` 還不能用 `tune` 調 | 📝 | [baseline README「門檻怎麼校、誰來調」](../../aiops-engine/configs/baseline/verification.md#門檻怎麼校誰來調) |
| ⑦⑨ 相似事故檢索（RAG 的檢索，欄位比對） | ✅ 模擬資料驗過；門檻、權重是估的；匯入指令有了，合成 20 筆 recall@3 50%，等真實事故單 | [phase4-rca-llm/2026-10-similar-incidents.md](phase4-rca-llm/2026-10-similar-incidents.md) |
| ⑦⑨ LLM：根因報告摘要（只加註、每句附依據） | ✅ template 與真的模型（OpenAI）都跑過，每句附依據；評測集 ❌ | [phase4-rca-llm/2026-10-llm-summary.md](phase4-rca-llm/2026-10-llm-summary.md)、[phase5-learning-loop/2026-10-fix-feedback-ux.md](phase5-learning-loop/2026-10-fix-feedback-ux.md) |

### 使用者提的問題（2026-11-07，還沒開工）

| 問題 | 現在做到哪 | 還缺 |
|---|---|---|
| ④ 流量會變、有季節性嗎？ | 動態基準線學習 (Dynamic Baseline Learning) 每條序列分 168 格（星期幾 × 幾點）各學一個中位數，歷史不夠退到「每天同一小時」再退到「不分時段」；粒度依流量自動挑（`pick_resolution`）；假日、活動用 `calendar.yaml` 不學不報；月底批次用 `recurring_override` 放寬 | 只有「一週」這個週期；月初／月底以外的月週期、年週期（年底、季末）、長期趨勢都沒有（[rca/](../rca/README.md) 的 📝「趨勢」） |
| ④⑥ 突然爆量怎麼處理？ | 偏太多（`instant_z`）不等確認直接報，其餘要連續 `confirm_points` 個時間窗；Pod 同儕橫向比對 (Peer Comparison) 抓「只有一個 Pod 爆」；爆完停在新水準由 `level_shift` 提 `reset_point`；預先知道的活動寫進行事曆 | 分不出「好的爆量」（活動、流量真的變多，錯誤率與延遲沒變）跟出事：沒有「流量變大所以延遲變高」的換算，也沒有「請求數爆但錯誤率正常就降級」 |
| ⑨ 回饋後抽什麼資料調門檻？ | 誤報：偏差多大（→ `min_abs`）、z 多少（→ z × 1.4）、事件數（→ `min_count`）、是不是集中在同一時段（→ 時段放寬）、老師傅選的原因優先；整條序列看 precision（<0.7 放寬、>0.95 收緊）；漏報 → z × 0.8；同根因確認 ≥ 3 次 → 規則，門檻取每件事故最低的異常值；事故 → 那段不學。全部 proposed → 回測 → 人核准（`learn/param_suggester.py`） | 真實回饋還沒有，係數（1.4、0.8、0.7）是估的 |
| ⑦⑨ 歷史 case 怎麼幫 RCA 定位？ | 只拿來**分類**：指紋（服務、API、慢的段、錯的起點、指標組合、log 樣式）找最像的 3 件、投票說像哪一類、列出為什麼像；刻意不拿舊根因當答案（`llm/similar.py`） | 想法：(1) 拿 case 的「慢的段、錯的起點」當 `rca_analyzer` 排序的先驗，當下的 trace 還是要證實；(2) 每類列「先查什麼」清單；(3) case ≥ 50 件用 `learn_weights` 學權重；(4) 當 RCA 的回歸測試集。要先有真實事故單 |

另外三個（2026-11-07，計畫在 [meta/2026-11-cases-tier-drop.md](meta/2026-11-cases-tier-drop.md)）：

| 問題 | 回答 | 要做 |
|---|---|---|
| ⑦ 根因分析已經看 metrics、trace、log，歷史 case 補什麼？ | 當下資料回答「哪裡壞了」；case 補「為什麼、上次是什麼、先查哪個」 | 線索排序（當下已證實的才列）、每類的先查清單、考卷（改程式後重跑已確認的事故）；學權重已有（`similar-eval --learn-weights`） |
| ⑥ 緊急的 P0 跟門檻？ | 門檻決定算不算異常，嚴重度決定要不要叫人；現在嚴重度不看服務多重要 | P0 服務清單，嚴重度升一級；門檻不動 |
| ④ baseline 往下掉，放寬？移動區間？依特徵？ | 依特徵：流量往下掉照常報；延遲、錯誤往下掉不通知，但提重新學；移動區間（4 週）已有，但要約 2 週才跟上 | `shift` 也看只判往上的序列往下的變化 |

**之後**：Elastic 付費 AIOps 功能評估（試用只能開一次，等資料累積夠再開）；工廠（EAP、機台、MES…）不在目前範圍。

**建議順序**：正式排程（只寫入不通知）→ 收斂(1)(2)＋告警出口＋事故紀錄、回饋 → 用 2～4 週真實資料校正門檻 → LLM 摘要 → 回測、版本 → 收斂(3)、自動調門檻。

## 每一輪的紀錄

寫紀錄的方式與全部紀錄的索引在 [records.md](records.md)（範本 [TEMPLATE.md](TEMPLATE.md)）。
