# 收值線故障時會不會掉、會不會重複（2026-10）

**狀態**：已完成（還沒合）　　**日期**：2026-10-28　　**相關**：分支 `claude/p1-durability-5194cd`；接續 [trace-durability](2026-10-trace-durability.md)、[es-startup-401](2026-10-es-startup-401.md)
**模組**：① 收值（三條線：node Collector、cluster Collector、trace-sampler → ES）

## Plan：這一次要達到什麼

- **需求**：收值線（log、metrics、trace）在 Collector 當掉、重啟、換版、ES 停機或限流時，會不會掉資料、會不會重複，之前只在 docker 量過一部分；[trace-durability](2026-10-trace-durability.md) 寫「trace-sampler 當掉仍掉約 20%」，沒有說清楚是什麼的 20%。
- **目的**：在 k3s 上（跟正式一樣的 DaemonSet、StatefulSet＋PVC、兩台 trace-sampler）每個故障情境量出：log 序號遺失／重複、metrics 空檔、trace 遺失；把「約 20%」換成確切條件；找到明顯錯誤就改、改了重測。
- **範圍**：本機單節點 k3s（Rancher Desktop）、單節點 ES 9.5.5、Collector 0.162.0。不含 ES 多節點（見 [es-startup-401](2026-10-es-startup-401.md)）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `telemetry/collectors/node.yaml` | `loadbalancing/traces` 的磁碟佇列從子 exporter 移回外層；子 exporter 不排隊、不重試 | 子 exporter 全部共用一個佇列檔，只有一台 trace-sampler 收得到，其他台的 trace 全被拒（Check 第 1 列） |
| 同上 | 拿掉 `batch` processor（log、trace 三條 pipeline） | 它在佇列前面、在記憶體裡；Collector 被 kill -9 掉最後約 1 秒（修正後最多 0.2 秒） |
| `telemetry/collectors/common.yaml` | `otlp_http/es` 的佇列加 `batch`（1 秒或 2,000 筆送一次） | 原本由 batch processor 湊批；改在磁碟佇列裡湊，當掉不掉 |
| 同上 | 佇列上限改用筆數算：2,000 萬筆（`sizer: items`） | 沒有 batch processor 後每個請求只有約 40 行，用請求數算的上限 33 分鐘就滿；2,000 萬筆＝原本 10,000 批 × 2,000 行 |
| `e2e/local-k3s/load/run-durability.sh`（新） | 12 個故障情境，每個自己拿 cluster 鎖、做完還原，印結果表 | 可重跑；破壞性動作（停 ES、刪 hostPath、PVC 換小 tmpfs）中途失敗也會還原 |
| `e2e/local-k3s/load/trace-gen.py`（新） | trace 產生器：每個 trace 帶序號，重試照 Java SDK 預設 | 數得出哪些 trace 不見、不見的是哪個時間點；區分「應用端丟」和「收值線丟」 |
| `e2e/local-k3s/load/generator.yaml` | 收到 SIGTERM 也印出送了幾筆、開始時間 | drain 時 Pod 被趕走，還要知道送了幾筆、每個序號的時間 |
| `docs/troubleshooting.md`、`docs/trace/README.md`、`docs/pdca/README.md` | 結論 | — |

## Check：測試的目的與結果

**怎麼測**（`e2e/local-k3s/load/run-durability.sh <情境>`）：

- log：每秒 200 行（es-429 是 3,000 行）、每行帶序號；ES 裡數不同序號有幾個、有沒有重複。
- trace：每秒 20 個（pvc-full 是 100 個），每個 2 段、帶序號；trace-sampler 全留（測試環境 `RANDOM_PCT=100`）。
- metrics：節點 CPU（kubeletstats，每 30 秒一點），數點數、最大間隔。
- 遺失的時間點＝序號換算成產生時間，減掉故障那一刻（t=0）。
- 「應用端丟」：SDK 重試 5 次（約 8 秒）都失敗、自己丟的，Collector 沒收到。

**設定**：「原設定」＝master；「修正後」＝這次的 node.yaml、common.yaml。沒標的都是修正後。

