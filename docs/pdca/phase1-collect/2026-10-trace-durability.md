# trace 收值線要不要 message queue（2026-10）

**狀態**：已完成（已合進 master，PR #11）　　**日期**：2026-10-24　　**相關**：分支 `claude/collect-time-mq`
**模組**：① 收值（trace 這條線）

## Plan：這一次要達到什麼

- **需求**：log、metrics 送 ES 的佇列寫在節點磁碟（`file_storage`），Collector 重啟、ES 停機都不會掉。trace 這條線有三段只在記憶體：
  1. node Collector → trace-sampler（`loadbalancing/traces`）：記憶體佇列，重試 `max_elapsed_time: 120s`；node Collector 重啟，佇列裡的就沒了。
  2. trace-sampler 本身：等 30 秒（`DECISION_WAIT`）收齊整個 trace 才決定，這段在記憶體。
  3. trace-sampler → ES：記憶體佇列（沒有 `storage`）；ES 停機久了佇列滿就丟，trace-sampler 重啟也沒了。
- 有問題的 trace（錯誤、慢）全留，是 ⑦ 根因分析最需要的；剛好出事時（重啟、擴縮、ES 慢）最容易掉。
- **目的**：實測每一種情況掉多少，比較兩種做法，決定要不要加 message queue：
  - **A. 磁碟佇列**：node 端與 trace-sampler 的佇列加 `file_storage`（trace-sampler 是 StatefulSet，可以掛 PVC）。
  - **B. Kafka**：node Collector 寫 Kafka（依 trace_id 分 partition），trace-sampler 從 Kafka 讀。
- **範圍**：docker 模擬兩層 Collector（跟正式一樣 0.162.0）＋ ES 9.5.5；A 實作並驗證；B 評估並實測。不改正式環境。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `telemetry/collectors/node.yaml` | `loadbalancing/traces`：佇列移到 `protocol.otlp`（每台 trace-sampler 的子 exporter），寫節點磁碟 | 佇列設在外層沒用：資料一進來就移到子 exporter 的記憶體佇列，node 重啟照掉 |
| 同上 | 一直重試（拿掉 120 秒的限制）；外層佇列關掉 | 同上 |
| `telemetry/collectors/sampler.yaml` | 加 `file_storage` extension；三個送 ES 的出口佇列寫磁碟 | ES 停機時 trace-sampler 重啟，記憶體佇列整個默默不見 |
| `telemetry/collectors/trace-sampler.yaml` | 每台加 1Gi PVC（`volumeClaimTemplates`），掛在 `/var/lib/otelcol` | Pod 重啟、搬節點後佇列還在 |
| 同上 | `fsGroup: 10001` | Collector 不是 root，要寫得進去 |
| `docs/trace/README.md`、`docs/pdca/README.md` | 狀態 | — |

測試用的腳本（docker 模擬兩層 Collector、送 trace、製造狀況、數 ES 裡的 trace）放在工作目錄外，沒有進 repo。

## Check：測試的目的與結果

**怎麼測**：
- docker 跑 ES 9.5.5、node 端 Collector、trace-sampler（用正式的 `sampler.yaml`），版本 0.162.0。
- 每秒送 20 個 trace、送 150 秒（共 3,000 個，每個 2 段，ID 都不一樣）。
- trace-sampler 設成全留（`RANDOM_PCT=100`），ES 裡少幾個就是掉幾個。
- 依情境製造狀況，結束後等 90 秒（決定的 30 秒＋佇列送完），用精確的段數算 trace 數。

**設定**：
- **現況**：跟正式設定一樣（node 端記憶體佇列、重試 120 秒；trace-sampler 送 ES 記憶體佇列）。
- **A**：node 端外層佇列加磁碟；trace-sampler 送 ES 加磁碟。
- **A2**：node 端磁碟佇列設在子 exporter；trace-sampler 同 A（**就是這次改的正式設定**）。
- **B**：node 端寫 Kafka（依 trace_id 分 partition），trace-sampler 從 Kafka 讀；trace-sampler 送 ES 跟現況一樣。

