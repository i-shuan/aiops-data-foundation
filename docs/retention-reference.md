# 保留天數與容量：現在的設定

> **類型：規格 Reference**　同主題：[原理（為什麼這樣定）](retention.md)・[操作（怎麼套用）](retention-how-to.md)

天數與每筆資料實測佔多少。為什麼這樣定見 [retention.md](retention.md)，怎麼改見 [retention-how-to.md](retention-how-to.md)。

## 現在的設定

| 資料 | 資料流 | ILM | 保留 |
|---|---|---|---|
| **metrics** | `metrics-*.otel-*` | `metrics-otel` | 原始 30 秒一點 **14 天** → 降成 **5 分鐘**一點 → 第 **90 天**降成 **1 小時**一點 → 第 **400 天**刪 |
| **log 原文** | `k8s-*` | `k8s-logs` | **30 天** |
| **log 統計** | `aiops-log-stats`（transform 每 5 分鐘寫） | `aiops-log-stats` | **400 天**：每 5 分鐘、每個 group × cluster × namespace × 服務 × level × `error.type` 的筆數 |
| **trace：隨機留下的 5%** | `traces-generic.otel-*` | `traces-otel` | **7 天**（給 baseline、拓撲用） |
| **trace：有問題的**（錯誤、5xx、例外、慢） | `traces-problem.otel-*` | `traces-problem-otel` | **30 天**（事故檢討常常一兩週後才開） |
| failure store（ES 拒收的資料） | 各資料流的 failure store | ES 預設 | **30 天**（ES 9 的預設，不用另外設） |
| AIOps 自己的（baseline、異常、trace 段統計） | `aiops-*` | — | 不會自己長大：baseline 每次訓練覆蓋；異常、段統計量很小，之後再定 |

- 天數從「換到下一個 index（rollover）」那一刻起算；rollover 每天一次，所以實際最多多留約 1 天。
- 30 秒、1 分鐘粒度的 baseline 只學最近 14 天（`fine_history`），剛好對齊 metrics 原始資料的 14 天；5 分鐘以上的粒度學 4 週，用降解析度後的資料也夠。

## 每筆資料佔多少（測試環境實測）

| 資料 | 實測每筆 | 估算用 | 一筆是什麼 |
|---|---|---|---|
| log | 135 bytes | **250 bytes** | 一行 log。測試資料每行很像，壓縮特別好；真實 log 內容較雜，保守用 250 |
| Pod 資源（kubelet） | 57 bytes | 60 bytes | 一個 Pod 或容器的一組數值，每個 Pod 每分鐘約 13 筆 |
| K8s 物件狀態 | 47 bytes | 50 bytes | 每個 Pod 每分鐘約 5 筆 |
| 應用自己的 metrics | 78 bytes | 80 bytes | 一組維度的一個時間點。Java agent 預設會吐 JVM、HTTP 等幾十到上百組，**每個 Pod 每分鐘約 100～300 筆，要用真的 Java 服務量過** |
| trace | 276 bytes | **400 bytes** | 一段（span）。測試的 span 很簡單；真實的帶 HTTP、SQL 等欄位，保守用 400 |

都是 ES 壓縮後、**不含副本**的大小。正式環境有一份副本的話，**乘以 2**。
