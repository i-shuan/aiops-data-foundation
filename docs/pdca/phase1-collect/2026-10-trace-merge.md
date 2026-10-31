# trace 不取樣、一個請求合成一筆（2026-10）

**狀態**：進行中　　**日期**：2026-10-31　　**相關**：分支 `claude/trace-merge`；取代 [tail-sampling 的設計](2026-10-trace-durability.md)、[durability](2026-10-durability.md) 裡 trace-sampler 那段
**模組**：①②③（收、存）、⑦（根因分析讀 trace）

## Plan：這一次要達到什麼

- **需求**：製造業的事故檢討不能少資料，trace 不能在收值端取樣（現在正常的只留 5%）。另外每個請求在 ES 是 5～8 筆 span 文件，查一個請求要撈多筆再接起來，希望一個請求一筆。
- **目的**：
  1. **100% 收**：拆掉尾端取樣（trace-sampler），node Collector 的 trace 直接寫 ES。
  2. **一請求一筆**：每個 trace 合成一份文件（`traces-merged-<group>-<日期>`，`_id` = trace_id），裡面每一段完整保留（`nested`），再加上層摘要（入口、總耗時、有沒有錯、經過哪些服務），查一個請求一筆就看完、依服務／API 挑請求不用先撈 span。
  3. 根因分析改讀合成的那份；trace 每一段的 baseline 照舊讀原始 span（它只看最近 10 分鐘）。
- **做法（決定）**：合併不在收值端、在 ES 端由 engine 做。Collector 照原本的路（磁碟佇列、重送不重複、停機不掉，三輪 PDCA 驗過的）把每一段寫進 `traces-generic.otel-<group>`（只留 **2 天**）；engine 的排程器每分鐘跑 `detector trace-merge`：找這一分鐘有新 span 進來的 trace、讀回它全部的 span、合成一筆 upsert。晚到的 span 會讓那個 trace 再被合一次（覆蓋），所以合得完整；重跑不會重複。
  - 不寫新的收值服務（自己寫的 OTLP 接收端要重做磁碟佇列、重送去重、停機測試）。
  - 不用 ES transform（`scripted_metric` 組 nested 難寫難測；continuous transform 寫不進 data stream）。
- **範圍**：`collector/`（拆 trace-sampler）、`elasticsearch/code`（raw 2 天、merged 的 template／ILM／pipeline、角色）、`aiops-engine/code`（`detector trace-merge`、讀端改讀 merged、排程表加一列）、e2e（trace、trace-baseline、rca、kafka、sdk-inject 改；tail-sampling 刪）、文件（trace README、retention 三份、architecture、collector README）。
- **不做**：MinIO／冷儲存（另案）；trace 每一段的 baseline 改讀 merged（它只看最近 10 分鐘，raw 夠；要改要把 composite 聚合改成 nested，這次不碰）。

| 取捷 | 說明 |
|---|---|
| 容量 | 100% 收：文件的估算低流量服務 7 天 1.2GB → 12GB、高流量 39GB → 387GB。合成一筆用 `nested` **不會省**（ES 內部每段仍是一筆隱藏文件），省的只有文件固定開銷。這是使用者的決定：收值不省，之後用保留天數與冷儲存處理 |
| raw 多留 2 天 | 合併前的 span 要在 ES 待一下；2 天內的量是雙份 |
| 合併晚 1～2 分鐘 | 排程器每分鐘跑；根因分析（alert 每分鐘）看到的 merged 是 1～2 分鐘前的 |
| 格式不再是 OTel 原生 | merged 是自訂格式；raw 那 2 天仍是 OTel 原生（Kibana 能看） |
| 讀端兩套 | 根因分析讀 merged（事故可能幾天後才查），trace baseline、資料品質讀 raw（只看最近） |

## Do：改動哪些 code、改了什麼、為何要改

