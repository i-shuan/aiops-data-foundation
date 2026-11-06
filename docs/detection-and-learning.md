# 異常偵測與學習機制：現在寫了什麼、沒有什麼

> **類型：原理 Explanation**　方法的判斷細節見 [rca/README.md](rca/README.md)・回饋閉環見 [feedback/README.md](feedback/README.md)・參數與規格見 [baseline 規格](../aiops-engine/configs/baseline/reference.md)
> 🟡 前面是現況；最後一節「貝氏更新」是構想，尚未實作。

**這個 repo 沒有強化式學習**：沒有獎勵函數、策略更新、Q-learning、bandit。現有的「學習」都是統計，加上人核准的規則調整；沒有一個是自己試錯、自己更新策略的演算法。[feedback/README.md](feedback/README.md) 定的原則是「AIOps 以統計＋LLM 為主，不自己訓練模型」。

## 異常偵測（④ 判斷，`service/detect/`）

| 方法 | 做法 | 程式 |
|---|---|---|
| 動態基準線學習 (Dynamic Baseline Learning) | 每條序列（服務 × API × 指標）取過去 4 週「同星期幾、同小時」的中位數與 MAD，共 168 個時段；歷史不夠往下退（同小時、全部）；z ≥ 5 且差距 ≥ `min_abs` 才算；連續 `confirm_points` 個窗才升 `anomaly`，否則 `warning` | [model.py](../aiops-engine/src/service/detect/baseline.py)：`train`、`score`、`mark_status` |
| 稀少筆數的卜瓦松版 | 學平均次數 λ，機率 < 0.001 才算 | `score_poisson` |
| 粒度自選 | 每條序列從 1m 到 1h 挑事件數夠的最細窗 | `pick_resolution` |
| 借組基準線 | 新 API、量少的序列借同類成員的基準線，門檻調高 | `train_groups` |
| 靜態臨界值規則 (Static Threshold Rules) | 狀態變化、錯誤率 5%、p95 2 秒、趨勢（Theil–Sen 斜率）、累計增量 | [rules.py](../aiops-engine/src/service/detect/rules.py) |
| Pod 同儕橫向比對 (Peer Comparison) | 同服務的 Pod 互比，用其他 Pod 的中位數與 MAD | [peer.py](../aiops-engine/src/service/detect/peer.py) |
| trace 每一段的基準線 | 每個請求每一段自己的時間，出事時跟平常比 | [trace.py](../aiops-engine/src/service/detect/trace_stats.py) |
| 排除時段、重置點 | 行事曆、假日、維護窗不學、不判斷 | [adjust.py](../aiops-engine/src/service/detect/adjustments.py) |

全部是統計方法（中位數、MAD、卜瓦松、秩統計），沒有訓練任何模型。

## 學習機制（⑨ feedback，人在迴圈裡）

| 機制 | 做了什麼 | 自動還是要人 |
|---|---|---|
| 誤報 → 調整建議 | 有人標就提（不累積）：時段固定 × z 壓得掉查表（那 1 小時 z／那 1 小時不報不學／整條 z／`min_abs`），老師傅選的原因優先（換版 → `reset_point`、壓測 → `exclude_window`、量太少 → `min_count`） | 提建議自動，**生效要回測、人核准** |
| 漏報 → 收緊建議 | z × 0.8 | 同上 |
| 回測 | 核准前用過去 N 週重跑，看告警少幾個、已確認事故還抓不抓得到 | 自動 |
| 同根因 ≥ 3 次 → 規則草稿 | 取那幾件的尖峰最小值當門檻 | 人核准 |
| 回饋一句話抽成結構化欄位 | LLM 抽 `feedback_type`、`suggested_action`，寫一筆待審核的調整 | 抽取自動，人核准 |
| precision 建議 | reviewed ≥ 10：precision < 70% 提整條放寬、> 95% 且有漏報提收緊 | 同上；**z 遠大於門檻的序列上無效**（見下） |
| case 分類 | 指紋比對（權重手設 4、4、3、2、2、1）、前 3 件以分數投票；`learn_weights` 在 case ≥ 50 件時才學，只印建議 | 投票自動，權重不會自動改 |
| 嚴重度分級 | 依訊號廣度、持續、換版上限分 critical／high／warning，只通知前兩級 | 自動 |