| 情境（為了確認什麼） | 現況 | A | A2 | B |
|---|---|---|---|---|
| 對照：沒有任何狀況（測試本身準不準） | 掉 0（600 個） | — | — | — |
| S1：trace-sampler 停 3 分鐘 | 掉 0 | 掉 0 | — | — |
| S1L：trace-sampler 停 400 秒 | **掉 200** | — | 掉 0 | 掉 0 |
| S2：trace-sampler 正常重啟 | 掉 0 | 掉 0 | — | — |
| S2K：trace-sampler 當掉（kill -9）馬上起來 | **掉 600** | — | **掉 620** | **掉 600** |
| S3：trace-sampler 停著時 node Collector 重啟 | **掉 820** | **掉 820** | 掉 0 | 掉 0 |
| S4：ES 停機中 trace-sampler 重啟 | **掉 2,540～2,620** | 掉 0 | 掉 0（兩次） | **掉 2,420** |
| S4，用改過的正式 `sampler.yaml` | — | — | 掉 0 | — |

**結論**：
- 現況 trace-sampler 停 3 分鐘不掉、停 400 秒才掉：`max_elapsed_time: 120s` 不是實際的上限（推論見 Act）。
- 佇列設在 loadbalancing 外層沒用（S3 的 A 跟現況一樣掉 820）；設在子 exporter（A2）才不掉。
- **記憶體佇列在重啟時默默不見**：S4 掉 2,620 個，trace-sampler log 只記到約 420 個（關機時正在送的那幾批），其他沒有紀錄。
- trace-sampler 當掉時，等 30 秒決定的那段只在記憶體，任何佇列都救不了（S2K 四種設定都掉約 600，約 20%）。
- Kafka 沒有多擋住任何情況：入口換成 Kafka 跟 A2 一樣；trace-sampler 送 ES 那段 Kafka 管不到，S4 照掉。
- S4 現況的 log 有 `Permanent error … 401`：ES 剛起來的幾秒回 401，Collector 不重試直接丟；A2 的兩次 S4 沒遇到（後續見 [es-startup-401](2026-10-es-startup-401.md)）。

**沒測到**：
- k3s 上用真的 PVC、真的 headless Service（docker 用的是固定主機名）。
- trace-sampler 兩台以上時，一台當掉的情況。
- 長時間 ES 停機（佇列滿 10,000 批）。
- PVC 的 1Gi 夠不夠（沒量佇列實際佔多少）。

## Act：Check 後沒改善的、還沒解決的

**決定**：不用 Kafka。兩段佇列都寫磁碟（A2）就擋住了 Kafka 擋得住的所有情況，還多擋住「ES 停機中 trace-sampler 重啟」；Kafka 要多維運一套叢集，換不到額外的保護。

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| trace-sampler 當掉（OOM、kill -9）掉約 20% | S2K，四種設定都一樣 | 當掉前 30 秒內的 trace 沒了；出事時最可能 OOM | 盯 trace-sampler 記憶體（`memory_limiter`、`GOMEMLIMIT` 已設） |
| ES 剛起來回 401，Collector 不重試直接丟 | S4 現況的 log | 磁碟佇列也擋不住（永久錯誤會從佇列移除） | 見 [es-startup-401](2026-10-es-startup-401.md) |
| 子 exporter 預設重試約 5 分鐘是推論，未讀程式確認 | S1 停 3 分鐘沒掉 | 不影響 A2（已設一直重試） | — |
| PVC 依賴 cluster 有預設的 StorageClass | 實作時 | 沒有的話 trace-sampler 起不來（Pending） | 部署前確認；k3s 有 local-path |
| PVC 1Gi 夠不夠沒量 | 沒測長時間 ES 停機 | 佇列滿了就丟 | 正式環境看 `/var/lib/otelcol` 用量 |

- trace-sampler 兩台以上時，一台掉只影響它負責的那部分。
- 401：log、metrics 線用同樣的 `otlp_http` exporter，推論也會；修法在 ES 入口，三條線一起受益。
