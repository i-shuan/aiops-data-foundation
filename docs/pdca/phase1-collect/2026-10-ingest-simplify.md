# 寫入簡化：集中式資料寫入憑證 (Shared Ingest Token)、ES failure store、靜態臨界值規則 (Static Threshold Rules)（2026-10）

**狀態**：已完成（k3s e2e 待下次跑）　　**日期**：2026-10-25　　**相關**：分支 `claude/single-writer-key`
**模組**：① 收值（寫入 key、Collector 出口）、③ 存（failure store）、④ 判斷（靜態保底規則）

## Plan：這一次要達到什麼

- **需求**：
  1. 每個部門 group（it、imc、unassigned）各一把寫入 key、一個 Collector 出口；三把 key 是三倍的出錯機會，換 key 也要做三次。
     - key 設錯或權限 pattern 漏了，ES 回 200、把整批拒絕放在回應內容裡，Collector 算成「送成功」、沒有 log（[es-startup-401](2026-10-es-startup-401.md) 實測）。
     - 組織**沒有部門間寫入隔離的硬性要求**（2026-10-25 確認）。
     - 「資料屬於哪個部門」本來就由 Collector 依 Pod 查 K8s 決定，不信應用自己填。
  2. 型別寫錯（mapping 衝突）、pipeline 失敗的文件，ES 直接拒絕，只有 Collector log 看得到。
  3. baseline 要累積資料才判斷得準（退階：同星期×同小時 2 週 → 每天同小時 3 天 → 不分時段 50 點）；上線第一天沒有「一定是壞了」的保底。
- **目的**：
  1. 集中式資料寫入憑證、一個出口；資料照樣依部門分到各自的 index（`data_stream.namespace` 由 Collector 依 group 設），讀取權限照樣依部門隔離。
  2. 被 ES 拒絕的文件留在 failure store，查得到、可以告警。
  3. 錯誤率、p95 有固定門檻的保底規則，第一天就能報。
- **範圍**：
  - 做：Collector 三份設定（common、node、sampler）、cluster 的出口；`elasticsearch-setup.sh` 發一把 key；`rotate-writer-key.sh` 改成一把；e2e 腳本；文件。
  - 做：failure store 開在 log、metrics、trace 的 index template；rules 加「一段時間內的比例／百分位」類型和兩條規則。
  - 不做：讀取權限（人看自己部門的角色）、`dept → group` 的分流規則、邊緣身份鑑別與標籤注入（Edge Identity Sanitization & Injection；Collector 依 Pod 注入身分）都不動。

| 不變的 | 為什麼還需要 |
|---|---|
| Collector 依 Pod 補 dept、group、service.name，刪掉應用自己帶的 | 決定資料屬於哪個部門；防填錯 |
| index 依 group 分（`k8s-<group>-*`、`*.otel-<group>`） | 讀取權限、保留天數依部門 |
| 給人用的唯讀角色每個 group 一個 | 人只看自己部門 |

| 變了的 | 之前 | 之後 |
|---|---|---|
| 寫入 key | 3 把（`es-writer-it`、`-imc`、`-unassigned`），各自只能寫自己的 index | 1 把（`es-writer`），能寫所有資料流（見下） |
| Collector 出口 | 每個 group 一個，`routing` 依 group 分 | 一個，不用 routing |
| Secret `es-writer-keys` | 3 個欄位 | 1 個欄位 `ES_WRITER_API_KEY` |
| 換 key | 每個 group 各換一次 | 換一次 |
| 失去的 | 「IT 的 key 寫不進 IMC」 | — |

