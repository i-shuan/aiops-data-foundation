# Feedback：告警出口、事故紀錄、回饋、調整、LLM

> **類型：原理 Explanation**

⑧ 出口之後的路是：告警送到值班 → 資深維運專家（老師傅）判斷對錯 → 結果寫回 ⑨ feedback → 調整設定；LLM 用現成模型加註報告，不微調、不自己訓練模型，統計層照常偵測。

## 閉環怎麼走

| 階段 | 做什麼 | 落在哪 |
|---|---|---|
| 判斷 | ④ 的結果全寫入異常總表，帶 `status`（warning／anomaly）、可信度、粒度、方法 | `aiops-anomalies` |
| 出口 | ⑥ 同服務合成一件，Grafana 依部門分送 | `aiops-alerts` |
| 紀錄 | 告警一開就建一筆 `auto`（帶指紋）；老師傅在回饋網頁確認後改 `confirmed`，存根因分類、處置、怎麼看出來的、查了哪裡 | `aiops-incidents` |
| 回饋 | 回饋網頁 `feedback_web`（通知裡有連結）或指令；記哪一筆、為什麼 | `aiops-feedback` |
| 調整 | `series.yaml` 的 `overrides` 或 `tune` 指令；記原因、誰、何時 | `aiops-tuning` |

欄位格式見 [aiops-data-reference.md](../aiops-data-reference.md)。

回饋四種結果與用法：

| 結果 | 用法 |
|---|---|
| 確認（根因對） | 寫進 `aiops-incidents`；相似事故檢索（RAG）只讀 `confirmed` 的；同根因累積 3 次，產生規則草稿供人審閱 |
| 根因錯 | 記正確根因，列入評測集，改根因分析或 prompt 時要能答對 |
| 誤報 | 同一條序列累積 3 次，產生調整建議，經人核准才生效；單次回饋不生效 |
| 漏報 | 降 z，補序列或規則；目前只能用指令回報 |

建議核准前必須跑 `backtest`：現行設定與「現行＋建議」各重跑過去 N 週，已確認事故少抓一件就不建議；沒回測的建議不能核准。

## 為什麼用 LLMOps，不自己訓練模型

決定：以 LLMOps 為主，借機器學習的紀律（版本、評測、回測、feedback 閉環）；不微調 LLM，不自己訓練偵測模型。

- **偵測不交給 LLM**：約 1 萬條序列，每 30 秒～5 分鐘判斷一次，一天幾百萬到幾千萬次；MAD 每次幾毫秒 CPU，LLM 撐不住。LLM 算中位數、百分位、z 值不可靠，同一份資料問兩次結果可能不同，說不出「z=11，超過 5」這種可稽核的門檻，全部 metrics 送雲端也過不了資安。
- **統計層的「訓練」保留**：每天用 4 週資料算每個時段的中位數與 MAD，幾秒跑完，不需要 MLOps 平台。
- **不微調**：微調要資料、版本、重訓。RAG 加 prompt 即可，回饋寫進去立刻有效，刪掉就退回，換模型不用重做。
- **不訓練分類器過濾誤報**：一天只有幾十筆異常，LLM 看異常、根因報告與事故紀錄就能判斷，不必累積數月標註。
- **多變量模型暫不做**：IT 服務多為單一指標問題，MAD、靜態臨界值規則、Pod 同儕橫向比對已涵蓋；趨勢預測用斜率或現成時序基礎模型。

重新評估 MLOps 的條件（任一成立）：

1. MAD 跑數月，誤報或漏報仍高，原因是要多指標一起看。
2. 範圍擴到機台、製程，有明確目標（非計畫停機、製程飄移）。
3. 已累積足夠標註：事故紀錄、告警回饋、機台故障與保養紀錄。

設備、製程部門若已有 FDC 或預測保養，本平台只提供 IT 層資料（EAP 通訊、交握耗時、主機狀態）給他們的模型用，不另建 MLOps。

LLM 的邊界：只加註、降級，不刪告警。統計層照常報，LLM 錯了也不漏。

## 紀律