| 哪裡 | 改了什麼 | 為什麼 |
|---|---|---|
| `collector/code/node.yaml`、`helmchart/` | 刪 `loadbalancing/traces` exporter，trace pipeline 直接送 `elasticsearch/es`；刪 `sampler.yaml`、`trace-sampler.yaml`（StatefulSet）、values 的 `traceSampler`、configmap／helpers 的 sampler 項 | 100% 收，不再有取樣那一跳 |
| `elasticsearch/code/storage/pipeline-traces-otel.json` | 去掉 `reroute` 到 traces-problem；加 `event.ingested` | 沒有分流了；`trace-merge` 靠 `event.ingested` 找「這一分鐘有新 span 的 trace」（`@timestamp` 是 span 開始時間，晚到的找不到） |
| `elasticsearch/code/storage/template-traces-merged.json`、`ilm-traces-merged.json`（新） | index template `traces-merged-*`：上層摘要欄位、`entries`／`spans` 用 `nested`、`attrs`／`resource` 用 `flattened`；ILM 7 天刪 | 一個請求一筆；屬性欄位名稱不固定，用 `flattened` 不會把 mapping 撐爆 |
| `elasticsearch/code/storage/traces-otel-custom.json` | mapping 加 `event.ingested: date` | ES 內建 OTel 模板根層 `dynamic: false`：pipeline 寫進去的 `event.ingested` 只在 `_source`、沒被索引，`trace-merge` 查到 0 筆（Rancher Desktop 實測第一輪發現） |
| `elasticsearch/code/scripts/elasticsearch-setup.sh` | 加 `rollover_if_no_field 'traces-*.otel-*' event.ingested` | mapping 只對新的 backing index 生效，已存在的資料流要換一個 index |
| `elasticsearch/code/storage/ilm-traces-otel.json`、`scripts/retention.conf` | raw 改 `TRACES_RAW_DELETE_AFTER=2d`；刪 `ilm-traces-problem-otel.json`、`traces-problem-otel-custom.json`、`PROBLEM` 參數 | raw 只是合成前的暫存 |
| `elasticsearch/code/scripts/elasticsearch-setup.sh` | 裝 merged 的 template／ILM；各部門唯讀角色加 `traces-merged-<group>-*`；aiops-reader 讀 `traces-merged-*`；baseline key 可寫 `traces-merged-*`；刪 traces-problem 區塊 | 根因分析、Grafana 讀 merged；只有 engine 能寫 merged |
| `aiops-engine/code/detector/trace_merge.py`（新） | `updated_trace_ids`（composite 聚合 `event.ingested` 範圍）→ 讀回全部 span → `build()` 合成一筆（入口、總耗時、錯誤、服務、段數、`entries`、`spans`）→ bulk upsert `_id`=trace_id；進度存 `aiops-state/trace-merge`；`run()` 從上次合到的時間接著合、最多回補 2 天、10 分鐘一段 | 晚到的 span 讓整個 trace 重合一次（覆蓋），合得完整、重跑不重複；pod 停很久能補 |
| `detector/__main__.py`、`cmd_trace.py` | 子命令 `trace-merge --config --window --at` | 排程器呼叫 |
| `configs/schedule/schedule.yaml` | 加 `trace-merge`（stage 1、每分鐘、timeout 50 秒） | 跟 alert 同一分鐘跑完 |
| `configs/baseline/trace.yaml` | 加 `raw_index`、`merge.settle_seconds: 10`；刪 `sample_policy` | 合到「現在 − 10 秒」，讓 Collector 剛寫進去的 span 進得了這一輪；沒有取樣就沒有 policy 要過濾 |
| `configs/rca/rca.yaml` | `indices.traces: traces-merged-*` | 根因分析讀 merged |
| `core/traces.py`、`investigator/es.py` | 依 index 名稱分 raw／merged 兩條路：merged 用 `nested` 查 `entries.*`、`spans.span_id`，`from_merged()` 展回原本的 span 列表 | 根因分析其餘程式不用改 |
| `detector/trace.py` | `policy` 註解更新，功能保留 | 沒有標記的 span 照樣抽 |
| e2e | `run-trace-e2e.sh` 加「7b. 合成一個請求一筆」（跑 `trace-merge`、檢查 merged 文件／摘要／entries／重跑不重複／權限）；`run-rca-e2e.sh` 在 investigate 前手動跑一次 `trace-merge`（測試 cluster 沒有 engine pod）、`tests/rca_k3s.yaml` 讀 `traces-merged-*`；`deploy-collector.sh`、durability、stress、keys、sdk-inject、kafka 去掉 trace-sampler；刪 `e2e/k8s/tail-sampling/`；`trace_quality_edge_e2e.py` 的尾端取樣模擬改成全留，並刪掉三項測隨機取 5% 的檢查（基準線只用 random 兩項、錯的慢的全留的對照、低流量 API 不學）與只給它們用的函式 | 跟著程式；這三項測的行為已不存在 |
| `aiops-engine/helmchart`（順手，不屬 trace） | `llmProvider` 改成 `llm.{provider,model,baseUrl,apiKey}`：前三個進 ConfigMap `aiops-env`（`LLM_CHAT_MODEL`、依 provider 給 `OPENAI_BASE_URL`／`ANTHROPIC_BASE_URL`），`apiKey` 由 chart 建 Secret `aiops-llm`；key 只寫在 `values-local.yaml`（新加進 `.gitignore`，附 `values-local.example.yaml`） | 原本 chart 沒地方設模型與閘道，只能用程式預設與公網；使用者要 key 跟著 yaml 走、但不能進 git |
| e2e（順手） | `run-metrics-e2e.sh`、`run-es-2node.sh` 的 kube context 預設改成目前的（原本寫死 `default`，Rancher Desktop 叫 `rancher-desktop`，第一個 kubectl 就失敗） | 同上 |
| e2e（順手） | 10 支腳本的 `${repo}/engine` 改成 `${repo}/aiops-engine/code`；`run-rca-e2e.sh` 建 `rca-lib` configmap 時去掉 `\r` | 元件重整（PR #34）時漏改，這些 e2e 從那之後其實跑不起來；Windows checkout 成 CRLF 時 busybox sh 讀 `lib.sh` 會 syntax error（trace-app 早就這樣處理，rca 漏了） |
| 文件 | `docs/trace/README.md` 重寫；architecture、retention 三份、rca、troubleshooting、根目錄與 docs README、sdk 埋點指引改成 100% 收／raw／merged | 跟著程式 |
| `core/traces.py` | `to_span` 的 duration 改用 `_duration_ms`：字串、負數、沒有一律當 0 | 排程器全跑時發現：測試 ES 裡有 duration 是 "not-a-number" 的 span（ES 的 `ignore_malformed` 收下來），`trace-merge` 除錯直接崩潰，狀態推不進去，之後每分鐘都失敗。一個壞 SDK 就能卡死合併 |
| e2e（順手） | `rca-demo/notify-sink.yaml` 的 Service 補 `ports`；`run-alert-flap.py`、`run-log-trace-skew.py`、`metrics_trace_check.py` 的 `engine` 路徑改 `aiops-engine/code`；`loop_e2e.py` 的 metrics 查詢區間結尾改成明天；`test_deploy.py` 預期多一個 Secret（`aiops-llm`） | 元件重整（PR #34）漏掉的；`loop_e2e` 把回饋時間（寫入當下）算在固定的 10/05～10/28 外，過了 10/28 就一定失敗 |
| e2e（順手） | `metrics_trace_check.py`：entry_* 查詢改讀 `traces-merged-*`、檢查前先跑一次 `trace-merge`，取 raw span 的 `traces()` 改用欄位條件；`run-metrics-scenarios.sh` 的 kube context 預設改成目前的 | 這支腳本用的 `entry_filter` 是 merged 的 nested 查法，對 raw 查全是 0 筆（第 3 串 11 項 FAIL，改後 15 項全過） |
| e2e（順手） | `run-dotnet-e2e.sh` 的 baseline 查詢窗口從「對齊 5 分鐘、往前 10 分鐘」改成「現在往前 1 分鐘、往前 15 分鐘」 | 第 3 串 dotnet 1 項 FAIL（`http.* 0 None`）：應用只跑約 4 分鐘，窗口對齊 5 分鐘時資料可能全落在最後一個還沒結束的窗口外，查到 0 列。不是 baseline 算出 0，也和 trace 無關；拿 ES 裡留著的 dotnet-app 資料，用同一段 `fetch` 重現：窗內有資料時三條序列都有值，窗內沒資料時印出一模一樣的 `0 None`。**只重現了原因，沒有整支重跑**（約 20 分鐘） |
| `feedback_web/app.py` | 拿掉送出回饋後多打的 `POST aiops-feedback/_refresh` | ⑧⑨ 實跑發現：用最小權限的 key（沒有 `maintenance`）時這一下回 403，資料其實已寫入，使用者卻看到「沒有存成功」。`bulk_index` 本來就 `refresh=wait_for`，這一下是多餘的 |