| 情境 | log 遺失／重複 | trace 遺失 | metrics 最大空檔 | 結論 |
|---|---|---|---|---|
| 原設定：node Collector 重啟一次（trace-sampler 不動） | — | 一台 trace-sampler 的那份**全部被拒，一直不會好** | — | **錯誤**：佇列檔共用，第二個子 exporter 啟動逾時 |
| 原設定：node Collector kill -9 | 240／0（t=-1.1～0 秒） | 4,799／4,799（全斷） | 36 秒 | batch processor 在記憶體；trace 同上 |
| node Collector kill -9 | 0／0、40／0（兩次；t=-0.2～0 秒） | 0 | 45～52 秒 | 從最後約 1 秒降到最多一次讀檔的量（0.2 秒） |
| node Collector `delete --grace-period=0 --force` | 0／0 | 0 | 49 秒 | 不掉、不重複 |
| node Collector OOM（limit 150Mi＋每秒 5,000 行） | 0／0 | 0 | 50 秒 | 沒有被 OOM 殺：`memory_limiter` 先擋，讀檔暫停 |
| node Collector OOM（limit 80Mi＋每秒 5,000 行，3 分鐘 OOMKilled 5 次） | **33,192**／0（t=+11.5～+178 秒） | 0（應用端丟 3,508） | 225 秒 | 連續 OOM 進 CrashLoopBackOff，停太久，log 檔被輪替（推論，見下） |
| trace-sampler kill -9（decision_wait 5 秒） | 0／0 | 57／4,799（t=-6.3～-0.3 秒） | 30 秒 | 掉「當掉前 decision_wait＋1 秒」那台的 trace |
| trace-sampler kill -9（decision_wait 30 秒，正式值） | 0／0 | 272／4,799（t=-31.5～-0.5 秒） | 30 秒 | 同上 |
| trace-sampler 兩台 rollout restart（IP 都變） | 0／0 | 0 | 30 秒 | 正常停不掉（推論：停之前先把等著的決定完） |
| trace-sampler 停 100 秒，中間 node Collector 重啟 | 0／0 | 0（應用端丟 20） | 44 秒 | 節點磁碟佇列撐過 node 重啟 |
| trace-sampler PVC 滿（4MB，ES 停 300 秒） | 0／0 | 10,346／41,999（t=+99 秒 PVC 滿～ES 起來） | 30 秒 | 滿了之後那台的 trace 全掉；log `no space left on device`、`Rejecting data`（再測一次：掉 6,784） |
| 單節點 drain（趕走產生器、trace-sampler-0，node Collector 停 30 秒） | 0／0 | 0 | 未量（見下） | 被趕走的 Pod 最後幾行也收到 |
| ES 回 429（write thread pool 1 條、佇列 1；每秒 3,000 行） | 0／0（719,970 行） | 0／4,799 | 30 秒 | 整批回 429，Collector 重試；只是晚到 |
| ES 停 10 分鐘 | 0／0（155,968 行） | 0／15,599 | 30 秒 | 不掉、不重複 |
| ES 停 30 分鐘 | 0／0（395,966 行） | 0／39,599 | 30 秒 | 不掉、不重複 |
| ES 停 10 分鐘（拿掉 batch processor 後） | 0／0（155,994 行） | 0／15,599 | 30 秒 | 不掉、不重複 |
| ES 停 10 分鐘（佇列改用筆數算後，最後的設定） | 0／0（155,978 行） | 0／15,599 | 30 秒 | 不掉、不重複；佇列 log 125,788 筆、metrics 16,440 筆 |
| node Collector 停 30 秒（保留 `/var/lib/otelcol/node`） | 0／0 | 0（應用端丟） | 62 秒 | 接著讀，不掉 |
| node Collector 停 30 秒，停的時候刪 `/var/lib/otelcol/node` | 6,432／0（t=0～+32 秒） | 0（應用端丟） | 64 秒 | 停機那段的 log 全掉（從檔尾讀） |
| 三種 Collector 一起換版 0.162.0 → 0.161.0 → 0.162.0 | 0／0 | 0 | 50 秒 | 換版不掉；metrics 少 1 點 |

- 「hostPath 刪掉」那列的應用端丟：兩次停機合計 1,066 個 trace（node Collector 停 30 秒，SDK 重試 8 秒就放棄）。
- drain 的 metrics：產生器被趕走後量測窗口就結束了；node Collector 停 30 秒的空檔同上一列（約 62 秒）。
- ES 停 10、30 分鐘前兩列是改 loadbalancing 之後、拿掉 batch processor 之前量的；最後一列是這次 commit 的設定。
- ES 回 429：ES 對整個請求回 429（不是 200 裡面部分被拒），Collector 記 `Will retry`，node 356 次、trace-sampler 159 次。重送時已寫入的回 `CONFLICT`（`_id` 擋掉重複），不算遺失。限流期間只會越積越多，ES 改回來後幾分鐘補完。
- 80Mi 的 OOM：遺失從「最後一次存讀檔位置」開始、到新檔開頭結束（推論：kubelet 每 10MiB 輪替一次 log 檔，`file_log` 只讀 `*.log`，被改名的舊檔沒讀完的就沒了；每秒 200 行約 2 分鐘輪替一次）。150Mi 時 `memory_limiter` 擋得住，沒有被殺。

**trace-sampler 當掉掉多少**（取代 [trace-durability](2026-10-trace-durability.md) 的「約 20%」）：

