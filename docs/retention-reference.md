# 保留天數與容量：現在的設定

> **類型：規格 Reference**　同主題：[原理（為什麼這樣定）](retention.md)・[操作（怎麼套用）](retention-how-to.md)

三種資料一覽與共用的設定。各資料的細節、每筆大小見 [log](log/README.md#多久壓縮)、[metrics](metrics/README.md#多久壓縮)、[trace](trace/README.md#多久壓縮)；為什麼這樣定見 [retention.md](retention.md)，怎麼改見 [retention-how-to.md](retention-how-to.md)。

## 現在的設定

| 資料 | 資料流 | ILM | 保留 |
|---|---|---|---|
| **metrics** | `metrics-*.otel-*`（下一列以外） | `metrics-otel` | 原始 30 秒一點 **14 天** → 降成 **5 分鐘**一點 → 第 **90 天**降成 **1 小時**一點 → 第 **400 天**刪 |
| **metrics：應用的直方圖** | `metrics-generic.otel-*` | `metrics-hist-otel` | **不降解析度**，原始 30 秒一點留 **90 天** |
| **log 原文** | `k8s-*` | `k8s-logs` | **30 天** |
| **log 統計** | `aiops-log-stats` | `aiops-log-stats` | **400 天** |
| **trace：raw**（每一段一筆，Collector 寫，100% 收） | `traces-generic.otel-*` | `traces-otel` | **2 天**：只是合成前的暫存 |
| **trace：merged**（一個請求一筆，engine 合成） | `traces-merged-<group>-<日期>` | `traces-merged` | **7 天** |
| failure store（ES 拒收的資料） | 各資料流的 failure store | ES 預設 | **30 天**（ES 8.19、9 的預設，不用另外設） |
| AIOps 自己的：衍生資料與告警（baseline、異常、trace 段統計、事故與 RCA 報告） | `aiops-baseline`、`aiops-anomalies`、`aiops-trace-segments`、`aiops-alerts` | — | `aiops-baseline` 每次訓練被取代；其餘**沒有 ILM、不會自己消失**，量小但會一直長。誰寫、多久一次、各索引的保留：[data-storage-reference.md](data-storage-reference.md) |
| AIOps 自己的：人工判斷（回饋、事故紀錄、調整） | `aiops-feedback`、`aiops-incidents`、`aiops-tuning` | — | 不自動刪，保留天數還沒定（事故紀錄要長留）；目前沒有 snapshot 備份，掉了不能重算 |

- 天數從「換到下一個 index（rollover）」那一刻起算；rollover 每天一次，所以實際最多多留約 1 天。
- 每筆大小都是 ES 壓縮後、**不含副本**的大小。正式環境有一份副本的話，**乘以 2**。