單元測試 `tests/test_trace_merge.py`：合成的摘要（入口、耗時、錯誤、服務）、group 沒有時退回 dept、`from_merged` 來回一致、讀端的 nested 查詢；全部 377 個測試通過。

## Check：測試的目的與結果

環境：Rancher Desktop（單節點）、ES 8.19.23、Collector chart 從 `collector/code` 帶設定。2026-10-31。

| 測什麼 | 怎麼測 | 結果 |
|---|---|---|
| 拆掉 trace-sampler 後 Collector 還正常 | `run-e2e.sh`（log 路線 54 項）；新 chart 裝上去 StatefulSet 消失，兩顆 `queue-trace-sampler-*` PVC 手動刪 | 全過 |
| trace 100% 直接進 raw | `run-trace-e2e.sh` 1～7：三個 namespace 的 trace-app 各送、依 group 分到 `traces-generic.otel-<group>`、冒充擋掉、log 用 trace_id 串得起來、重送同一個 span 只有一筆 | 全過 |
| 合成一個請求一筆 | `run-trace-e2e.sh` 7b：手動跑 `detector trace-merge --window 15min`，查那個 trace 在 merged 裡 `_id` = trace_id、index 是 `traces-merged-it-2026.10.31`、上層摘要（root service／name／kind、group、span_count、error）、`entries` 是 Server span、`spans[].attrs` 保留、送 3 次的 span 合成後 `span_count` 仍是 1 | 全過；一輪合了 161 個 trace |
| raw 的每個 trace 都合成一筆 | 同一段時間（測試開始～60 秒前）raw 的 trace_id 數 vs merged 筆數 | 21 = 21 |
| 重跑不重複 | 再跑一次 `trace-merge`，同一個 trace_id 仍只有 1 筆 | 過 |
| 權限 | baseline key 可寫 merged、Collector 的寫入 key 不可、IT 唯讀角色含 `traces-merged-it-*` | 全過 |
| 根因分析讀 merged | `run-rca-e2e.sh`：正常 7 分鐘 → 事故 5 分鐘 → 手動 `trace-merge`（一輪合了 1,531 個 trace）→ investigate 不指定服務、`tests/rca_k3s.yaml` 讀 `traces-merged-*` | 11 項全過：從異常找到 rca-mes-api、瓶頸與錯誤源頭都是 Oracle 那段、最慢那個請求兩個服務的 log 都撈到、呼叫關係 mes-api → wip-worker → oracle |
| 單元測試 | `pytest`：合成的摘要、group 退回 dept、`from_merged` 來回、讀端 nested 查詢、排程表多一列 | 376 passed |
| 排程器每分鐘跑 `trace-merge` | `run-schedule-e2e.sh`（20 分鐘）：第一次 19 次全失敗（壞 duration，見 Do），修好後重跑 | （重跑後補） |