1. **版本**：baseline、設定、prompt 帶版本，每筆異常記下產生它的版本。
2. **回測**：改門檻前用新設定重跑過去 4 週，過去事故仍抓得到、誤報減少。
3. **評測集**：20～30 件過去事故，改根因分析、prompt、換模型前都跑。
4. **防污染**：回饋累積多次或第二人確認；`confirmed` 事故優先於單次回饋。
5. **記憶期限**：舊版批次造成的誤報，改版後不成立，記憶要綁版本或設期限。
6. **呼叫記錄**：每次記輸入、輸出、token、時間，沿用 OpenTelemetry 生成式 AI 欄位寫進同一個 ES，成本與延遲用現有監控看；資料不能出廠時用可自架工具。

## LLM 何時叫、token 怎麼省

程式負責算與收斂，LLM 只看收斂後的一件事故，不收原始 metrics、log。新的一件事故跑完根因分析後呼叫一次，由 `rca.yaml` 的 `llm: true` 開關控制。輸出是報告最前面 3～5 句，每句結尾附依據 id。引用不存在的 id 或一句沒有依據，整段不顯示（程式檢查）。LLM 掛了或逾時，報告照出、少這一段。實作在 [llm.py](../../engine/core/llm.py)，`LLM_PROVIDER` 可選 `template`（固定句型，不叫模型，也是失敗時的退路）、`anthropic`、`openai`。

輸入包的 token 估計（五項合計約 4,000）：

| 內容 | 壓法 | token |
|---|---|---|
| 指示 | 固定 | ~800 |
| 異常表 | 收斂後最多 10 筆，每筆一行 | ~500 |
| trace 段落 | 差最多的前 5 段、錯誤源頭、最慢請求摘要 | ~1,000 |
| log 樣式 | 新出現或變多的前 10 種，只送樣式 | ~600 |
| 過去事故 | RAG 取最像 3 件，只放結論 | ~900 |
| 輸出 | 摘要、判斷、建議 | ~500 |

一天 50 件（每件輸入加輸出約 4,500 token）合計約 22.5 萬 token。省法：

1. 只送收斂後的事故，一天幾十件，不是幾千筆異常。
2. 固定指示放前面，用 prompt caching。
3. 先給摘要，LLM 需要才呼叫工具（查某服務 trace、某段 log 樣式、某 API 的 p95），數字由程式算。
4. 分級：低嚴重度、可信度 low 只做摘要；critical 才做完整推理與對照過去事故。

## 現況與沒做的

| 項目 | 狀態 | 依據 |
|---|---|---|
| 異常總表 | ✅ | `aiops-anomalies` 全部判斷結果寫入 |
| 告警出口 | ✅ | [alert-exit](../pdca/phase3-locate/2026-10-alert-exit.md)、[schedule](../pdca/phase3-locate/2026-10-schedule.md) |
| 事故紀錄 | ✅ | [expert-feedback](../pdca/phase5-learning-loop/2026-10-expert-feedback.md)、[similar-incidents](../pdca/phase4-rca-llm/2026-10-similar-incidents.md) |
| 回饋與調整 | ✅ | 回饋網頁加指令兩條路；漏報只能用指令；網頁沒有登入機制 |
| 回測 | ✅ | [loop-update](../pdca/phase5-learning-loop/2026-10-loop-update.md) |
| LLM 摘要 | ⚠️ | [llm-summary](../pdca/phase4-rca-llm/2026-10-llm-summary.md)、[fix-feedback-ux](../pdca/phase5-learning-loop/2026-10-fix-feedback-ux.md)：OpenAI 實跑 12 次，修後 6/6 每句附依據、每件 5～8 秒；沒有評測集，token 用量未量 |
| 量化 | ⚠️ | `metrics` 指令輸出件數、有回饋比例、誤報率、到回饋多久；[metrics-kpi](../pdca/phase5-learning-loop/2026-10-metrics-kpi.md)；真實數字等試點 |
| 版本、評測集 | ❌ | baseline 只記 `trained_at` 與訓練範圍，無版本號；評測集未建 |
| 基準 | ❌ | 上線前未記每天告警筆數、誤報比例、告警到找到原因的平均時間 |

整條線在模擬資料上跑通一輪（[architecture.md](../architecture.md)）。下一步：上 K8s、用有 trace 的事故驗證、試點產線先只寫入不通知 2～4 週。

## 細節去哪看

- 五個 aiops-* 索引的欄位、誰寫、範例：[aiops-data-reference.md](../aiops-data-reference.md)
- 根因分析的程式與設定：[engine/configs/rca/README.md](../../engine/configs/rca/README.md)
- 調整與 baseline 的規格：[engine/configs/baseline/reference.md](../../engine/configs/baseline/reference.md)