程式：⑨ 在 [service/feedback/](../aiops-engine/src/service/feedback/)（[suggest.py](../aiops-engine/src/service/feedback/suggest.py)、[feedback_service.py](../aiops-engine/src/service/feedback/feedback_service.py)、[backtest.py](../aiops-engine/src/service/feedback/backtest.py)）、[similar.py](../aiops-engine/src/service/rca/similar.py)、[severity.py](../aiops-engine/src/service/converge/severity.py)。

## 跟強化式學習差在哪

| 強化式學習的要件 | 現況 |
|---|---|
| 狀態、動作、獎勵 | 沒有定義；回饋只是人標的「真的／誤報」，沒有轉成獎勵 |
| 策略自己更新 | 沒有；門檻只能「提建議 → 回測 → 人核准」，不會自己調 |
| 探索與試錯 | 沒有；調錯會漏報，這種代價不能讓系統自己試 |
| 回饋量 | 測試環境 42 筆回饋、約 16% 的告警有人標；強化式學習要的樣本量差好幾個數量級 |

門檻自動調整風險高（漏報比誤報貴），所以每筆調整都要回測、人核准。

## 已知哪裡不準

用合成資料（5 個服務、5 個情境）量到的誤報來源：

| 來源 | 內容 | 狀態 |
|---|---|---|
| 稀少事件的比例用 z／MAD 判 | `http.error_rate` 平常約 1%，每分鐘中位數與 MAD 都是 0，波動只剩下限 1e-9，任何一個失敗的 z 都是 5000 萬倍以上；擋它的只剩 `min_abs`、`min_count`，但 1% 失敗率下一分鐘隨機失敗 ≥ 3 個本來就不稀奇（50 個請求約 1.4%；5 服務 × 180 分鐘約 12 個窗） | 未修；該用二項檢定（跟 `score_poisson` 並列） |
| 偵測不知道剛換版 | 換版後 4 分鐘的震盪是真的偏離，只是不是事故 | 嚴重度那層把換版後 10 分鐘內壓在 warning；偵測層未處理 |
| 放寬 z 對 z 是上億倍的序列無效 | 誤報 z 是 7500 萬～2 億，放寬到 7 擋不到；有效的是那 1 小時不報不學或 `min_count` | 已修：`suggest_for_false_positives` 改查表，同一批 28 筆標記誤報率 61% → 21%（[紀錄](pdca/meta/2026-11-suggest-features.md)）；`precision 建議` 仍提 z |
| 基準線學進進行中的事故 | 事故拖過每天 02:00 的 `train`，會被當成平常 | 未修 |
| 分類沒有拓撲否決 | 分類為 db 但這件的 trace 沒有 DB 類 span | 未做；23 件合成事故分類準確率 52% |

通知給人的誤報（嚴重度分級之後）在這批資料上是 0／3，異常點層級仍是 74／347（21%）；樣本小、標準答案是自己定的、分級參數在同一組資料上調過，不能當真實誤報率。

## 貝氏更新（⚪ 構想，尚未實作）

想要「門檻隨回饋自動調整、越用越準」，比強化式學習合適的是對每條序列做貝氏更新：

| 項目 | 做法 |
|---|---|
| 狀態 | 每條序列一個 precision 的 Beta 分布（先驗 Beta(1, 1)，或用同類序列的平均當先驗） |
| 更新 | 老師傅標「真的」加一個 α、標「誤報」加一個 β；根因報錯算真的 |
| 動作 | precision 的下界（例如 5% 分位）< 目標 → 放寬；上界 > 目標且有漏報 → 收緊；每次只動一步（z × 1.2、有上下限） |
| 安全範圍 | 範圍內可以不經人核准自動生效；超出範圍（例如 z 要超過 10）仍走「建議 → 回測 → 人核准」 |
| 對誤報來源 ① 無效 | z 失效的序列要先改判法（二項檢定），貝氏更新動不了 |

好處是便宜、可解釋、樣本需求小（十幾筆回饋就有信號）。要先有真實的老師傅回饋量，否則 Beta 分布只是在擬合合成資料。
