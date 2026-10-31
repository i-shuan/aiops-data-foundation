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
| `elasticsearch/code/storage/ilm-traces-otel.json`、`scripts/retention.conf` | raw 改 `TRACES_RAW_DELETE_AFTER=2d`；刪 `ilm-traces-problem-otel.json`、`traces-problem-otel-custom.json`、`PROBLEM` 參數 | raw 只是合成前的暫存 |
| `elasticsearch/code/scripts/elasticsearch-setup.sh` | 裝 merged 的 template／ILM；各部門唯讀角色加 `traces-merged-<group>-*`；aiops-reader 讀 `traces-merged-*`；baseline key 可寫 `traces-merged-*`；刪 traces-problem 區塊 | 根因分析、Grafana 讀 merged；只有 engine 能寫 merged |
| `aiops-engine/code/detector/trace_merge.py`（新） | `updated_trace_ids`（composite 聚合 `event.ingested` 範圍）→ 讀回全部 span → `build()` 合成一筆（入口、總耗時、錯誤、服務、段數、`entries`、`spans`）→ bulk upsert `_id`=trace_id；進度存 `aiops-state/trace-merge`；`run()` 從上次合到的時間接著合、最多回補 2 天、10 分鐘一段 | 晚到的 span 讓整個 trace 重合一次（覆蓋），合得完整、重跑不重複；pod 停很久能補 |
| `detector/__main__.py`、`cmd_trace.py` | 子命令 `trace-merge --config --window --at` | 排程器呼叫 |
| `configs/schedule/schedule.yaml` | 加 `trace-merge`（stage 1、每分鐘、timeout 50 秒） | 跟 alert 同一分鐘跑完 |
| `configs/baseline/trace.yaml` | 加 `raw_index`、`merge.settle_seconds: 10`；刪 `sample_policy` | 合到「現在 − 10 秒」，讓 Collector 剛寫進去的 span 進得了這一輪；沒有取樣就沒有 policy 要過濾 |
| `configs/rca/rca.yaml` | `indices.traces: traces-merged-*` | 根因分析讀 merged |
| `core/traces.py`、`investigator/es.py` | 依 index 名稱分 raw／merged 兩條路：merged 用 `nested` 查 `entries.*`、`spans.span_id`，`from_merged()` 展回原本的 span 列表 | 根因分析其餘程式不用改 |
| `detector/trace.py` | `policy` 註解更新，功能保留 | 沒有標記的 span 照樣抽 |
| e2e | `run-trace-e2e.sh` 加「7b. 合成一個請求一筆」（跑 `trace-merge`、檢查 merged 文件／摘要／entries／重跑不重複／權限）；`deploy-collector.sh`、durability、stress、keys、sdk-inject、kafka 去掉 trace-sampler；刪 `e2e/k8s/tail-sampling/`；`trace_quality_edge_e2e.py` 的尾端取樣模擬改成全留 | 跟著程式 |
| 文件 | `docs/trace/README.md` 重寫；architecture、retention 三份、rca、troubleshooting、根目錄與 docs README、sdk 埋點指引改成 100% 收／raw／merged | 跟著程式 |

單元測試 `tests/test_trace_merge.py`：合成的摘要（入口、耗時、錯誤、服務）、group 沒有時退回 dept、`from_merged` 來回一致、讀端的 nested 查詢；全部 376 個測試通過。

## Check：測試的目的與結果

（實作後補）

## Act：Check 後沒改善的、還沒解決的

（實作後補）
