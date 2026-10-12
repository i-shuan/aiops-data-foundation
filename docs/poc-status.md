# AIOps PoC：現況與下一步（簡報內容）

簡報「IT Observability 與 AIOps PoC：現況與下一步」的文字。狀態依 k3s 實測結果，不用完成比例。

**副標**：從資料打通、異常辨識到 RCA 說明，並以回饋閉環持續校正，逐步驗證端到端分析能力

## 六個階段

| # | 階段 | 內容 | 狀態 |
|---|---|---|---|
| 01 | 資料打通 Data | Metrics / Logs / Traces 欄位一致（服務、Pod、namespace）＋ Trace ID 關聯；尾端取樣保留問題 trace | PoC 已驗證 |
| 02 | 異常辨識 Detect | Median + MAD Baseline（星期 × 小時）：流量、錯誤率、延遲、CPU、記憶體＋trace 每一段 | PoC 已驗證｜下一步：正式排程 |
| 03 | 問題定位 Locate | Metrics → Trace → Log，定位服務、API 與 Segment（扣掉下游時間、找錯誤源頭） | PoC 已驗證 |
| 04 | 輔助判斷 Diagnose | Rule Engine + Peer Analysis，判斷是資源、平台還是單一 Pod | PoC 已驗證｜Peer → Trace 待整合 |
| 05 | 事件整合 Correlate | Alert Correlation + Incident，合併同一事件與上下游影響 | 規劃中 |
| 06 | 智慧說明 Explain | Log Pattern + Knowledge + LLM，產出有證據的 RCA 建議（只加註、不擋告警） | Log Pattern 已驗證｜LLM 規劃中 |

**↺ 回饋學習 Feedback（貫穿 02～06）**：值班標註「真事故／誤報」＋事故紀錄 → ① 排除事故時段再算 baseline ② 調整門檻 ③ 寫入 LLM 知識記憶。以版本、回測、評測集控管，不自己訓練模型。狀態：規劃中，與 02 上線同時開始收集。

**底部說明**：目前以 PoC 驗證端到端可行性；重點不是完成比例，而是確認每個階段所需的資料、分析方法與輸出結果，並以回饋閉環讓結果持續變準。

## 下一步

1. **02 正式排程上線**：只寫入、不通知，用 2～4 週真實資料校正門檻。
2. **告警出口＋回饋＋事故紀錄**：先收集，不自動生效。
3. **05 合併告警、連續異常才報**：誤報降下來再接通知。
4. **06 LLM**：從根因報告摘要開始（只加註），先用 20～30 件過去事故評測。

依據：[aiops-llm-mlops.md](aiops-llm-mlops.md)（feedback 閉環、LLMOps）、[aiops-rca.md](aiops-rca.md)、[aiops/baseline/README.md](../aiops/baseline/README.md)。
