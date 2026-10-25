# 排程上線：定期跑、停了要知道、先只寫入不通知（2026-10）

**狀態**：進行中　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-complete`；主線 [product-loop](2026-10-product-loop.md) 順序 1 之後的「上線」
**模組**：④⑤⑥（排程）、⑧（Grafana 多一條告警）

## Plan：這一次要達到什麼

- **需求**：rules、detect、quality、peer、alert、train、chronic 現在都是手動打指令。沒有排程，正式環境跑不起來；`investigator alert` 停了，Grafana 會以為所有件都恢復（心跳沒了），沒有人會知道。
- **目的**：
  1. 一份 K8s 的 CronJob 設定（跟 Collector 一樣用 kustomize），每個指令照它該跑的頻率跑；同一個指令不會兩份同時跑。
  2. Grafana 多一條「AIOps 停了」的告警：`aiops-alerts` 10 分鐘沒有任何寫入就送平台。
  3. 正式環境**先只寫入、不開通知**跑 2～4 週，記下基準（[量化](2026-10-metrics-kpi.md) 的 `metrics` 指令算）。
- **範圍**：engine 的 Dockerfile、CronJob、ConfigMap／Secret 的接法、Grafana 規則。不做：自動擴充、多副本。

| 指令 | 頻率 | 為什麼 |
|---|---|---|
| `detector rules` | 每 1 分鐘 | 狀態變化要快 |
| `detector detect` | 每 1 分鐘（`--window 10m`） | 30 秒一點的序列，每分鐘看最近 10 分鐘；同一筆重跑會覆蓋 |
| `detector peer` | 每 5 分鐘 | 跟兄弟比看最近 10 分鐘 |
| `detector quality` | 每 10 分鐘 | 資料品質 |
| `investigator alert` | 每 1 分鐘 | 合成一件、心跳 |
| `detector train`、`trace-train` | 每天 02:00 | 學過去 4 週 |
| `detector trace-collect`、`trace-detect` | 每 5 分鐘 | trace 每一段 |
| `detector chronic` | 每週一 08:00 | 慢性病清單 |

`concurrencyPolicy: Forbid`：上一次還沒跑完就跳過這次，不會兩份同時寫。

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