| 項目 | 結果 |
|---|---|
| 掉哪些 | 當掉那台，第一段在當掉前「decision_wait＋約 1 秒」內到的 trace |
| 掉幾個 | 每秒 trace 數 ÷ trace-sampler 台數 ×（decision_wait＋1 秒） |
| 對照實測 | 20 個/秒、2 台：5 秒 → 預估 60、實測 57；30 秒 → 預估 310、實測 272 |
| 原本的「約 20%」 | docker 單台、20 個/秒 × 30 秒 = 600，剛好是那次 3,000 個的 20%；跟比例無關 |
| 正常停（rollout restart） | 0（推論：停之前先把等著的決定完） |

**node Collector 的佇列共用一個檔**（原設定）：

- 證據：`/var/lib/otelcol/node` 只有一個 `exporter_otlp_grpc_traces_traces`；log 有 `failed to start new exporter for endpoint ... "error": "timeout"`，之後那台每批都 `couldn't find the exporter for the endpoint`、`Rejecting data`。
- 只重啟 node Collector、trace-sampler 不動也會（15:55 UTC 重現）：一台載入佇列、另一台逾時。
- trace-sampler 換 IP 時更糟：舊 IP 的子 exporter 佔著檔，新的兩台都起不來，trace 全斷（15:15～16:26 UTC 整個 cluster 沒有 trace，部署修正後恢復）。
- 舊 IP 子 exporter 關掉時，它佇列裡的直接丟（`interrupted due to shutdown`、`Dropping data`）。
- [trace-durability](2026-10-trace-durability.md) 在 docker 只有一台 trace-sampler、主機名固定，所以沒遇到。

**佇列要多大**：

| 項目 | 實測 | 換算 |
|---|---|---|
| trace-sampler PVC | 每秒 50 個 trace（2 段）時每秒長 31KB | 約 0.3KB/段；1Gi 約 330 萬段 |
| node 佇列檔（ES 停 30 分鐘，batch processor 還在時） | 285MB（log 每秒 200 行，36 萬行＋metrics） | 約 0.8KB/行 |
| node 佇列檔（ES 停 10 分鐘，12 萬行） | 用請求數算時 96MB；改用筆數算後 304MB | 後者約 2.5KB/行；為什麼變大沒查（見 Act） |
| node 佇列請求數（ES 停 10 分鐘、每秒 200 行） | log 3,000 個、metrics 100 個 | 沒有 batch processor 時每個請求約 40 行；用請求數算上限（10,000）33 分鐘就滿，所以改用筆數算（2,000 萬筆） |

- PVC 的檔案只長不縮：`du` 量到的是歷來最大，不是現在用多少（10、30 分鐘兩次都是 74.7MB）。

**沒測到的**：

- 真的多節點 drain：Pod 搬到別的節點；local-path 的 PVC 綁節點，trace-sampler 只能等原節點回來。
- 節點整台不回來：那台 `/var/lib/otelcol/node` 裡還沒送的佇列就沒了（推論）。
- ES 多節點時的 429、滾動重啟（見 [es-startup-401](2026-10-es-startup-401.md)）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| trace-sampler 當掉掉「decision_wait＋1 秒」那台的 trace | trace-sampler kill -9 | 正式值 30 秒；出事時最可能 OOM | 盯記憶體（`memory_limiter`、`GOMEMLIMIT` 已設）；正常停不掉 |
| node Collector 停的期間，應用端會丟 trace | hostPath 那兩次停 30 秒、node 重啟 | SDK 預設重試 5 次（約 8 秒）就放棄 | 換版排離峰；不改 SDK 預設 |
| 刪掉 `/var/lib/otelcol/node` 會掉停機那段的 log | 停的時候刪 | 讀檔位置沒了，從檔尾讀 | 已在 troubleshooting；hostPath 不要刪 |
| PVC 滿了 trace 直接丟 | PVC 4MB | 1Gi 約 330 萬段 | 正式環境依 trace 量估 ES 最長停機時間 |
| node Collector 停超過「10MiB ÷ 每秒寫入量」，那段 log 會掉 | limit 80Mi 連續 OOM | 被輪替的舊檔沒讀完 | 正式環境 limit 不要低於平常用量的 2 倍；量大服務照 troubleshooting 調大 `container-log-max-size`；要不要讓 `file_log` 也讀輪替後的檔，下一輪測 |
| 佇列改用筆數算後，佇列檔變大約 3 倍 | ES 停 10 分鐘，96MB → 304MB | 節點磁碟用得比預期多 | 正式環境看 `/var/lib/otelcol/node` 用量；下一輪查原因（每筆的存法） |
| node Collector kill -9 仍可能掉最後 0.2 秒的 log | 修正後 2 次 kill -9，1 次掉 40 行 | 一次讀檔的量 | 不再處理 |
| metrics 重啟少 1 點 | 每個重啟、換版情境 | 30～60 秒 | 已知（troubleshooting） |

- **決定**：node 端 trace 佇列放外層、子 exporter 不排隊；batch 改在出口佇列裡湊。不加 Kafka（理由同 [trace-durability](2026-10-trace-durability.md)）。
- **下一輪**：多節點 cluster 上重跑 drain、節點不回來。