第一輪實測 7b 合了 0 個 trace：raw 的 `event.ingested` 沒被索引（內建 OTel 模板根層 `dynamic: false`），補 mapping 並 rollover 後才過；另外兩項是測試本身的錯（jq regex 多跳脫一層；比數量時沒排除還在 Collector 批次裡的最後幾十秒），都已修。

## Act：Check 後沒改善的、還沒解決的

- **升級那一刻會漏合**：setup 把舊 raw 資料流 rollover 之前寫進舊 index 的 span 沒有索引的 `event.ingested`，`trace-merge` 看不到；實測從 setup 到 rollover 之間約 9 個 trace 沒合成。只影響第一次升級，之後不會；要補可以對舊 index 跑一次 `update_by_query`（這次不做）。
- **根因分析慢 1～2 分鐘**：alert 每分鐘跑，看到的 merged 是上一分鐘合好的。事故當下要看即時的查 raw（2 天內）。
- **merged 不省空間**：`nested` 的每一段內部仍是一筆；100% 收之後容量靠保留天數（raw 2 天、merged 7 天）撐，冷儲存（MinIO）另案。
- **trace-quality 的 6 項失敗**（`tests/trace_quality_edge_e2e.py`，自己的 ES 灌合成 trace）：3 項在測已廢止的「隨機取 5%」，已從測試刪掉；其餘查出的原因如下，**都還沒改**。

  | 失敗項 | 原因 | 改的話 |
  |---|---|---|
  | 只有 10% 的請求簡訊多 2 秒，沒報 | 以前尾端取樣把「慢的」分到別的 policy，baseline 只學 random，很乾淨；現在 100% 收，平常 3% 的長尾（1.5～3 秒）進了 baseline，每個點只抽 20 個請求，平均值的標準差約 86 ms，+200 ms 只有約 2 個標準差，過不了 z=5 | 每點抽 100 個：標準差約 38 ms，報得出來。要不要調 `trace.yaml` 的 `sample_per_bucket`（正式是每 5 分鐘 20 個）看讀取成本 |
  | 長期 2% 吞例外，`swallowed` 規則與 `swallowed_rate` 都報了 | 規則是每個點各自算：20 個裡只要 1 個吞例外就是 5%，剛好等於門檻 5%。2% 的真實比例，每個點有 33% 機率抽到 1 個，3 個點湊起來多半會報 | 每點抽 100 個後兩項都不報。或規則要求吞例外至少 2 個才算 |
  | B6a：三成的 Pod 沒有服務 label，`quality.log_missing_identity` 沒報 | Pipeline 沒 label 時 `service.name` 退回 container 名稱（`app`），所以欄位不是「缺」，檢查看不到（120 筆 `service.name=app`）。`pipeline.painless` 與 `quality.yaml` 本分支沒動，master 上一樣 | 要偵測得另外寫（例如 `service.name` 等於 container 名稱算缺），不是 trace 的問題 |

  實驗：只把測試用的 `sample_per_bucket` 暫時改成 100，A 段 17 項全過，改回後沒留在 repo。目前單獨跑 A 段 14 過 3 失敗（上面前兩列）。單獨跑 B 段 19 過 2 失敗：B6a，以及 B8（unassigned 的 log 比例沒報）；B8 在整串完整跑時是過的，單跑時失敗，推測是測試 ES 裡剛跑完 durability 留下大量其他 log、把分母墊大，沒查證。
