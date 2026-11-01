# 資料存哪裡：ES 與 Grafana

> **類型：規格 Reference**　同主題：[保留天數（ILM、容量）](retention-reference.md)・[aiops-* 的欄位](aiops-data-reference.md)・[回饋與學習](feedback/README.md)

AIOps 與 Grafana 的資料各存在哪裡、誰寫、多久一次、會不會自己消失。**沒有模型檔、沒有本機資料庫**：訓練的結果、RCA 報告、回饋，全部是中央 ES 裡的索引。Grafana 自己幾乎不存資料。

## 一眼看懂

| 問題 | 答案 |
|---|---|
| 訓練（基準線）存哪？ | ES 的 `aiops-baseline`，不是模型檔 |
| RCA 報告存哪？ | ES 的 `aiops-alerts`，一件一筆，報告全文在 `report` 欄位 |
| Grafana 怎麼拿到？ | 直接讀 ES 的 `aiops-*`；通知裡的報告連結由回饋網頁從 `aiops-alerts` 讀出來顯示 |
| 會自己消失的有哪些？ | 原始資料有 ILM（log、metrics、trace）；`aiops-baseline` 每次訓練被取代。**所有其他 `aiops-*` 都不會** |
| Grafana 自己存什麼？ | 設定在 repo 的檔案；內部 sqlite 在容器裡、沒掛 PVC，Pod 重建就清掉 |

## ES：AIOps 寫的索引

| 索引 | 存什麼 | 誰寫、多久一次 | 會不會自己消失 |
|---|---|---|---|
| `aiops-baseline` | 動態基準線：每條序列 × 星期幾 × 小時的中位數、MAD、p05、p95 | `detector train`，每天 02:00 | **被取代**：先寫新的，再刪這次沒有的舊版（改了粒度、4 週內沒出現的 API）；ES 查不到任何資料時不刪 |
| `aiops-trace-segments` | trace 每一段的統計：每個 API × 每一段 × 每 5 分鐘的呼叫次數、耗時、錯誤率、吞例外比例；另有時鐘差 | `detector trace-collect`，每 5 分鐘 | **不會**，沒有 ILM |
| `aiops-anomalies` | 異常總表：每個異常一筆（序列、對象、值、平常值、z、status） | `detect`（每分鐘）、`rules`（每分鐘）、`peer`、`trace-detect`、`quality` | **不會**；同一個對象同一時槽的 `_id` 固定，重跑是覆蓋，但新的時槽一直新增 |
| `aiops-alerts` | 事故（一件一筆）：標題、摘要、**RCA 報告全文**、LLM 摘要、相似事故、連帶的上游服務；另有 `heartbeat`、`detect-status` 兩筆狀態 | `investigator alert`，每分鐘；開著的件每輪覆蓋同一筆 | **不會**，結案後也留著 |
| `aiops-incidents` | 事故庫：老師傅確認過的事故、根因分類、怎麼看出來的、指紋 | 回饋網頁、`incident` 指令、`import-incidents` | **不會**，刻意不刪 |
| `aiops-feedback` | 值班回饋：真的／誤報／根因錯／漏報，一句話與抽取結果 | 回饋網頁、`feedback` 指令 | **不會**，刻意不刪 |
| `aiops-tuning` | 偵測的調整與建議（override、排除時段、週期放寬、`rule_suggestion`），含 `status`、誰核准 | `tune`、`adjust`、`feedback suggest`、回饋網頁 | **不會**；`expires_at` 到期只是不再生效，文件還在 |
| `aiops-state` | `trace-merge` 合到哪個時間（就一筆） | `detector trace-merge`，每分鐘 | 每次覆蓋同一筆 |
| `aiops-log-stats` | log 長期統計：每 5 分鐘 × 每個服務 × level × error.type 的筆數 | ES 的 transform，不是 engine | **會**，ILM 400 天 |