- `es-writer` 能寫：`k8s-*`、`metrics-*`、`logs-*.otel-*`、`traces-*.otel-*`。
- 失去的那道防線：某個 cluster 的 Collector 設定被改壞時的最後一道。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `telemetry/collectors/common.yaml` | 3 個 `bearertokenauth` → 1 個 `bearertokenauth/es`（讀 `ES_WRITER_API_KEY`） | 寫到哪個 data stream 由 `data_stream.namespace` 決定，不需要依 group 分出口 |
| 同上 | 3 個 `otlp_http` 出口 → 1 個 `otlp_http/es` | 同上 |
| 同上 | 拿掉 `routing/group` connector 和 3 條 `metrics/<group>` pipeline | 出口少了，key 設錯的機會也少了 |
| `node.yaml`、`cluster.yaml` | metrics pipeline 直接送 `otlp_http/es`；拿掉 `routing/logs` 和 3 條 `logs/<group>` | 同上 |
| `sampler.yaml` | 1 把 key、1 個出口，拿掉 `routing/traces` | 同上 |
| `node-collector.yaml`、`cluster-collector.yaml` | 註解 | — |
| `telemetry/scripts/elasticsearch-setup.sh` | 3 把 `es-writer-<group>` → 1 把 `es-writer`，只有 `create_doc`、`create_index`、`auto_configure` | pattern 寬到不會因為資料流名字（例如 `-default`）沒列到而被拒 |
| 同上 | 已存在的 `k8s-*` 資料流打開 failure store | template 的設定只管新建的資料流 |
| 同上 | AIOps baseline key 加 `read_failure_store` | 讀 `::failures` 要另外的權限 |
| `telemetry/storage/index-template.json` | `k8s-logs` 加 `data_stream_options.failure_store.enabled: true` | ES 9.5 的 OTel 資料流預設已開，我們的 `k8s-*` 沒開 |
| `telemetry/scripts/rotate-writer-key.sh` | 不用給 group，換 `es-writer`、Secret 的 `ES_WRITER_API_KEY` | 只有一把 |
| `engine/configs/baseline/quality.yaml` | 新增 `quality.ingest_rejected`（type `present`）：三種 `::failures` 有一筆就報 | 被拒的文件 ES 回 201，Collector 看不到；不數就沒人知道 |
| 同上 | 依 `document.index`、`error.type` 分 | — |
| `engine/detector/rules.py`、`__main__.py` | 新增 rule type `window`：整段 lookback 用 series.yaml 的查法（`hist_ratio`、`percentile`）算一個值 | 原本的規則是「每個點看欄位值」，算不了「5 分鐘的錯誤率、p95」 |
| 同上 | 請求數不到 `min_events` 不判斷；`show: pct`、`seconds` | 查法沿用 baseline 的 `fetch`，不另寫一套 |
| `engine/configs/baseline/rules.yaml` | `http.error_rate_floor`（≥ 5%，critical）、`http.latency_p95_floor`（≥ 2 秒，warning） | 上線第一天 baseline 還沒學夠時的保底 |
| 同上 | 兩條都是 5 分鐘、至少 20 個請求 | 同上 |
| `engine/tests/test_rules.py` | `window` 類型的測試；設定檢查認得 `window` | — |
| `e2e/local-k3s/*.sh`（11 支） | Secret 改一個欄位；權限檢查改成「能寫 IT、IMC，不能讀、不能寫 aiops-*」 | 跟著改 |
| 同上 | `run-metrics-scenarios.sh keyfail`、`load/run-load.sh keyfail`：作廢 `es-writer`，預期兩個 group 都停 | 同上 |
| 同上 | `run-key-rotation.sh` 換 `es-writer` | 同上 |
| `README.md`、`docs/architecture.md`、`poc-status.md` | 寫入 key、Secret、failure store 的描述 | — |
| `docs/log/README.md`、`metrics/README.md`、`trace/README.md` | 同上 | — |

