# AIOps PoC：現況與下一步（簡報內容）

簡報「IT Observability 與 AIOps PoC：現況與下一步」的文字。狀態依 k3s 實測結果，不用完成比例。編號只有一套：①～⑨ 資料模組（定義見 [architecture.md](architecture.md)）。

**副標**：從資料打通、異常辨識到 RCA 說明，並以回饋閉環持續校正，逐步驗證端到端分析能力

## 九個模組

| # | 模組 | 內容 | 狀態 |
|---|---|---|---|
| ① | 收值 | Metrics / Logs / Traces 收進來，舊專案不改程式；尾端取樣保留問題 trace | PoC 已驗證 |
| ② | 清資料 | 欄位一致（服務、Pod、namespace）＋ Trace ID 關聯；擋冒充、去重複、格式統一。餵給 baseline 前再檢查一次：資料品質、排除事故與維護時段、換版重置、慢性病清單、行事曆 | 進來時的清理 PoC 已驗證｜餵給 baseline 前的檢查程式已完成、實測過，尚未排程 |
| ③ | 存 | 中央 ES，資料依部門分開存、一把寫入 key；被拒的文件進 failure store；保留天數、降解析度 | PoC 已驗證｜天數是暫定值 |
| ④ | 判斷 | Median + MAD Baseline（星期 × 小時）：流量、錯誤率、延遲、CPU、記憶體＋trace 每一段；規則（Rule）；同服務 Pod 互比（Peer），判斷是資源、平台還是單一 Pod | PoC 已驗證｜下一步：正式排程｜Peer → Trace 待整合 |
| ⑤ | 異常總表 | 所有判斷結果寫同一個 index（`aiops-anomalies`） | PoC 已驗證 |
| ⑥ | 收斂 | 合併同一事件與上下游影響（Alert Correlation） | 規劃中 |
| ⑦ | 根因分析 | Metrics → Trace → Log，定位服務、API 與 Segment（扣掉下游時間、找錯誤源頭）；Log 樣式 | PoC 已驗證 |
| ⑧ | 出口 | 告警通知、事故紀錄 | 事故紀錄已可收集｜告警通知規劃中 |
| ⑨ | feedback | 值班標註「真事故／誤報」、誤報率統計、誤報累積才提門檻建議；LLM 摘要（只加註、不擋告警） | 值班回饋已可收集｜LLM 規劃中 |

**↺ ⑨ feedback 回饋學習**：值班標註「真事故／誤報」＋事故紀錄 → (a) 排除事故時段再算 baseline (b) 調整門檻 (c) 寫入 LLM 知識記憶。以版本、回測、評測集控管，不自己訓練模型。狀態：(a)(b) 的建議已能產生（都要人核准才生效），版本、回測、評測集和 (c) 規劃中。

**底部說明**：目前以 PoC 驗證端到端可行性；重點不是完成比例，而是確認每個模組所需的資料、分析方法與輸出結果，並以回饋閉環讓結果持續變準。

## 下一步

1. **④ 判斷：正式排程上線**：只寫入、不通知，用 2～4 週真實資料校正門檻。
2. **⑧ 出口、⑨ feedback：告警出口**：事故紀錄與回饋已能收集（不自動生效），還缺通知出口與回測。
3. **⑥ 收斂**（連續異常才報已做：跳一次 warning、連續兩個時間窗才 anomaly）：合併同一事件，誤報降下來再接通知。
4. **⑨ LLM**：從根因報告摘要開始（只加註），先用 20～30 件過去事故評測。

依據：[feedback/llm-mlops.md](feedback/llm-mlops.md)（feedback 閉環、LLMOps）、[rca/README.md](rca/README.md)、[engine/configs/baseline/README.md](../engine/configs/baseline/README.md)、[pdca/README.md](pdca/README.md)。