- `aiops-trace-segments`（trace 每一段的 baseline）還是讀 raw、只看最近 10 分鐘；要改讀 merged 得把 composite 聚合改成 nested，這次不碰。
- **ES 8.19 的 otel 資料流 failure store 預設沒開**：`elasticsearch-setup.sh` 的註解寫「ES 預設就開了」，8.19.23 實測 `traces-*.otel-*`、`metrics-*.otel-*` 都是關的（只有腳本自己打開的 `k8s-*` 是開的）。`trace_quality_edge_e2e.py` 灌壞時間的 span 因此被拒絕、測試中斷；資料品質的 `ingest_rejected` 在 8.19 也不會有值。上線前要確認正式 ES 的版本，或讓 setup 對 otel 資料流也打開。
- **ES 停 180 秒：log 原本就會掉，trace 這次起也會掉（回歸）**：同一個測試 ES（記憶體約 97%）、同一個情境，master 與本分支對照。

  | 版本 | log 遺失／送出 | trace 遺失／送出 |
  |---|---|---|
  | master（有 trace-sampler） | 9,649／71,996 | 0／7,199 |
  | 本分支（trace 直寫 ES） | 7,715／71,994 | 2,552／7,199 |

  原因：Collector 的 `elasticsearch` 元件遇到連線逾時（`dial tcp … i/o timeout`）直接丟資料，不重試，`retry` 只管 HTTP 狀態碼。log、metrics 本來就走這個元件所以本來就掉；trace 以前走 `loadbalancing` 到 sampler，那一段自己無限重試、佇列在節點磁碟，所以不掉。trace 改直寫後繼承了同一個弱點。要處理得讓 ES 元件對連線錯誤也重試（對 log 一起有效），或讓 trace 回到有重試的出口；還沒做。