- `quality.ingest_rejected` 查 `k8s-*::failures`、`metrics-*.otel-*::failures`、`traces-*.otel-*::failures`。
- `docs/log/fluentbit-vs-otel.md` 是過去的測試紀錄，沒改（記的是當時每個 group 一把 key 的結果）。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| Collector 設定結構對不對 | `otelcol validate`（contrib 0.162.0）：common+cluster、sampler | 通過 |
| 同上 | common+node | 卡在建 pipeline 時讀 K8s 憑證檔；master 版一樣卡在這裡 → 出口和引用都對 |
| 集中式資料寫入憑證的權限夠不夠、會不會太寬 | es-t 跑改過的 `elasticsearch-setup.sh`，用新 key 查 `_has_privileges` | 五種資料流（見下）：能寫、不能讀 |
| 同上 | 同上 | `aiops-anomalies`：不能寫 |
| trace：一個出口，三個 group 還是分開 | docker：正式 `sampler.yaml`＋node 端 → es-t，三個 group 各送 60 個 trace | it、imc、unassigned 各 120 段，在 `traces-generic.otel-<group>` |
| 同上 | 同上 | sampler 沒有錯誤，`sent_spans` 360 |
| metrics：Java 改名＋集中式資料寫入憑證 | docker：假 Java agent → Collector（`node.yaml`、`common.yaml` 的處理器，見下）→ es-t | `metrics.http.server.request.duration`、`http.response.status_code` 是數字，寫在 `-imc`；舊名字 0 筆 |
| metrics：保底規則 | 4 分鐘後跑 `python -m detector rules --only http.error_rate_floor,http.latency_p95_floor` | 錯誤率 13%（3/23，對）critical、p95 3.75 秒 warning，寫進 `aiops-anomalies` |
| failure store 在我們的權限下接不接得住 | es-t：只有 `create_doc` 的 key 送 `@timestamp` 壞掉的 log（單筆；bulk 混一筆好的） | ES 回 201、`failure_store: used`；好的照寫 |
| 同上 | 同上 | `k8s-it-c1::failures` 2 筆 `document_parsing_exception`；**寫入 key 不用加權限** |
| 讀 failure store 的權限 | 只有 `read` 的 key 讀 `::failures` | 403；要 `read_failure_store` |
| `quality.ingest_rejected` 抓不抓得到 | 用新的 AIOps baseline key 跑 `quality_hits` | 「有資料被 ES 拒收（在 failure store）：k8s-it-c1、document_parsing_exception，最近有 2 筆」 |
| `window` 規則的邏輯 | 單元測試 | 超過門檻報、請求數不到 20 不報、沒值不報、訊息用 % 和秒；200 個測試全過 |

- common+node 卡住的原因：kubelet、cAdvisor 要讀 serviceaccount，測試機沒有。
- 權限測的五種：`k8s-it-c1`、`k8s-imc-x`、`metrics-a.otel-imc`、`traces-a.otel-unassigned`、`logs-generic.otel-it`。
- metrics 測試：假 Java（Prometheus 格式，每 5 秒 20 個成功、3 個 500 在 2.5～5 秒）；Collector 用 `metricstransform/java-http`、`transform/java-http`、`cumulativetodelta`、`otlp_http/es`（程式從 repo 檔案讀出來組設定）。

**沒測到**：k3s 端對端（11 支 e2e 腳本改了沒跑）；權限被拒（403）不會進 failure store，只靠 `*_stopped`；`window` 規則在大量服務時的查詢時間。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| key 失效（作廢、過期）時所有 group 一起停 | 改 `keyfail` 情境時 | 之前只停一個 group；現在整個 cluster 的資料都進不來 | 換 key 一律用 `rotate-writer-key.sh`；不要手動刪 key |
| 失去「IT 的 key 寫不進 IMC」 | 設計時 | Collector 設定改壞（`transform/group` 寫錯）時資料寫進錯的 group，ES 不擋 | 接受；`quality.log_unassigned_share` 等會看到比例變化 |
| 所有 group 共用一個佇列 | 改出口時 | 之前每個 group 各 10,000 批；現在一個 group 暴量，其他 group 也會被丟 | 盯 `otelcol_exporter_queue_size`；真的遇到再分 |
| 權限被拒（403）不進 failure store | Check | key 的 pattern 漏了、key 被作廢時，Collector 不會報 | 作廢靠 `*_stopped` 和部署檢查清單 |
| 保底規則的門檻（5%、2 秒）是估的 | 沒有真實資料 | 報表 API 本來就 >2 秒的會一直報 | 正式環境只寫入、不通知跑 2～4 週 |
| failure store 的保留天數用 ES 預設 | 沒設 | 預設 30 天 | 夠用 |

- key 失效時 401 會丟資料（[es-startup-401](2026-10-es-startup-401.md)；見 troubleshooting：key 作廢期間資料整批不見）。
- `rotate-writer-key.sh` 先換新、確認都在用才作廢舊的，實測不掉。
- failure store 只接 pipeline、mapping 失敗；key pattern 漏了時 ES 回 200（OTLP partial success），作廢時回 401。
- 一把寬 pattern 的 key 已讓「漏了 pattern」幾乎不會發生；部署檢查清單＝新 cluster 上線後確認 ES 有資料。
- 保底規則要排除的用 `match` 或改門檻。
- failure store 天數要改在 template 的 `data_stream_options.failure_store.lifecycle`。
- **決定**：寫入用集中式資料寫入憑證；資料依 group 分 data stream、讀取依 group 分權限照舊。
- **下一輪**：k3s e2e 跑一次；告警出口（[⑧]）。
