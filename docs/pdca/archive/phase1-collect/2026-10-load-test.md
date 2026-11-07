# 收值壓測與邊緣測資（2026-10）

**狀態**：已完成　　**日期**：2026-10-28　　**相關**：分支 `claude/p1-load-77be9d`；Collector 設定 `6c59f67`（本分支 cherry-pick 成 `0a67fd5`）
**模組**：① 收值、③ 存

## Plan：這一次要達到什麼

- **需求**：log、metrics、trace 經 OTel Collector 寫進 ES，不知道量多大開始掉、遇到大行、突發、ES 慢或拒收時會怎樣。
- **目的**：每條線量到「開始掉或開始晚到」的速率，與每種邊緣情況的遺失、重複、延遲、Collector 資源；能重跑。
- **範圍**：本機 k3s 單節點（20 核、7.9GB）、ES 單台（容器 2Gi、heap 1g）。不測多節點、不測正式環境的 ES 大小。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/local-k3s/load/run-stress.sh` | 新增：每個情境一個指令，印每條資料的表、Collector／ES 資源表、Collector log 錯誤 | 原本的 `run-load.sh` 只測 log、要在節點上跑；這支從 Windows 也能跑 |
| `e2e/local-k3s/load/loadgen.py` | 新增：產生 log、OTLP metrics、OTLP trace、Prometheus 高基數序列，與「ES 慢」代理 | 一個 Pod 一份，用 Indexed Job 擴到 50 個 |
| `e2e/local-k3s/load/probe.py` | 新增：在 cluster 裡的 Pod 每 2 秒記 Collector `:8888`、ES 狀態；算遺失、重複、延遲 | 量測不靠 port-forward；log 延遲用 `event.ingested`，metrics、trace 用每秒輪詢 |
| `docs/troubleshooting.md` | 加兩列：log 佇列只增不減、高基數時 `send_failed_metric_points` 暴增 | 兩個都是這次量到的 |

Collector 設定沒改。log、metrics、trace 都在 `6c59f67` 的設定上量（`master` 的設定 2 台 trace-sampler 時 trace 全斷，見 [2026-10-durability.md](2026-10-durability.md)）。

跑法（cluster 只有一個，先拿鎖）：

```bash
source e2e/local-k3s/cluster-lock.sh; cluster_lock "phase1-壓測"
e2e/local-k3s/load/run-stress.sh log          # 其他：metric trace smoke burst bigline pods50 bigtrace cardinality reject slow 5 slow 35 queuefull
cluster_unlock
```

## Check：測試的目的與結果

量測期間背景還有 cluster 上其他應用（其中 `it-mes-prod/mt-edge` 每秒約 37 個 trace，全程都在）。延遲：log 是 `event.ingested − @timestamp`；metrics、trace 是每秒查一次 ES 的新增量（±1 秒）。trace 延遲含 trace-sampler 的 `decision_wait` 5 秒（測試設定）。資源是 node Collector；trace 那幾列括號是 trace-sampler（每台）。metrics、trace 加量在 10-27 23:26～23:53Z 量，當時 log 佇列有積壓（下方「log 寫入停住」）；log 各列與其他情境在 10-28 00:58～01:52Z 量。

| 情境 | 送出 → ES 收到（遺失／重複） | 延遲 p50／p95 | CPU 平均（最高）、記憶體最高 | 結論 |
|---|---|---|---|---|
| log 5,000 行／秒 60 秒 | 300,000 → 300,000（0／0） | 0.4／0.6 秒 | 0.24（0.29）核、485MB | 跟得上 |
| log 10,000 行／秒 | 599,950 → 599,950（0／0） | 0.3／0.3 秒 | 0.40（0.49）核、468MB | 跟得上 |
| log 20,000 行／秒 | 1,199,900 → 1,199,900（0／0） | 97／148 秒 | 0.36（0.99）核、454MB | 不掉、晚到；佇列最高 92 萬行 |
| metrics 5,000 點／秒 | 300,000 → 300,000（0／0） | 0.0／1.0 秒 | 0.43（0.50）核、488MB | 跟得上 |
| metrics 20,000 點／秒 | 1,200,000 → 全收（0／0） | 0.0／0.0 秒 | 0.18（0.21）核、483MB | 跟得上 |
| metrics 50,000 點／秒 | 2,999,880 → 全收（0／0） | 0.6／1.6 秒 | 0.37（0.41）核、500MB | 跟得上 |
| metrics 100,000 點／秒 | 6,000,000 → 全收（0／0） | 0.8／49 秒 | 0.51（0.95）核、528MB | 不掉、晚到 |
| trace 1,000 span／秒 | 60,000 → 全收（0／0） | 6.3／7.3 秒 | 0.11 核（0.03 核、220MB） | 跟得上 |
| trace 3,000 span／秒 | 180,000 → 全收（0／0） | 6.3／8.3 秒 | 0.14 核（0.06 核、305MB） | 跟得上 |
| trace 6,000 span／秒 | 360,000 → 全收（0／0） | 14／23 秒 | 0.17 核（0.10 核、411MB） | 開始晚到 |
| trace 10,000 span／秒 | 600,000 → 全收（0／0） | 60／101 秒 | 0.13 核（0.14 核、494MB） | 不掉、晚 1～2 分鐘 |
| 突發：三條線 10 倍 60 秒 | log 143,996、點 720,000、span 144,000 全收 | log 32／68；點 25／42 秒 | 0.15（0.24）核、418MB | 不掉；突發那分鐘晚約 1 分鐘 |
| 64 KB 的 log 每秒 20 行 | 1,200 → 1,200（0／0），長度全對 | 0.8／1.3 秒 | 0.34（0.40）核、342MB | 不切、不截斷 |
| 1 MB 的 log 每秒 2 行 | 120 → 120（0／0），長度全對 | 0.9／1.4 秒 | 同上 | 不切、不截斷、JSON 有解析 |
| 50 個 Pod 同時（各 40 行＋20 span／秒） | log 239,850、span 120,000 全收 | log 0.7／1.1；span 5.5／6.5 | 0.65（0.89）核、307MB | 跟得上 |
| 一個 trace 1,000 段，每秒 2 個 | 240,000 → 全收；240 個 trace 各 1,000 段 | 5.1／6.1 秒 | 0.09（0.11）核、212MB | 整個 trace 完整 |
| 高基數：20,000 條、每 30 秒全換 | 200,000 → 全收（20 萬條不同序列） | 0.0／0.0 秒 | 0.09（0.13）核、398MB | 不掉；失敗計數 +162,000 是假的 |
| ES 擋寫入 180 秒（平常量） | 三條線全收（0／0） | p95 167～172 秒 | 0.09（0.21）核、414MB | 不掉，恢復後約 30 秒追上 |
| ES 每個回應慢 5 秒 | 三條線全收（0／0） | log 0.8／1.3 秒 | 0.12（0.14）核、445MB | 沒影響 |
| ES 慢 35 秒（> Collector 逾時 30） | 三條線全收（0／0） | 71／176 秒 | 0.15（1.39）核、417MB | 逾時重送不重複（`_id`） |
| 佇列縮小＋ES 擋寫入 180 秒 | log 全收；OTLP 點 173,000 被回 503；span 掉 32,565 | — | 0.08（0.17）核、381MB | 佇列滿：見下方 |
| log 寫入 index 7.77M 筆／1.37GB | 佇列 124 萬行、送出 0 行／秒 | 停住 | 0.10 核、742MB | ES 端瓶頸：見下方 |

- **佇列滿了怎麼樣**（`queuefull`：log、metrics 佇列 20,000 筆、trace 20 批）：
  - log：`file_log` 不讀下去、等佇列有空，**掉 0**（enqueue_failed 3,028 是被退回再讀，不是丟）。
  - metrics：OTLP 推送的被 Collector 回 **503**（173 個請求）；應用 SDK 有重試就不會掉，測試的產生器不重試所以算拒收。Prometheus 抓的直接丟（`err: sending queue is full`）。
  - trace：node Collector 收下了，但 trace-sampler 的佇列滿 → **掉 32,565／72,000 段（45%）**，送的人不知道。
  - 正式設定佇列：log、metrics 2,000 萬筆；trace 10,000 批。ES 擋 180 秒時佇列最高 log 38,392、點 191,078、trace-sampler 185 批，離上限很遠。
- **log 寫入停住**：log 用指紋 `_id` 去重，每筆寫入都在寫入 index 查 `_id`。壓測灌到 `k8s-imc-local` 寫入 index 7.77M 筆／1.37GB，大過 ES 的 page cache：
  - ES hot threads：write 執行緒 cpu 5.9%、其他 94.1%（等 IO，卡在 `PerThreadIDVersionAndSeqNoLookup.getDocID`）；16 個 write 執行緒全滿。
  - 一批 2,000 行 > 40 秒（`_tasks` 看到 `otlp/logs` 跑 42 秒），Collector 30 秒逾時重送，ES 照做一次；`otelcol_exporter_sent_log_records` 30 秒內 +0，佇列 124 萬行不減。資料有進 ES、不重複（`_id`）。
  - `_rollover` 之後（00:50:54Z）追上；之後每個情境開始前寫入 index ≥ 800MB 就 rollover（`ROLLOVER_MB`，01:10:32Z 換過一次，931MB）。上表 log 那幾列都是在 rollover 後、寫入 index < 1GB 時量的。
- **高基數失敗計數**：序列每 30 秒全換，Prometheus receiver 對消失的序列送沒有值的 staleness marker，ES 回 400（`number data point without a value`）。ES 收到的點一筆不少，但 `otelcol_exporter_send_failed_metric_points` +162,000。
- **記憶體**：node Collector 的 RSS 最高 742MB（`otelcol_process_memory_rss`），超過 limit 512Mi 卻沒被 OOM（重啟 0）。推論：RSS 含佇列檔（bbolt）的 mmap，不算進 cgroup 的匿名記憶體；未驗證。
- **沒測到的**：多台節點、正式大小的 ES、ES 剛起來的 401（另一份 [2026-10-es-startup-401.md](2026-10-es-startup-401.md)）、Collector 被 OOM kill（[2026-10-durability.md](2026-10-durability.md)）、應用 SDK 對 503 的重試（產生器不重試）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [③] log 寫入 index 大過 ES 記憶體後寫入停住 | 灌到 7.77M 筆／1.37GB（ES 容器 2Gi） | 全部 log 晚到，佇列不減 | 正式環境定 ILM 依大小 rollover，寫入 index 要放得進 page cache；ES 記憶體一起定 |
| Collector 逾時 30 秒短於 ES 一批的處理時間時，無限重送 | 同上；ES 慢 35 秒 | ES 白做一倍工，追不上 | 逾時要大於 ES 一批 2,000 行的最壞時間，或縮小一批；未驗證 |
| trace-sampler 佇列滿直接丟，送的人不知道 | 佇列縮到 20 批＋ES 擋 180 秒 | trace 掉 45% | 正式 10,000 批，ES 擋 180 秒只用到 185 批；監控 `otelcol_exporter_enqueue_failed_spans` |
| Prometheus 抓的 metrics 佇列滿直接丟 | 同上 | 那段 metrics 缺 | 監控 `otelcol_exporter_enqueue_failed_metric_points` |
| trace ≥ 6,000 span／秒 開始晚到 | trace 加量 | 10,000 span／秒 晚 1～2 分鐘 | 只有一個節點 Collector、2 台 trace-sampler；量大時再測擴 sampler |
| `send_failed_metric_points` 含 staleness marker | 高基數、Pod 刪掉 | 用它判斷遺失會誤報 | 寫進 troubleshooting；告警規則要排除 |

- **決定**：Collector 設定不改。這次量到的掉資料都在佇列刻意縮小時；正式設定下各情境掉 0。
- **下一輪**：log 的 ILM rollover 大小上限、Collector 逾時與 ES 記憶體一起定（正式環境的 ES 規格出來後）。
