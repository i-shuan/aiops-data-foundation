# Grafana 上看得到：根因分析、老師傅回饋、回饋調門檻（2026-10）

**狀態**：完成（60 分驗證：能跑通、有截圖；資料是模擬加 k3d 模擬服務）　　**日期**：2026-10-29　　**相關**：分支 `claude/grafana-demo-screens`；前置 [grafana-form](2026-10-grafana-form.md)、[rule-threshold](2026-10-rule-threshold.md)、[k3d-e2e](../archive/phase1-collect/2026-10-k3d-e2e.md)
**模組**：⑦ 根因分析、⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：Grafana 只有回饋表單；看不到事故、根因報告、LLM 摘要，也看不到「回饋之後門檻真的變了」。
- **目的**：在 Grafana 上走一遍並截圖：① 事故的根因報告（metrics → trace → log＋LLM 摘要＋相似事故）② 老師傅在表單填回饋 ③ 回饋累積成調整建議，核准後偵測的結果真的變少。
- **範圍**：只做兩個唯讀儀表板和對應的 ES data source；不改 engine 邏輯。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/configs/grafana/dashboards/aiops-rca.json`（新） | 事故清單、LLM 摘要、相似事故與通知摘要、根因報告全文、異常；選一件事故看 | Grafana 上看根因 |
| `engine/configs/grafana/dashboards/aiops-learning.json`（新） | 批次異常筆數、回饋筆數、已生效調整、調整建議表（含回測）、老師傅回饋 | 看得到回饋 → 建議 → 回測 → 核准 → 筆數變化 |
| `engine/configs/grafana/datasource.yaml` | 加 `aiops-anomalies`、`aiops-tuning`（時間欄位是 `at`）、`aiops-feedback` | 兩個儀表板要讀 |

## Check：測試的目的與結果

環境：docker 的 ES 9.5.5、Grafana 12.2＋Business Forms 6.3.1、真的 OpenAI；Grafana 用唯讀 API key（`aiops-*` read）連 ES。

| 步驟 | 做了什麼 | 結果（截圖） |
|---|---|---|
| ① metrics → trace → log | k3d 上模擬 mes-api → wip-worker → Oracle，Oracle 變 1.5 秒；一筆 p95 異常進 `investigator alert` | 一件事故，報告六段齊全；LLM 摘要每句附依據：Oracle 那段每個請求多 841 ms、連線池逾時 115 次 → [img](img/grafana-1a-rca-metrics-trace-log.png) |
| ① 相似事故（RAG） | 模擬資料第 2 次同樣症狀 | 附第 1 次的根因、處置、怎麼看出來的；LLM 摘要引用 [I1] → [img](img/grafana-1b-rca-similar-rag.png) |
| ② 老師傅回饋 | 在 Grafana 表單對上面那件選「真的」、DB、根因、處置、怎麼看出來的，送出 | 「已記錄」；`aiops-incidents` 變 confirmed、記了根因與確認人 → [img](img/grafana-2-expert-feedback.png) |
| ③ 核准前 | 月底批次被報 3 次、值班都標誤報；同根因確認 3 次；`feedback suggest`、`backtest` | 兩筆建議 proposed：z 5 → 7（回測告警 3 → 1）、升級成規則（門檻 451.1）；批次異常 3 筆、已生效調整 0 → [img](img/grafana-3a-before-approve.png) |
| ③ 核准後 | 人核准兩筆；清掉批次的異常、同樣的三段時間重跑 detect | 批次異常 3 → 1、已生效調整 2；跟回測預測一致 → [img](img/grafana-3b-after-approve.png) |

- 核准後不是 0 而是 1：第 2 次批次的 z 是 7.3，比新門檻 7 高，仍會報。這是回測就預測到的（3 → 1），不是壞掉。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 根因報告是 markdown 原文，在 Grafana 表格裡顯示成純文字 | 截圖 1a | 表格、瀑布圖沒排版，可讀但不漂亮 | 要漂亮再裝 markdown 外掛，或連到 feedback_web 的 `/report` |
| 那件 trace 事故的「平常」是空的 | 截圖 1a：「平常（沒有資料）」 | k3d 的 Collector 剛起，沒有前一段 trace，只能看現在 | 試點有歷史資料後會有 |
| 事故的 metrics 異常是我寫一筆進 ES 模擬的，不是 baseline 偵測出來的 | 腳本 | metrics 偵測這段沒包含 | 真服務上跑 `detect` |
| 儀表板的 ES key 是這次臨時發的 | demo | 正式要用 `elasticsearch-setup.sh` 的 Grafana 唯讀 key，並確認讀得到 `aiops-*` | 上 K8s 時一併驗 |
| 儀表板要看的時間範圍要自己選（模擬資料在 10 月初） | 截圖 | 預設 `now-7d` 看不到 | 真資料就是現在 |

- **決定**：60 分的驗證到此；Grafana 上三段都看得到、動得了。
