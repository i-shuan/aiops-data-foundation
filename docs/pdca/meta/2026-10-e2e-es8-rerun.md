# e2e 在 ES 8.19 重跑（2026-10）

**狀態**：進行中　　**日期**：2026-10-31　　**相關**：分支 `claude/e2e-es8-fixes`；前一輪 [2026-10-metrics-es8-shape.md](2026-10-metrics-es8-shape.md)
**模組**：①②③④⑦

## Plan：這一次要達到什麼

- **需求**：ES 從 9.5.5 釘成 8.19.23 後，`e2e/local-k3s/` 底下的測試多數沒在 8.19 跑過，不知道哪些是測試寫死了 ES 9 的行為、哪些是真的問題。
- **目的**：每支測試在 k3d＋ES 8.19.23 上跑一次，失敗的分清是「測試的假設」、「測試環境」還是「產品的問題」。
- **範圍**：先把能跑的跑完；只修測試與腳本的假設，產品的問題記在 Act，不在這份修。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/local-k3s/metrics_trace_check.py` | 新增 `to_ms()`，`metric_docs()` 與 trace 時間一律轉成毫秒 | 8.19 的 `elasticsearch` 寫入元件存的時間格式跟 ES 9 的 OTLP 入口不同：metrics 是 ISO 字串、trace 是數字字串（例如 `"1793413555561.0"`）。原本假設是數字，`/` 運算直接 `TypeError` |
| `e2e/local-k3s/run-rules-e2e.sh` | 節點磁碟那項預期的節點名稱改成從 cluster 讀 | 原本寫死 Rancher Desktop 的 `vm`，k3d 的節點叫 `k3d-aiops-server-0` |

## Check：測試的目的與結果

環境：k3d（k3s v1.30.14）、cluster 內 ES 8.19.23（3Gi，原本 2Gi 被 OOMKilled）、Collector 用 master 的設定。腳本逐支跑，各自記 log。

| 測試 | 結果 | 失敗的原因 |
|---|---|---|
| `run-metrics-e2e.sh` | 46／46 | — |
| `run-keys-e2e.sh` | 84／84 | — |
| `run-trace-id-e2e.sh` | 15／15 | — |
| `run-rca-e2e.sh` | 11／11 | 第一次因 `rca-lib.sh` 的換行是 CRLF，busybox 解析失敗（工作目錄的問題，已轉 LF，不是 repo 的內容） |
| `run-rules-e2e.sh` | 11 過、1 項失敗 | 前一支壞掉的 rca 留下 `rca-mes-api`（錯誤率 100%）被規則抓到，屬測試互相干擾；節點磁碟那項已修（見 Do）。時鐘偏移 137ms 那筆是 k3d 節點本身 |
| `run-metrics-trace-e2e.sh` | 16／16 | 第一次 `TypeError`（見 Do），修完用 `CHECK_ONLY` 重跑檢查，資料沿用同一輪 |
| `run-trace-baseline-e2e.sh` | 9／9 | — |
| `run-metrics-scenarios.sh keyfail` | 4／4 | — |
| `run-metrics-scenarios.sh label` | **無效** | 腳本第 73 行 `value too great for base`（`date` 字串被當成 8 進位數字），印出「全部通過」但實際沒驗到東西 |
| `run-metrics-scenarios.sh outage` | **1 過 3 失敗** | 見 Act |

- **沒跑到**：`run-dotnet-e2e.sh`、`run-metrics-cases-e2e.sh`、`sdk-inject`、`run-peer-e2e.sh`、`run-peer-edge-e2e.sh`、`kafka`、`tail-sampling`、`run-schedule-e2e.sh`、`run-es-2node.sh`。跑到 `dotnet` 時 Docker Desktop 停了（17:23 已不在跑），整批中斷。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [①] ES 停 180 秒、**中途重啟兩個 Collector**，IT 的 metrics 整段 0 筆、IMC 缺 1 筆 | `run-metrics-scenarios.sh outage`（k3d、8.19.23） | 停機期間的資料沒補進來：Collector 重啟時佇列裡的資料遺失。需要確認是 `elasticsearch` 寫入元件的佇列沒持久化，還是這輪 ES 在 k3d 上的特殊情況（ES 記憶體曾被 OOMKill） | 單獨重跑一次、看 Collector 重啟後佇列檔（`file_storage`）還在不在、log 有沒有 `Dropping data` |
| `run-metrics-scenarios.sh label` 檢查 `value too great for base` 卻回報通過 | 讀 log 發現 | 這支測試沒在驗證，不能信它的「全部通過」 | 修第 73 行的時間算術，並讓腳本遇到這種錯誤時失敗 |
| 測試之間互相干擾（`rules` 抓到 rca 留下的服務） | `run-rules-e2e.sh` | 單獨跑會過；連著跑會誤判 | 跑完每支清掉自己的 namespace，或 `rules` 的「其他沒被抓」排除 `it-mes-prod` 的 `rca-*` |
| Docker Desktop 在跑到一半時停掉 | 17:23 檢查時 `dockerDesktopLinuxEngine` 管道不存在 | 後面 9 支沒跑；k3d 一起消失，需要重建 | 重啟 Docker Desktop、`create-cluster.sh` 重建後從 `dotnet` 接著跑 |
