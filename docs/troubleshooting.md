# Troubleshooting

只寫結論：症狀 → 原因 → 做法。依區塊分。

## 收值（log、metrics、trace 共用）

| 症狀 | 原因 | 做法 |
|---|---|---|
| key 作廢或打錯期間，資料整批不見、補不回來 | OTLP 出口只對連不上、逾時、429、502、503、504 重試；401 當永久錯誤直接丟，沒有設定可改 | 換 key 一律用 `telemetry/scripts/rotate-writer-key.sh`（先發新的、換進 Secret、Collector 換用後才作廢舊的；Collector 從檔案讀 key，不用重啟）。監控 Collector `:8888/metrics` 的 `otelcol_exporter_send_failed_metric_points`、`otelcol_exporter_send_failed_log_records`，大於 0 就是有資料被丟 |
| Collector log 出現 `Partial success ... dropped` | ES 拒收其中幾筆（例如欄位型別衝突，400），那幾筆丟掉 | 監控這行。`CONFLICT`（409）是重送的被 `_id` 擋掉，屬於正常去重複，不算遺失 |
| 連線逾時時資料不見 | `elasticsearch` 寫入元件逾時直接丟 | 用 OTLP 出口，不要換回 `elasticsearch` 寫入元件（[metrics/README.md](metrics/README.md#為什麼用-otlp-送不用-elasticsearch-寫入元件)） |
| 兩個 cluster 的資料混在一起 | `CLUSTER_NAME` 沒改（它是每筆的 `k8s.cluster.name`，metrics 的資料流名稱裡沒有它） | 每個 cluster 部署時一定要改 |
| 改了部門，資料還進舊的 group | dept 清單有兩處：`telemetry/groups.yaml` 和 `common.yaml` 的 `transform/group` | 兩處一起改。改 namespace 的 `dept` label 後 2～17 秒生效，log、metrics 同時切 |
| 平台元件（kube-system 等）的資料在 unassigned | 這些 namespace 沒有 `dept` | 正常，跟 metrics 的兜底 tenant 同義 |
| 正式環境 kubelet 連線不驗憑證 | `kubeletstats` 設了 `insecure_skip_verify: true` | kubelet 憑證由內部 CA 簽的話改成驗證 |

## log

| 症狀 | 原因 | 做法 |
|---|---|---|
| ES 拒收時 log 量暴增 | Collector 收到自己印的錯誤再送一次，形成放大迴圈 | `node.yaml` 的 `file_log` 排除 `otel-collector` namespace，不要拿掉 |
| log 停在 `logs-generic.otel-*`，沒進 `k8s-*` | 改了 `k8s-logs-otel`、`logs-otel@custom` 之後，舊的 backing index 還在用 | `elasticsearch-setup.sh` 會自動檢查並 rollover；手動改的話要 rollover `logs-*.otel-*` |
| 目的地的 default pipeline 沒跑 | 改了 `_index` 後 ES 不會自動跑目的地的 default pipeline | `k8s-logs-otel` 最後自己呼叫 `k8s-logs`，不要拿掉 |
| ES 停很久之後 log 有缺 | 佇列在節點上（`/var/lib/otelcol/node`），每個 group 上限 10,000 批、每批最多 2,000 行 | 量大或可能停很久時先估算 |
| Pod 重建後 log 重複或中間缺 | 讀檔位置也記在 `/var/lib/otelcol/node`；目錄被刪就從檔案尾巴讀 | 這個目錄要用 hostPath，不要刪 |
| 第一次部署時舊的 log 沒收 | `start_at: end`，已經存在的檔從尾巴讀 | 預期行為，不補收 |
| 量大的服務掉 log | kubelet 預設 10MB 就輪替壓縮，Collector 沒讀完的那段會掉 | 平台端把 kubelet 的 `container-log-max-size` 調大（例如 50Mi）。Collector `poll_interval` 0.2 秒，每秒 5,000 行不會掉 |
| log 有 trace_id，用 `trace_id` 卻查不到 trace | 值不是 32 個 hex（印錯、截斷、GUID 帶減號）；pipeline 不設 `trace.id` | 看 `app.trace_id_invalid`，改應用的 log 設定 |
| 開了 `otel.logs.exporter: otlp`，同一行 log 有兩筆 | stdout 那份（檔案）＋ agent 直送那份；內容指紋不同，不會去重 | 預期行為；查詢時用 `log.source: otlp`（或 `file`）只取一份 |
| 用 Datadog 的服務 log 綁不到 trace | `dd.trace_id` 是十進位，對不到 OTel 的 trace | 換 OTel agent；`dd.trace_id` 留在 `app` |
| Java stack trace 變成很多筆 | 純文字 log 一行一筆 | 標準格式是 JSON（stack trace 在一個欄位）；沒照標準的服務 AIOps 效果較差 |
| 節點用 Docker 當 runtime 的 cluster 收不到 log | log 檔在 `/var/lib/docker/containers` | 另外掛這個目錄。RKE2、k3s 是 containerd，不需要 |

## metrics

| 症狀 | 原因 | 做法 |
|---|---|---|
| ES 重啟後少了停機期間的 metrics，Collector 卻沒報錯 | TSDS 每個 backing index 只收一段時間範圍（預設往後 9 分鐘），範圍外的進 failure store（`.fs-metrics-*`），回給 Collector 的是成功 | `metrics-otel@custom` 把 `look_ahead_time` 設 2 小時（`elasticsearch-setup.sh` 會建，只對新的 backing index 生效，既有的要 rollover 一次）。監控 failure store：`curl -s -u elastic:<password> "https://<es>:9200/.fs-metrics-*/_count?expand_wildcards=all&ignore_unavailable=true"` |
| 重啟 Collector 後少 1～2 個點 | 舊的停、新的還沒開始抓之間（約 30～60 秒）沒人抓，事後補不回來；已經抓到的不受影響 | 換版、改設定排在離峰 |
| K8s 物件狀態的資料重複 | cluster 那一份的 Collector 多於 1 個副本 | replicas 一定是 1，換版用 `Recreate` |
| 應用的 `service.name` 變成 `pods` | Prometheus receiver 把抓取工作的名稱填進 `service.name`，讀 Pod label 的那一步不覆蓋 | `common.yaml` 先清掉再讀，不要改順序（測試有一項擋這個） |

## trace

| 症狀 | 原因 | 做法 |
|---|---|---|
| trace 進 unassigned | 應用連到別的節點的 Collector，那裡認不得這個 Pod | Service 一定要 `internalTrafficPolicy: Local` |
| 用 hostNetwork 的應用 trace 進 unassigned | 連線來源是節點 IP，對不到 Pod | 應用不能用 hostNetwork |
| ES 空間不夠 | trace 量通常比 log 大；尾端取樣後約剩 15% | 全廠上線前估算；不要在應用調低 `otel.traces.sampleRatio` |

## .NET

| 症狀 | 原因 | 做法 |
|---|---|---|
| Collector 抓不到 .NET 的 metrics | 自動埋點的 Prometheus 端點只聽 localhost | 改成推（OTLP），chart 已經這樣設 |
| 沒有 `http.server.request.duration` | 自動埋點預設沒開 ASP.NET Core 的 HTTP 指標 | `OTEL_DOTNET_AUTO_METRICS_ADDITIONAL_SOURCES=Microsoft.AspNetCore.Hosting,…`（chart 已加） |
| log 量暴增 | ASP.NET Core 預設每個請求印 5 行 Information | `Logging__LogLevel__Microsoft.AspNetCore=Warning`（chart 已設） |
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

## AIOps engine

| 症狀 | 原因 | 做法 |
|---|---|---|
| 根因報告「最慢那個請求的 log」少了幾行 | log 的 `@timestamp` 是應用時間，時鐘偏或請求結束後才寫，離 span 十幾分鐘 | `logs_of_traces` 已前後多放 30 分鐘；更多的看 `quality.log_clock_skew` |
| 根因報告的「平常」數字跟出事時差不多（例如平常 p95 也是 1.5 秒） | 一週前沒資料時，「平常」改用出事前一段、中間空 `ref_gap`；偵測比事故晚約 4 分鐘，`ref_gap` 太短就包到事故 | `ref_gap` 要大於偵測延遲；正式設定 10 分鐘（`engine/configs/rca/rca.yaml`） |

## 本機測試環境（Windows、Rancher Desktop）

| 症狀 | 原因 | 做法 |
|---|---|---|
| `python` 沒有任何輸出 | 那是 Windows Store 的殼 | 用 `py`，或 `engine/.venv/Scripts/python` |
| `GrafanaPluginInit` 3 個測試失敗：`NoneType is not a container` | 子程序輸出用 cp950 解碼失敗 | `PYTHONUTF8=1` |
| 加了 `PYTHONUTF8=1` 中文還是亂碼 | `python -I` 會忽略所有 `PYTHON*` 環境變數 | 用 `-X utf8` |
| busybox Pod 跑不了 `rca-lib.sh` | Windows checkout 成 CRLF | 建 ConfigMap 前 `tr -d '\r'`（`run-rca-demo.sh` 已做） |
| 容器裡的路徑變成 `C:/Program Files/Git/...` | Git Bash 改寫 `/` 開頭的參數 | 那一行加 `MSYS_NO_PATHCONV=1`；它也會讓 `KUBECONFIG=/c/...` 不轉換，kubectl 讀不到，所以只加在需要的指令 |
| `elasticsearch-setup.sh` 裝到別人的 ES | 那個 port 已經是別的任務的 `kubectl port-forward`，自己的容器沒綁上 | 跑之前 `curl <ES_URL>/` 看 `version.number`、`cluster_name` 是不是自己那台 |
| `localhost:<port>` 連不上，`127.0.0.1` 卻可以 | 停掉的 nerdctl 容器還佔著那個 port 的 `::1` | 用 `127.0.0.1`，或換 port、刪掉舊容器 |
| Grafana 面板一直 `Loading plugin panel...` | `kubectl port-forward` 在 Windows 上同時開很多連線會逾時 | Service 用 `LoadBalancer`（Rancher Desktop 對應到 `localhost`），例如 rca-demo 的 `localhost:3300` |
| port-forward 一連就斷：`connection refused` | 連到正在 Terminating 的舊 Pod | 等舊 Pod 刪完，或 `port-forward deploy/<名稱>` |
| `run-e2e.sh` 跑完 ES 就連不上 | 腳本結束時關掉自己開的 port-forward | 自己另開 `kubectl port-forward --address 127.0.0.1 svc/elasticsearch 29200:9200` |
| Rancher Desktop 設定的 VM 記憶體沒作用 | Windows 上 VM 大小由 WSL 決定（預設主機的一半） | 改 `~/.wslconfig` |
| e2e 腳本 `jq: command not found` | 這台沒有 jq | `winget install jqlang.jq` |
| 內建瀏覽器裡 Grafana 面板一直 Loading | 瀏覽器 pane 隱藏時頁面在背景，載不完 | 用 `e2e/local-k3s/rca-demo/shot.py`（headless Edge）截圖 |
| 平台頻道一直收到「主機時鐘偏移」「容器記憶體快到上限」 | WSL 時鐘偏移約 0.2 秒；測試 ES 的記憶體用到 92～98% | 這台機器的狀況，規則本來就該報；跟測的事故無關 |
