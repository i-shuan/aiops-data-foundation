# 截圖：POC 五個 KPI 在 Grafana 上長什麼樣

> **類型：案例 Cases**　KPI 的定義與量法見 [pdca/meta/2026-10-poc-acceptance.md](../pdca/meta/2026-10-poc-acceptance.md)・儀表板在 `grafana/helmchart/files/dashboards/aiops-rca.json`

2026-11-03 截自示範 Grafana（Grafana 13、合成的 APM 資料），事故 `a-20261102-1225-mes-api`。

| 圖 | KPI | 儀表板上的位置 | 看什麼 |
|---|---|---|---|
| [01-aggregated-alerts.png](01-aggregated-alerts.png) | 聚合警報 | 「事故與根因」→ 事故清單 | 一列一件事故；mes-api 那件把 95 筆異常合成一件 |
| [02-llm-summary.png](02-llm-summary.png) | LLM 解析 | 「事故與根因」→ LLM 摘要 | `llm_provider` 是 `openai`；每句後面有依據編號 |
| [03-e2e-chain.png](03-e2e-chain.png) | 端到端串查 | 根因報告第 3～5 節 | metrics 異常 → 慢在哪一段 → 最慢請求的 `trace_id` → 依 `trace_id` 撈到的 log |
| [04-similar-top3.png](04-similar-top3.png) | 相似事故 Top-3 | 根因報告「過去相似的事故」 | 只檢索老師傅確認過的；每筆附為什麼像、當時的根因與處置 |
| [05-rca-report.png](05-rca-report.png) | RCA 全文 | 「事故與根因」→ 根因報告全文 | 整份報告：摘要、相似事故、六節分析、結論 |

報告在 Grafana 裡是表格的一格純文字，所以 Markdown 的表格符號會直接顯示出來。