欄位細節：`aiops-anomalies`、`aiops-alerts`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning` 見 [aiops-data-reference.md](aiops-data-reference.md)；`aiops-baseline`、`aiops-trace-segments`、`aiops-state` 的 mapping 在 [detector/es.py](../aiops-engine/code/detector/es.py)、[detector/trace.py](../aiops-engine/code/detector/trace.py)、[detector/trace_merge.py](../aiops-engine/code/detector/trace_merge.py)。誰在什麼時候跑見 [schedule.yaml](../aiops-engine/code/configs/schedule/schedule.yaml)。

## ES：收進來的原始資料

| 資料 | 資料流 | 保留 |
|---|---|---|
| log 原文 | `k8s-*` | 30 天 |
| metrics | `metrics-*.otel-*` | 30 秒一點留 14 天 → 5 分鐘一點 → 第 90 天 1 小時一點 → 第 400 天刪；應用的直方圖 `metrics-generic.otel-*` 不降解析度，留 90 天 |
| trace raw（Collector 寫，100% 收） | `traces-generic.otel-*` | 2 天，只是合成前的暫存 |
| trace merged（一個請求一筆，engine 合成） | `traces-merged-<group>-<日期>` | 7 天 |

天數與 ILM 政策的完整說明：[retention-reference.md](retention-reference.md)。

## RCA 報告：從產生到 Grafana

| 步驟 | 寫在哪、讀自哪 |
|---|---|
| 1. 根因分析、LLM 摘要、相似事故 | `investigator alert` 算，寫進 `aiops-alerts` 那一件：`report`（全文，不建索引）、`llm_summary`、`findings`、`similar`（結構化，不建索引） |
| 2. 通知 | Grafana 告警規則讀 `aiops-alerts`，帶 `title`、`summary`、`report_url`、`feedback_url` 這些 keyword 欄位（告警只能帶 labels） |
| 3. 看報告全文 | 通知裡的 `report_url` → 回饋網頁 `/report?alert=…` 從 `aiops-alerts` 讀出來顯示 |
| 4. 儀表板 | Grafana 的資料來源直接讀 `aiops-alerts`、`aiops-anomalies`、`aiops-tuning`、`aiops-feedback`（[datasource.yaml](../grafana/code/datasource.yaml)） |

## Grafana 自己存什麼

| 項目 | 放哪 | Pod 重建後 |
|---|---|---|
| 資料來源、告警規則、通知對象、通知路由、儀表板（`aiops-rca`、`aiops-feedback`、`aiops-learning`） | repo 的 [grafana/code/](../grafana/code/)，部署時 provisioning 載入 | **回來**（在檔案裡） |
| 告警目前的狀態與歷史、使用者、在畫面上手動改的東西 | Grafana 內部 sqlite，**在容器裡，chart 沒掛 PVC** | **清掉** |
| 回饋表單外掛（Business Forms） | `emptyDir` | 由 initContainer 重新下載安裝 |

所以改儀表板、告警規則要改 repo 的檔案，不要在畫面上改。repo 的 chart 是測試用；正式環境 Grafana 是否外接資料庫、告警歷史留多久，**這份沒有資料**（要問管 Grafana 的人）。

## engine 的暫存檔

排程器 pod 有一個 `emptyDir`，放心跳檔（`touch`）、台灣行事曆的快取。Pod 重建就沒了，兩者都能重新產生。

## 沒有備份、會一直長的

| 項目 | 現況 |
|---|---|
| `aiops-feedback`、`aiops-incidents`、`aiops-tuning` | 不自動刪、**沒有 snapshot 備份**，掉了不能重算（[open-issues 第 7 節](pdca/meta/2026-10-open-issues.md)） |
| `aiops-alerts`、`aiops-anomalies`、`aiops-trace-segments` | 沒有 ILM，會一直長。每件告警、每個異常一筆，量不大；`aiops-trace-segments` 隨 API 數 × 段數 × 每 5 分鐘成長最快。正式環境的量沒算 |
| `aiops-baseline` | 每天重寫的衍生資料，重要性較低；重建 ES 後要手動跑一次 `train`，否則等隔天 02:00（期間 `detect` 的基準線這一路靜默停擺，Grafana 的 `aiops-baseline-missing` 會響） |

測試叢集 2026-11-01 的大小（`aiops-alerts` 32 筆 1 MB、`aiops-anomalies` 157 筆 0.5 MB、`aiops-baseline` 1,512 筆 0.5 MB、`aiops-trace-segments` 3,431 筆 1.2 MB）只是量級參考，不代表正式環境。
