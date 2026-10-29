# Troubleshooting

> **類型：操作 How-To**

只寫結論：症狀 → 原因 → 做法。依區塊分。

## 收值（log、metrics、trace 共用）

| 症狀 | 原因 | 做法 |
|---|---|---|
| key 作廢或打錯期間，資料整批不見、補不回來 | OTLP 出口只對連不上、逾時、429、502、503、504 重試；401 當永久錯誤直接丟，沒有設定可改 | 換 key 一律用 `telemetry/scripts/rotate-writer-key.sh`（先發新的、換進 Secret、Collector 換用後才作廢舊的；Collector 從檔案讀 key，不用重啟）。監控 Collector `:8888/metrics` 的 `otelcol_exporter_send_failed_metric_points`、`otelcol_exporter_send_failed_log_records`，大於 0 就是有資料被丟 |
| ES 限流（429）時資料晚到好幾分鐘 | ES 對整個請求回 429，Collector 退避重試（最多 30 秒一次） | 不會掉（實測每秒 3,000 行、ES 拒絕 902 次，掉 0）；只是晚到 |
| Collector log 出現 `Partial success ... dropped` | ES 拒收其中幾筆（例如欄位型別衝突，400），那幾筆丟掉 | 監控這行。`CONFLICT`（409）是重送的被 `_id` 擋掉，屬於正常去重複，不算遺失 |
| 連線逾時時資料不見 | `elasticsearch` 寫入元件逾時直接丟 | 用 OTLP 出口，不要換回 `elasticsearch` 寫入元件（[metrics/README.md](metrics/README.md#為什麼用-otlp-送不用-elasticsearch-寫入元件)） |
| 兩個 cluster 的資料混在一起 | `CLUSTER_NAME` 沒改（它是每筆的 `k8s.cluster.name`，metrics 的資料流名稱裡沒有它） | 每個 cluster 部署時一定要改 |
| 改了部門，資料還進舊的 group | dept 清單有兩處：`telemetry/groups.yaml` 和 `common.yaml` 的 `transform/group` | 兩處一起改。改 namespace 的 `dept` label 後 0～3 秒生效，五種資料同時切；Prometheus 要等下一次抓（最多 30 秒） |
| 平台元件（kube-system 等）的資料在 unassigned | 這些 namespace 沒有 `dept` | 正常，跟 metrics 的兜底 tenant 同義 |
| Collector 被 OOM 或 kill -9 之後，少了最後約 1 秒的 log、trace | `batch` processor 在磁碟佇列前面、在記憶體裡；交給它時讀檔位置已經往前 | 不用 `batch` processor，在出口佇列裡湊批（`common.yaml` 的 `sending_queue.batch`，已改）。實測修正後 kill -9 掉 0～40 行（最多 0.2 秒） |
| node Collector 停的期間 trace 變少，Collector 沒有錯誤 | 應用的 SDK 連不上就重試 5 次（約 8 秒）後自己丟，Collector 收不到 | 換版、維護排離峰；node Collector 停 30 秒、每秒 20 個 trace 時應用端丟約 530 個 |
| 新 Pod 剛起來的第一筆 OTLP 資料在 unassigned、沒有 Pod 名字 | Pod 的 IP 還沒進 Collector 的 Pod 清單（實測每個新 Pod 約 1 筆；stdout log、Prometheus 不會） | 預期行為，不補 |
| `service.name` 不是應用自己設的 `OTEL_SERVICE_NAME` | 有 Pod label `app.kubernetes.io/name` 時以 label 為準，三種資料才對得起來 | 要改名就改 label（chart 的 `name`） |
| Pod 刪掉後 Collector 印 `Dropping data`，訊息是 `number data point without a value` | Prometheus 收到 Pod 消失的標記（staleness marker），ES 不收這種點 | 不是遺失；看 `send_failed_metric_points` 時扣掉這種 |
| 正式環境 kubelet 連線不驗憑證 | `kubeletstats` 設了 `insecure_skip_verify: true` | kubelet 憑證由內部 CA 簽的話改成驗證 |

## log

| 症狀 | 原因 | 做法 |
|---|---|---|
| ES 拒收時 log 量暴增 | Collector 收到自己印的錯誤再送一次，形成放大迴圈 | `node.yaml` 的 `file_log` 排除 `otel-collector` namespace，不要拿掉 |
| log 停在 `logs-generic.otel-*`，沒進 `k8s-*` | 改了 `k8s-logs-otel`、`logs-otel@custom` 之後，舊的 backing index 還在用 | `elasticsearch-setup.sh` 會自動檢查並 rollover；手動改的話要 rollover `logs-*.otel-*` |
| 目的地的 default pipeline 沒跑 | 改了 `_index` 後 ES 不會自動跑目的地的 default pipeline | `k8s-logs-otel` 最後自己呼叫 `k8s-logs`，不要拿掉 |
| ES 停很久之後 log 有缺 | 佇列在節點上（`/var/lib/otelcol/node`），log、metrics 各上限 2,000 萬筆（`common.yaml` 的 `queue_size`）；實測佇列檔約 0.8～2.5KB/行 | 量大或可能停很久時先估算磁碟與上限；盯 `:8888` 的 `otelcol_exporter_queue_size` ÷ `otelcol_exporter_queue_capacity` |
| Pod 重建後 log 重複或中間缺 | 讀檔位置也記在 `/var/lib/otelcol/node`；目錄被刪就從檔案尾巴讀，停機那段全掉（實測停 30 秒掉 6,432 行；保留目錄掉 0） | 這個目錄要用 hostPath，不要刪 |
| 第一次部署時舊的 log 沒收 | `start_at: end`，已經存在的檔從尾巴讀 | 預期行為，不補收 |
| node Collector 連續 OOM 之後少了一大段 log | 一直重啟（CrashLoopBackOff）停太久，log 檔被 kubelet 輪替，`file_log` 只讀 `*.log`，舊檔沒讀完的就沒了（推論；實測 3 分鐘掉 33,192 行） | memory limit 不要低於平常用量的 2 倍（`memory_limiter` 在 150Mi 時擋得住，80Mi 擋不住） |
| 量大的服務掉 log | kubelet 預設 10MB 就輪替壓縮，Collector 沒讀完的那段會掉 | 平台端把 kubelet 的 `container-log-max-size` 調大（例如 50Mi）。Collector `poll_interval` 0.2 秒，每秒 5,000 行不會掉 |
| log 有 trace_id，用 `trace_id` 卻查不到 trace | 值不是 32 個 hex（印錯、截斷、GUID 帶減號）；pipeline 不設 `trace.id` | 看 `app.trace_id_invalid`，改應用的 log 設定 |
| 開了 `otel.logs.exporter: otlp`，同一行 log 有兩筆 | stdout 那份（檔案）＋ agent 直送那份；內容指紋不同，不會去重 | 預期行為；查詢時用 `log.source: otlp`（或 `file`）只取一份 |
| 用 Datadog 的服務 log 綁不到 trace | `dd.trace_id` 是十進位，對不到 OTel 的 trace | 換 OTel agent；`dd.trace_id` 留在 `app` |
| node Collector 的 log 佇列只增不減、`otelcol_exporter_sent_log_records` 不動；Collector log 一直 `/_otlp/v1/logs ... context deadline exceeded` | log 用指紋 `_id` 去重，每筆寫入都要在寫入 index 查 `_id`。index 大過 ES 的 page cache 後每筆都讀磁碟（實測 7.77M 筆／1.37GB、heap 1g、容器 2Gi：write 執行緒 94% 在等 IO，一批 2,000 行 > 40 秒）；超過 Collector 逾時 30 秒 → 逾時重送，ES 照做一次，佇列永遠追不上（資料有進 ES、不重複） | 立即：對寫入的 `k8s-*` data stream `_rollover`（新 index 小，查 `_id` 回到記憶體）。長期：ILM 依大小 rollover（寫入 index 要放得進 page cache）、ES 記憶體、Collector 逾時要大於 ES 一批的處理時間（[2026-10-load-test.md](pdca/phase1-collect/2026-10-load-test.md)） |
| Java stack trace 變成很多筆 | 純文字 log 一行一筆 | 標準格式是 JSON（stack trace 在一個欄位）；沒照標準的服務 AIOps 效果較差 |
| 節點用 Docker 當 runtime 的 cluster 收不到 log | log 檔在 `/var/lib/docker/containers` | 另外掛這個目錄。RKE2、k3s 是 containerd，不需要 |

## metrics

| 症狀 | 原因 | 做法 |
|---|---|---|
| ES 重啟後少了停機期間的 metrics，Collector 卻沒報錯 | TSDS 每個 backing index 只收一段時間範圍（預設往後 9 分鐘），範圍外的進 failure store（`.fs-metrics-*`），回給 Collector 的是成功 | `metrics-otel@custom` 把 `look_ahead_time` 設 2 小時（`elasticsearch-setup.sh` 會建，只對新的 backing index 生效，既有的要 rollover 一次）。監控 failure store：`curl -s -u elastic:<password> "https://<es>:9200/.fs-metrics-*/_count?expand_wildcards=all&ignore_unavailable=true"` |
| 重啟 Collector 後少 1～2 個點 | 舊的停、新的還沒開始抓之間（約 30～60 秒）沒人抓，事後補不回來；已經抓到的不受影響 | 換版、改設定排在離峰 |
| `otelcol_exporter_send_failed_metric_points` 暴增、Collector log `Dropping data ... 400 ... number data point without a value`，ES 的點卻沒少 | 序列消失時（Pod 刪掉、label 值換掉）Prometheus receiver 送出沒有值的 staleness marker，ES 回 400 不收。不是真的遺失（實測序列每 30 秒全換：ES 收到 200,000／送出 200,000，失敗計數 +162,000） | 用 `send_failed_metric_points` 判斷遺失時要扣掉這種；看 Collector log 的原因是不是 `without a value` |
| K8s 物件狀態的資料重複 | cluster 那一份的 Collector 多於 1 個副本 | replicas 一定是 1，換版用 `Recreate` |
| 應用的 `service.name` 變成 `pods` | Prometheus receiver 把抓取工作的名稱填進 `service.name`，讀 Pod label 的那一步不覆蓋 | `common.yaml` 先清掉再讀，不要改順序（測試有一項擋這個） |
| Spring 服務的 HTTP 延遲、錯誤率規則、baseline 都沒有 | Micrometer 的名字是 `http_server_requests_seconds`、label `uri`，不在 node Collector 的改名對照裡，`quality.metric_name_legacy` 也不盯 | 改用 OTel Java agent 的指標，或在 `node.yaml` 加一組對照（[metrics-trace](pdca/phase1-collect/2026-10-metrics-trace.md)） |
| 容器重啟、Pod 重建後少一段請求數（重啟約 1 次抓取、換 Pod 最多約 2 次） | 直方圖換成增量時，第一次抓取沒有「上一次」可比，整筆丟掉；換 Pod 還少舊 Pod 最後一次抓取後那段。不會有負值或尖峰 | 預期行為；算錯誤率、p95 不受影響 |

## trace

| 症狀 | 原因 | 做法 |
|---|---|---|
| trace 進 unassigned | 應用連到別的節點的 Collector，那裡認不得這個 Pod | Service 一定要 `internalTrafficPolicy: Local` |
| trace 幾乎都沒進 ES；node Collector log 一直 `couldn't find the exporter for the endpoint` | `loadbalancing/traces` 的磁碟佇列設在子 exporter：每台 trace-sampler 的子 exporter 共用同一個佇列檔，只有一個鎖得到，其他的啟動逾時（`failed to start new exporter ... timeout`），那台的 trace 從此全被拒 | 佇列設在外層、子 exporter 的 `sending_queue`、`retry_on_failure` 關掉（`node.yaml` 已改）。只有一台 trace-sampler 時不會出現 |
| trace-sampler 當掉（OOM、kill -9）後少了一段 trace | 等著決定的 trace 在記憶體 | 掉「每秒 trace 數 ÷ 台數 ×（decision_wait＋1 秒）」，正式值約 31 秒；正常停（rollout）不掉。盯 trace-sampler 記憶體 |
| ES 停機時 trace 不見，trace-sampler 沒重啟，log 有 `no space left on device`、`Rejecting data` | trace-sampler 的 PVC 滿了，佇列寫不進去就丟 | 約 0.3KB/段，1Gi 約 330 萬段；依「保留下來的段數/秒 × ES 最長停機時間」估 PVC。PVC 的檔只長不縮，`du` 看到的是歷來最大 |
| 用 hostNetwork 的應用 trace 進 unassigned | 連線來源是節點 IP，對不到 Pod；OTLP 送的 log、metrics 也一樣（stdout log、Prometheus 認得出） | 應用不能用 hostNetwork |
| ES 空間不夠 | trace 量通常比 log 大；尾端取樣後約剩 15% | 全廠上線前估算；不要在應用調低 `otel.traces.sampleRatio` |
| Java 自己 `new Thread` 印的 log 沒有 trace_id | agent 不會把 context 帶到自己開的執行緒 | `Context.current().wrap(...)`（[tracing.md 2-4](../sdk/standards/tracing.md#2-4-自己管理的執行緒)） |
| 背景排程的 log 沒有 trace_id | 沒有上游請求，log 當下沒有 span | 預期行為；要查就印業務 ID |
| log 的 trace_id 在 ES 查不到 trace | 上游送的 `traceparent` 結尾 `-00`（未取樣），整條都不送 span | 找送 `-00` 的上游 |

## .NET

| 症狀 | 原因 | 做法 |
|---|---|---|
| Collector 抓不到 .NET 的 metrics | 自動埋點的 Prometheus 端點只聽 localhost | 改成推（OTLP），chart 已經這樣設 |
| 沒有 `http.server.request.duration` | 自動埋點預設沒開 ASP.NET Core 的 HTTP 指標 | `OTEL_DOTNET_AUTO_METRICS_ADDITIONAL_SOURCES=Microsoft.AspNetCore.Hosting,…`（chart 已加） |
| log 量暴增 | ASP.NET Core 預設每個請求印 5 行 Information | `Logging__LogLevel__Microsoft.AspNetCore=Warning`（chart 已設） |
| .NET Kafka 消費端的 log 沒有 trace_id | 自動埋點的 `receive` span 在 `Consume()` 回來時就結束 | 不改程式時印業務 ID；要串起來得在消費端改程式（[metrics/dotnet.md](metrics/dotnet-how-to.md#kafkaconfluentkafka)） |
| .NET 的 log 對不到 trace | trace_id 在 `Scopes` 裡，不在最外層 | 開 `IncludeScopes`；中央 pipeline 會從 `Scopes` 拿 `TraceId`、`SpanId`，也認得 Serilog compact JSON。都能用環境變數開（`Logging__Console__*`），不用改程式 |
| 告警顯示的連線池名稱是一串連線字串 | Npgsql 用連線字串當名稱（密碼已拿掉） | 能改程式就用 `NpgsqlDataSourceBuilder` 設 `Name` |
| 執行緒池飢餓時佇列長度一直是 0 | .NET 6 以後偵測到同步等 Task 會馬上多開執行緒，不排佇列 | 看「執行緒 ÷ CPU 核數」（`thread_pool_growth`） |
| 記憶體洩漏時 Pod 沒被重開，但每個請求回 500 | GC heap 到上限（容器的 75%）後程式不結束，一直丟 OutOfMemoryException；工作集停在 90% 上下，容器記憶體規則不一定觸發 | 靠 `dotnet.out_of_memory`（.NET 9）、`dotnet8.exceptions_burst`（.NET 8）、Gen2 GC 的規則；.NET 沒有 ExitOnOutOfMemoryError 的開關 |
| .NET 9 的 OOM 規則沒觸發 | `error.type` 是短名稱 `OutOfMemoryException` | 規則用短名稱 |
| .NET 8 沒有例外種類 | .NET 8 只有例外總數 | .NET 8 看「例外暴增」（5 分鐘 ≥ 500 次） |
| .NET 8 和 9 的規則互不適用 | runtime 指標名稱、維度完全不同（`process.runtime.dotnet.*` vs `dotnet.*`） | 規則分兩套：`dotnet8.*`、`dotnet.*`；HTTP、連線池兩版同名 |
| 刪掉的 Pod 還一直報 | 計數類規則對已刪的 Pod 照算 | 最新資料超過 2 分鐘就不報；只有容器重啟、Job 失敗照報（`report_gone`） |
| 測試環境編譯失敗 | NuGet 經過會重簽憑證的 proxy | 測試時信任 proxy 的 CA（`e2e/local-k3s/dotnet-app.yaml`）；正式環境 image 先建好 |

## Elasticsearch

| 症狀 | 原因 | 做法 |
|---|---|---|
| 兩個節點，掛一台整個 cluster 停擺 | 兩個節點時只有一台能投票 | 至少三個 master 候選：三台一樣的節點，或兩台存資料加一台 `voting_only` |
| ES 被 kill -9 後擔心掉資料 | — | 已經回成功的不會掉，前提是 `index.translog.durability` 保持預設 `request`，不要改 `async` |
| 一個節點時 metrics 是 yellow | metrics 資料流預設要一份副本；log 設了 `auto_expand_replicas: 0-1` 所以是 green | 不影響寫入 |

## Grafana 告警

| 症狀 | 原因 | 做法 |
|---|---|---|
| 規則狀態 Error，ES 回 400 | Grafana 的 terms 預設用 `_term` 排序，ES 9 不認得 | 每個 terms 都寫 `orderBy: _count`（`engine/configs/grafana/alert-rules.yaml` 已寫） |
| data source 測試回 403 | Grafana 會先打 `GET /` 看版本 | key 要有 cluster `monitor` 權限（`elasticsearch-setup.sh` 的 Grafana 唯讀 key 已給） |
| 同一件事每分鐘收到「舊的 resolved＋新的 firing」 | `summary` 是告警的 label，內容一變就是另一則 | engine 寫的 summary 不放「持續 N 分鐘」「等 N 項異常」這種每分鐘會變的字（`engine/investigator/alerts.py`） |
| 件還開著，Grafana 卻發 resolved，之後又 firing | 一件只有一筆、每輪覆蓋；Grafana 的查詢範圍到「排定的評估時間」為止，查詢晚幾秒才送出。覆蓋剛好落在這幾秒之間時，`@timestamp` 晚於範圍結束點，查到 0 筆 | engine 寫開著的件和心跳時，`@timestamp` 往前 1 分鐘（`cmd_alert.STAMP_LAG`）。驗證：`e2e/pipeline/run-alert-flap.py` |
| ES 連不上時收到 `DatasourceError`，不是「AIOps 停了」，而且一次兩則 | 規則 `execErrState: Error` 時 Grafana 發自己的 DatasourceError；兩條規則各發一則 | 要它也叫「AIOps 停了」就把心跳規則改 `execErrState: Alerting`；事件規則的 DatasourceError 用 notification policy 靜音（還沒改，要跟管 Grafana 的人定） |
| 儀表板查 `_exists_:report` 永遠沒資料 | `aiops-alerts` 的 `report` 是 `index: false` | 用 `category:service` 篩 |
| `aiops-rca` 儀表板「這件事故的異常」No data（規則觸發的事故一定空） | 原本查 `status:(warning OR anomaly)` 又只依時間範圍篩：規則觸發的異常沒有 `status`，而且撈到的不一定是這件的 | 已改：`investigator alert` 併件時把 `alert_id` 寫回 `aiops-anomalies`（`cmd_alert.tag_anomalies`），面板查 `alert_id:"$alert"`；改版前已結束的件撈不到（當時沒寫回） |

## AIOps engine

| 症狀 | 原因 | 做法 |
|---|---|---|
| 根因報告「最慢那個請求的 log」少了幾行 | log 的 `@timestamp` 是應用時間，時鐘偏或請求結束後才寫，離 span 十幾分鐘 | `logs_of_traces` 已前後多放 30 分鐘；更多的看 `quality.log_clock_skew` |
| 下游（DB 連線池）跟上游（前端 API）同時出事，`aiops-alerts` 開了兩件 | 這段時間的 trace 算不出「上游呼叫下游」：trace context 沒傳到下游、trace 的 `service.name` 跟異常的服務名不同、`alerts.chain` 關了；或兩件開始時間差超過 `close_after` | `investigator topology --window 2h` 看有沒有那條邊；alert 的輸出有「呼叫關係查不到」就是查 trace 失敗（[rca README](../engine/configs/rca/README.md#事件整合alert)） |
| 上游那件「恢復」、同時下游開了新的一件（連帶上游） | 上游先被偵測到、下游晚一輪：下游開件時把上游那件整件併進來（`merged_into`） | 正常；看下游那件就好。要避免就縮短偵測延遲（規則窗、排程間隔） |
| A→B→C 三個一起出事，開成兩件 | 事件整合只併一層（直接呼叫者）：C 連帶 B，A 自己一件 | 已知限制（[cross-service-aggregation](pdca/phase3-locate/2026-10-cross-service-aggregation.md) Act） |
| Kafka 落後在變少（消費者追上來），卻報「落後一直增加」 | `increase` 把數字變小當成計數器歸零重來，把新的值整個算成增加；落後是量測值不是計數器 | 會上下動的量測值用 `type: delta`（最後 − 最前）；`increase` 只用在計數器（`kafka.consumer_lag_growing` 已改） |
| 新建的 Pod 一直 Pending 才 2 分半就報（`for: 5m`）；新 Deployment 1 分半就報副本不夠 | 舊的 `for` 只要求 for 裡最早的點離現在超過一半；新對象從第一筆就成立時，一半就報 | 已改：對象要在 for 開頭之前就有資料（`detector/rules.py`） |
| 核准的規則草稿有一條壞掉（缺欄位、查詢錯），`rules` 整輪都沒跑 | 一條規則丟例外就整個指令結束，其他規則也沒寫 | 已改：只跳過那一條、其他照跑照寫，最後 exit 1、印「N 條失敗」 |
| 規則草稿設了 `expires_at`，過期後還在報 | `approved_rules` 只看 `status`，沒看 `expires_at` | 已改：跟 `--at` 的時間比，到期就不載入 |
| Pod 同儕橫向比對報剛起來的新 Pod CPU 高 | 啟動時 JIT、載入類別，前幾分鐘 CPU 高 | `peer.yaml` 的 `warmup`（5m）：起來不到 5 分鐘的 Pod 不比 |
| 擴容時舊 Pod 被報「記憶體比其他 Pod 高很多」 | 新 Pod 比舊 Pod 多、記憶體還在長，中位數被新 Pod 拉低 | 同上 `warmup`；heap 長得比 5 分鐘久的服務調長 `warmup`（要比 `window` 短） |
| 全部 Pod 都壞，同儕比對卻只報最嚴重的一個 | 只看「比其他 Pod 高多少」，沒看其他 Pod 本身壞了 | `max_peer_median`：其他 Pod 中位數到規則門檻（5%、2 秒）就不挑，交給規則 |
| 2 個副本的服務壞了一個，同儕比對不報 | `min_peers: 3`：2 個分不出誰怪 | 正常；看規則 `http.error_rate_floor`、baseline |
| 根因報告的「平常」數字跟出事時差不多（例如平常 p95 也是 1.5 秒） | 一週前沒資料時，「平常」改用出事前一段、中間空 `ref_gap`；偵測比事故晚約 4 分鐘，`ref_gap` 太短就包到事故 | `ref_gap` 要大於偵測延遲；正式設定 10 分鐘（`engine/configs/rca/rca.yaml`） |
| `quality` 中斷：`bulk 寫入 aiops-anomalies 失敗 ... Non-standard token 'NaN'` | 缺欄位那組（例如 log 沒有 namespace）的 key 被 pandas 3 變成 NaN | 已修（`quality._frame`）；舊版這一輪的結果全沒寫 |
| trace 有 N+1，`trace.segment.repeated` 沒報、反而報一個沒有「→」的新段 | DB span 只有新版 `db.system.name`，段名沒有對象 | 已補這個欄位；Client span 什麼對象欄位都沒有的仍看不到 |
| 程式 catch 掉 DB 例外、回 200，`trace.segment.swallowed` 沒報 | Java agent 會把丟例外的那段標成錯誤，舊定義只算沒標錯誤的 | 已改：入口沒標錯誤就算吞掉（已決定維持；重試後成功也會算，另外排除留到下一輪） |
| 某支 API 一直沒有 trace 基準線 | 尾端取樣只留 5%：每點隨機留下的 < `min_requests` 5 | 每個 interval 至少 100 個請求才學得到（5 分鐘 → 每分鐘 20 個） |
| 只有一部分請求變慢，trace 段的 time_ms 沒報 | 每點約 20 個請求的平均；長尾讓基準線很寬（實測門檻約 430 ms） | 已知限制；要看 p90 或慢請求比例（還沒做） |
| trace 基準線的錯誤率、時間偏高很多 | 讀到尾端取樣全留的那份（錯、慢、例外） | `traces_index` 用 `traces-generic.otel-*`、`sample_policy: random`（實測混進來錯誤率 4% → 35%） |
| Pod 沒有 `app.kubernetes.io/name`，`quality.log_missing_identity` 卻沒報 | log pipeline 退回用 container 名稱（例如 `app`），不算缺 | 已知缺口；看 `service.name` 是不是 container 名稱 |
| span 某欄位型別錯，failure store 卻沒有 | traces 是 logsdb、ignore_malformed：文件存進去，那個欄位丟掉 | 查 `_ignored`；`quality.ingest_rejected` 看不到這種 |
| 收集系統停了半小時以上，每個服務都被報成「流量掉到 0」 | 偵測時有 baseline、整段沒資料的服務一律補 0 | 已改成只在 index 有資料的時間窗補 0（`es.alive_times`），[baseline-verify](pdca/phase2-anomaly/2026-10-baseline-verify.md) |
| 稀少的 error log，baseline 學出來的平常次數偏高 | 用序列自己的 filter（只數 error）判斷收集系統有沒有在跑，沒人出錯的小時被當「不知道」 | 已改成看整個 index；實測 λ 1.68 → 1.21（實際 1.2） |
| `train`／`detect` 整個失敗：`KeyError: 'end'`、`PatternError` | `aiops-tuning` 裡有缺欄位、正規表示式錯的調整 | 已改成略過那一筆並印「⚠ 略過調整 <id>」；照提示 `adjust remove --id` |
| p95 一直報 anomaly，值在 0.175、0.375 之間跳 | 直方圖的 p95 落在桶中點，MAD＝0，跨一格 z 約 23 | p95 已設 `instant_z: null`（跨一格只算 warning，連續 2 窗才 anomaly；合成資料一週 anomaly 3.52% → 0.93%，warning 變多）；仍吵的對象 `tune --z`；根本修法待改程式（σ 至少半個桶寬，[threshold-tune](pdca/phase2-anomaly/2026-10-baseline-threshold-tune.md)） |
| 根因報告說「這段時間沒有這個服務的 trace」，Kibana 卻看得到 | 鑰匙對不上：路由沒參數化（`/items/123`）、舊版 Java agent 的 span 名稱沒有 method（`/stock/{id}`）；Pod 沒有 `app.kubernetes.io/name` label 時，trace 的 `service.name` 是應用自設的、可能跟 metrics 不同（推論） | 不給 `--route` 再跑；用 metrics 的 `k8s.pod.name` 查 trace 的 `service.name`；根本做法是應用修路由模板、Pod 加 label（[metrics-trace](pdca/phase1-collect/2026-10-metrics-trace.md)） |
| 開了台灣行事曆，假日那天工廠（24×7）服務的故障也沒報 | 台灣行事曆每一天沒有 `match`，變成所有服務都不學、不判斷 | `calendar.yaml` 的 taiwan 來源寫 `match` 限定週休的系統（來源層級的 `match` 會套到每一天） |
| `adjust add --start 2026-10-20T02:00` 排除的時段差 8 小時 | 舊版把沒寫時區的時間當 UTC | 現在當台北時間（`train.timezone`）；舊資料用 `adjust list` 看，差 8 小時的重寫一次 |
| 查 `aiops-chronic` 找不到天天觸發的規則 | 規則那類的 `keys` 是 `series`＋對象（`service`…），不是只有服務 | 用 `keys.series` 找 |

## 本機測試環境（Windows、Rancher Desktop）

| 症狀 | 原因 | 做法 |
|---|---|---|
| `python` 沒有任何輸出 | 那是 Windows Store 的殼 | 用 `py`，或 `engine/.venv/Scripts/python` |
| `GrafanaPluginInit` 3 個測試失敗：`NoneType is not a container` | 子程序輸出用 cp950 解碼失敗 | `PYTHONUTF8=1` |
| 加了 `PYTHONUTF8=1` 中文還是亂碼 | `python -I` 會忽略所有 `PYTHON*` 環境變數 | 用 `-X utf8` |
| `wsl -d rancher-desktop -- sh -c '...'` 報 `syntax error: unexpected word` | wsl 把參數再交給 shell 解析一次，引號、`$( )` 會壞掉 | 指令從 stdin 給 `wsl -d rancher-desktop -- sh -s`（`run-durability.sh` 的 `on_node`） |
| busybox Pod 跑不了 `rca-lib.sh` | Windows checkout 成 CRLF | 建 ConfigMap 前 `tr -d '\r'`（`run-rca-demo.sh` 已做） |
| 容器裡的路徑變成 `C:/Program Files/Git/...` | Git Bash 改寫 `/` 開頭的參數 | 那一行加 `MSYS_NO_PATHCONV=1`；它也會讓 `KUBECONFIG=/c/...` 不轉換，kubectl 讀不到，所以只加在需要的指令 |
| `elasticsearch-setup.sh` 裝到別人的 ES | 那個 port 已經是別的任務的 `kubectl port-forward`，自己的容器沒綁上 | 跑之前 `curl <ES_URL>/` 看 `version.number`、`cluster_name` 是不是自己那台 |
| 本機 ES、kubectl 一直 `Connection was reset`、`TLS handshake timeout`，`nerdctl` 沒回應 | Rancher Desktop VM 記憶體與 swap 用滿（多個測試 ES＋共用 cluster） | 同時跑的測試 ES 最多 2 個（各 heap 512m）；用完就 `nerdctl rm -f` |
| `localhost:<port>` 連不上，`127.0.0.1` 卻可以 | 停掉的 nerdctl 容器還佔著那個 port 的 `::1` | 用 `127.0.0.1`，或換 port、刪掉舊容器 |
| Grafana 面板一直 `Loading plugin panel...` | `kubectl port-forward` 在 Windows 上同時開很多連線會逾時 | Service 用 `LoadBalancer`（Rancher Desktop 對應到 `localhost`），例如 rca-demo 的 `localhost:3300` |
| port-forward 一連就斷：`connection refused` | 連到正在 Terminating 的舊 Pod | 等舊 Pod 刪完，或 `port-forward deploy/<名稱>` |
| port-forward 一用就 `curl: (52) Empty reply from server` | ES Pod 被重建（別的任務重啟），舊的 port-forward 斷了 | 重開；`sdk-inject` 腳本斷了會自己重連 |
| Pod 一直 Pending：`Insufficient memory`，節點其實還有空 | initContainer 只寫 limit，request 也算成 limit（例如 2Gi） | 同時寫 request（`sdk-inject/dotnet.yaml` 的編譯寫 512Mi） |
| `run-e2e.sh` 跑完 ES 就連不上 | 腳本結束時關掉自己開的 port-forward | 自己另開 `kubectl port-forward --address 127.0.0.1 svc/elasticsearch 29200:9200` |
| 寫 ES 的 OTLP 入口一直逾時、`_cat` 被重設，最後 WSL 回 `Wsl/Service/0x8007274c` | 好幾個測試 ES（各 512MB heap）加共用 cluster 擠在同一個 VM，記憶體 98%、load 100 以上 | 同一時間只開一個測試 ES（拿名額），用完 `nerdctl rm -f` |
| 想把 4 週前的合成 metrics 寫進 `metrics-*.otel-*` | TSDS 的 `index.look_back_time` 上限 7 天 | 測試用 data stream 另建 standard mode 的 template（`baseline_edge_e2e.setup`）；欄位型別、聚合結果一樣 |
| Rancher Desktop 設定的 VM 記憶體沒作用 | Windows 上 VM 大小由 WSL 決定（預設主機的一半） | 改 `~/.wslconfig` |
| 本機 ES 一直 `Connection reset`、逾時，ES log「writing cluster state took 58s」，之後 nerdctl、kubectl 都連不上 | 同時起 5 台 ES 加共用 cluster：WSL 記憶體 7.7GB 用光、swap 滿、load 200 以上 | 同一時間只開 1 台自己的 ES（scratchpad 的 `es-slot.sh` 拿名額），用完 `nerdctl rm -f` |
| 合成 metrics 灌進 TSDS 前先建欄位，報 `Cannot update parameter [time_series_metric]` | 欄位型別要跟 ES OTLP 入口建的一樣（直方圖帶 `time_series_metric: histogram`） | bulk 用 `dynamic_templates`（`gauge_double`、`exponential_histogram`）；已有的欄位不再送（`tests/rules_edge_e2e.py`） |
| e2e 腳本 `jq: command not found` | 這台沒有 jq | `winget install jqlang.jq` |
| `elasticsearch-setup.sh`、e2e 腳本在已有資料的 ES 上 `curl: (3) URL rejected: Malformed input` | Windows 的 `jq.exe` 在 Git Bash 裡把換行輸出成 CRLF，`$(jq ...)` 拿到的名稱帶 `\r` | 用 `jq -b`（`--binary`）：PATH 前面放一個 `exec jq.exe -b "$@"` 的 `jq`。Linux 不會發生 |
| `KUBECONFIG` 有設，kubectl 卻連 `localhost:8080` | 腳本那行有 `MSYS_NO_PATHCONV=1`，`/c/...` 的路徑沒被轉換 | `KUBECONFIG` 用 `C:/...`（`cygpath -m`） |
| curl 報 `URL rejected: Malformed input`，`elasticsearch-setup.sh` 中斷 | winget 的 jq 輸出 CRLF，變數尾巴帶 `\r` | PATH 前面放一個 `jq -b "$@"` 的小腳本（`-b`：不轉 CRLF） |
| `run-trace-e2e.sh` 的 trace-app 一直 CrashLoopBackOff | `trace-app.sh` 是 CRLF，ConfigMap 原樣建 | 跑之前把 `trace-app.sh` 轉 LF（`tr -d '\r'`），跑完 `git checkout` 還原 |
| 內建瀏覽器裡 Grafana 面板一直 Loading | 瀏覽器 pane 隱藏時頁面在背景，載不完 | 用 `e2e/local-k3s/rca-demo/shot.py`（headless Edge）截圖 |
| 測試 ES 寫 cluster state 要 10～30 秒、連線常被重設（WinError 10054），最後 nerdctl、kubectl 都卡住 | 好幾個代理各起一個 ES 加共用 cluster，WSL 記憶體不夠 | 同時最多 2 個測試 ES：起之前 `es_slot_take`，用完刪容器、`es_slot_release` |
| 合成 log 寫進 `k8s-*` 後服務、namespace 都是 null | log pipeline 吃的是 Fluent Bit 的樣子 | 原文放 `log`，K8s 放 `kubernetes.namespace_name`、`pod_name`、`labels`（`trace_quality_edge_e2e.py` 的 `log()`） |
| 平台頻道一直收到「主機時鐘偏移」「容器記憶體快到上限」 | WSL 時鐘偏移約 0.2 秒；測試 ES 的記憶體用到 92～98% | 這台機器的狀況，規則本來就該報；跟測的事故無關 |
| nerdctl、kubectl 都 timeout，WSL 報 `0x8007274c`，主機只剩幾百 MB | 好幾個測試 ES 容器加上共用 cluster，WSL 的 VM 記憶體不夠 | 同一時間只起 1 個測試 ES（scratchpad 的 `es-slot.sh` 拿名額），用完 `nerdctl rm -f` |
| Git Bash 裡 `TZ=Asia/Taipei date` 印出 UTC | Git Bash 沒有 zoneinfo，不認得的時區當 UTC，也不報錯 | 用 POSIX 寫法 `TZ=UTC-8`（＝ +08:00） |
| 腳本結束了，`kubectl port-forward` 還佔著 port | Git Bash 的 `kill` 有時停不掉原生的 `kubectl.exe` | 再用 `/proc/<pid>/winpid` 的 Windows PID `taskkill //F`（`run-schedule-e2e.sh` 的 cleanup）；`tasklist` 確認 |
| `kubectl create job --from=cronjob/...` 後 CronJob 出現 `UnexpectedJob` 事件 | 手動建的 Job 帶 ownerReference，不是 controller 排的 | 不影響排程，可以不管 |
