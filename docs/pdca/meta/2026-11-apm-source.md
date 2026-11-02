# aiops-engine 讀 Elastic APM 的資料（2026-11）

**狀態**：已完成（程式與設定；未在真實 APM 資料上跑）　　**日期**：2026-11-02　　**相關**：分支 `claude/apm-source`；[aiops-engine/code/configs/apm/README.md](../../../aiops-engine/code/configs/apm/README.md)
**模組**：④ 判斷、⑤ 異常總表、⑦ 根因分析

## Plan：這一次要達到什麼

- **需求**：公司的 ES 裡已經有 Elastic APM（Fleet 的 `apm` integration）收的資料：`traces-apm-*`、`logs-apm.app.*`、`logs-apm.error-*`、`metrics-apm.transaction.1m-*`、`metrics-apm.service_destination.1m-*` 等。要在**不改 Collector、不改 ES 現有索引**的前提下跑 aiops-engine。engine 現在讀的是我們 Collector 寫的 OTel 格式（`metrics-*.otel-*`、`k8s-*`、`traces-generic.otel-*`），欄位名、索引名、直方圖單位都不同。
- **目的**：設 `source: apm` 部署 engine，就能用 APM 的資料學基準線、判斷、合成 trace、根因分析、分件告警；哪些功能在 APM 資料上做不到，寫清楚。
- **範圍**：
  - 做：metrics／log 序列、規則、Pod 同儕比對、trace-merge、根因分析的 log 讀取、dept／group 歸屬改成能換資料來源；一套 `configs/apm/`；Helm 一個開關；ES role 的正確寫法。
  - 不做：資料品質檢查（`quality.yaml`，整份是 OTel 專用）、慢性病清單、trace baseline 的 raw 模式（只走 merged）、把 `logs-apm.error` 的例外接回 span（例外吞噬檢測）。

### 盤點：engine 讀哪裡、哪些寫死

| 讀什麼 | 現在怎麼決定 | APM 要怎麼辦 |
|---|---|---|
| metrics、log 的序列（series／rules／peer） | 全在 YAML：`index`、`filter`、`by`、`value` | 另一套 YAML；直方圖是微秒，要加 `scale` |
| 往外呼叫的平均耗時 | 沒有「兩個欄位總和相除」的 value | 加 `div` |
| raw trace 的欄位（`trace_id`、`parent_span_id`、`duration` 奈秒、`kind`） | 寫死在 `core/traces.py` | 加 APM 的轉換，依索引名選 |
| trace-merge 找有新 span 的 trace | 寫死 `trace_id` | 依 schema 選欄位 |
| dept／group 歸屬 | 寫死 `metrics-*.otel-*`、`resource.attributes.*` | 依來源 profile 選 |
| 根因分析讀 log | 寫死 `resource.attributes.service.name`、`trace_id`、`log.level` | 依來源 profile 選 |

### APM 的資料對到 engine 要的

| engine 要的 | APM 哪裡有 | 差別 |
|---|---|---|
| 每支 API 的請求數、錯誤率、p95、慢請求比例 | `metrics-apm.transaction.1m-*` 的 `transaction.duration.histogram`（ES histogram）、`event.outcome`、`_doc_count` | 已是每分鐘的增量，不用 cumulativetodelta；值是**微秒**；桶是 HDR（寬約值的 1%），沒有固定邊界 |
| 服務、API | `service.name`、`transaction.name`（"GET /api/x"，含 method） | 沒有分開的 method |
| 往外呼叫（DB、下游） | `metrics-apm.service_destination.1m-*` 的 `response_time.count`、`response_time.sum.us`、`span.destination.service.resource` | 沒有直方圖，只能算平均 |
| log 行數、錯誤數 | `logs-apm.app.*`（`service.name`、`log.level`、`message`、`trace.id`） | 只有有開 log shipping 的服務才有；例外在 `logs-apm.error-*` |
| trace | `traces-apm-*`：`processor.event` transaction／span、`trace.id`、`span.id`／`transaction.id`、`parent.id`、`*.duration.us`、`event.outcome`、`span.destination.service.resource`、`span.db.statement` | 沒有 OTel 的 `kind`，要從 transaction／span 與 `span.type` 推 |
| Pod CPU、記憶體 | `metrics-apm.app.*`（agent 的 system metrics） | 公司的資料流清單裡**沒有**這條，先不做 |
| dept、group | `labels.dept`、`labels.group`（agent 要設） | 沒設就全部 unassigned |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `core/source.py`（新） | 來源 profile：`otel`、`apm` 各一組「owner 查哪個索引、哪幾個欄位」「log 的欄位、哪些算 problem」「raw trace 的 trace id 欄位」；環境變數 `AIOPS_SOURCE` 選 | 這幾個欄位名寫死在三個模組裡，呼叫端沒有 cfg 可傳；一個開關比到處傳參數清楚，也跟 chart 的 `source` 對得上 |
| `core/traces.py` | `schema_of(index)` 分 otel／apm／merged；`_apm_span` 把 transaction／span 轉成同一種 span（`trace.id`、`parent.id`、`*.duration.us` 微秒→毫秒、`event.outcome` 當 error、`span.destination.service.resource` 當 target、`span.db.statement`→`db.statement`、`kubernetes.pod.name`→`k8s.pod.name`、`labels.*`）；`kind` 從 transaction／`span.type` 推；`spans_of`、`spans_by_id` 對 APM 用 `trace.id`、`span.id`／`transaction.id`，分頁次序 `_doc` 並依 span_id 去重 | 這是唯一一處讀 raw span 的地方；轉成同一格式後 trace-merge、trace baseline、根因分析一行不用改。transaction 沒有 `span.id`，分頁的次要排序只能用 `_doc`，跨頁可能重複所以去重 |
| `detector/es.py` | 序列可設 `scale`（有單位的值乘、`hist_ratio_above` 的 `gt` 除回去；比例、次數不動）；新 value `div`（兩個欄位的總和相除，`num` 可加 query） | APM 的直方圖是微秒，不加 `scale` 的話 `min_abs`、門檻、`gt` 全要寫微秒、跟 OTel 版對不起來；`service_destination` 沒有直方圖，平均耗時只能 `sum.us ÷ count` |
| `detector/owner.py` | 索引、namespace／service／dept／group 欄位改讀 `source.profile("owner")` | APM 的 dept 在 `labels.dept`，索引是 `metrics-apm.transaction.1m-*` |
| `detector/trace_merge.py` | 找有新 span 的 trace 時 trace id 欄位依索引名（`trace_id_field`） | APM 是 `trace.id` |
| `investigator/es.py` | log 的欄位、problem 的 level 清單、額外的 problem query 改讀 `source.profile("log")`；訊息依序試 `message`、`error.exception.message`、`error.log.message` | APM 的 log 是 `service.name`、`trace.id`；`logs-apm.error-*` 的例外文件沒有 `log.level`，用 `processor.event: error` 一起撈才看得到例外 |
| `investigator/__main__.py` | `_route_of`：keys 只有 `route` 沒有 `method` 時直接用 `route` | APM 的 `transaction.name` 本來就是 "GET /api/x"，序列的 keys 沒有 method，否則根因分析不會限定 API |
| `configs/apm/`（新）：`series.yaml`、`rules.yaml`、`peer.yaml`、`trace.yaml`、`rca.yaml`、`schedule.yaml`、`README.md` | APM 版的設定：序列 11 條（HTTP 5、往外呼叫 3、log 3）、規則 2、peer 2、trace 的 raw 是 `traces-apm-*`、rca 的 log 是 `logs-apm.app.*,logs-apm.error-*`、排程少 quality 與 chronic；README 寫 ES role、template、`source: apm`、欄位對照、能用與不能用 | 寫法跟 baseline 一樣，可以逐條對照；用不到的（K8s 狀態、kubelet、JVM／.NET runtime）不列，免得每分鐘查空 |
| `helmchart/values.yaml`、`templates/env.yaml`、`templates/deployment.yaml` | `source: otel|apm` → 環境變數 `AIOPS_SOURCE`，排程表換成 `configs/apm/schedule.yaml` | 一個開關換整套 |
| `tests/test_apm.py`（新） | 見 Check | |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| APM 的 transaction／span 轉得對 | 一個 transaction（含 `timestamp.us`、`labels`、`kubernetes.pod.name`）、三個 span（db、external http、app）；看 kind、target、parent、耗時、error、attrs、resource | transaction → Server、300000 µs → 300 ms、`start_ms` 用 `timestamp.us`；db span → Client、target `oracle`、段名 `wip-worker › SELECT LOT_HISTORY → oracle`、`db.statement` 在 attrs；external → Client；app → Internal 無 target；messaging transaction → Consumer、send → Producer |
| 轉完能合成一筆、根因分析的函式照用 | 四個 span 丟 `trace_merge.build` 再 `from_merged`、`error_origins` | index `traces-merged-it-2026.11.02`（group 用 `labels.group`）、entries 只有 transaction、`error` 為真、錯的源頭是 db span |
| OTel 的轉換沒被改到 | 原本的 OTel span 例子 | target `oracle`、2_000_000 ns → 2 ms，跟以前一樣 |
| 讀 `traces-apm-*` 時查對欄位 | 假 ES 記下查詢 | `terms trace.id`、排序 `@timestamp` + `_doc`、`_source` 排除 `span.stacktrace`、同一筆重複回來只留一個；`spans_by_id` 用 `span.id` 或 `transaction.id`；trace-merge 的 composite 用 `trace.id` |
| `scale`、`div` 算得對、不該乘的沒乘 | 直接算 `_value_aggs`／`_bucket_value` | p95 250000 µs → 0.25 s；`gt: 2.0` 查 2000000；比例、次數不乘（第一版 `hist_count` 掉進通用分支被乘成 0.00126，測試抓到、已修）；`div` 21309833 ÷ 7488 µs ≈ 2.8 ms、事件數是 7488、分母 0 是 None；帶 query 的 `div` 是 10 ÷ 200 |
| `AIOPS_SOURCE=apm` 時 owner、log 用 APM 欄位；預設不變 | 假 ES 記下查詢 | owner 查 `metrics-apm.transaction.1m-*`、`service.name`、`labels.dept`，異常寫回帶 (D3120, it)；`problem_logs` 是 `service.name` + （level 清單含 `ERROR` 或 `processor.event: error`），例外文件的訊息從 `error.exception.message` 取；`logs_of_traces` 用 `trace.id`。`otel` 時查詢跟改之前一字不差 |
| 沒弄壞別的 | `python3 -m pytest -q` | 562 個全過（含新加 17 個）；先前 3 個 `test_deploy` 失敗是 Windows 終端 cp950 編碼，`PYTHONUTF8=1` 重跑就過，master 上同樣 |
| 設定檔載得起來、排程指到的檔案都在 | 載入六份 YAML，每條序列、規則組聚合；排程每個 `--config` 檢查存在 | 11 條序列、2 條規則、2 個 peer 都組得出來；`slow_ratio` 的 `gt` 查 2000000；11 個排程工作的設定檔都在 |
| chart 兩種 `source` 都 render 對 | `helm template` 預設與 `--set source=apm` | 預設 `AIOPS_SOURCE=otel`、`configs/schedule/schedule.yaml`；apm 時 `AIOPS_SOURCE=apm`、`configs/apm/schedule.yaml` |
| 文件 | `tools/check-docs.py` | 連結、索引、舊說法、類型 0 個問題 |

**沒測到的**：沒有真實 APM 資料，整條沒在 ES 上跑過。APM 的欄位名是照 Elastic 官方資料模型文件（8.8 的範例）寫的，README 列了要對樣本核對的欄位。`metrics-apm.transaction.1m` 的 `event.outcome` 是每筆文件的維度（成功、失敗各一筆直方圖），`hist_ratio` 依此算錯誤率，推論正確但沒實測。`service.node.name` 在 K8s 上是不是 pod 名要看 agent 設定。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 例外吞噬檢測在 APM 資料上不會觸發 | APM 的例外是 `logs-apm.error-*` 的獨立文件（帶 `transaction.id`、`parent.id`），span 上沒有 `exception` | trace 每一段的 `swallowed_rate` 永遠 0 | 要的話 trace-merge 多查一次 error 文件接回 span |
| 沒有 Pod CPU、記憶體 | 公司的資料流清單沒有 `metrics-apm.app.*` | 資源類序列、peer 的 CPU／記憶體、記憶體洩漏規則都沒有 | agent 開 system metrics 後打開 `series.yaml` 註解的兩條 |
| 往外呼叫只有平均，沒有 p95 | `service_destination` 只有 count、sum | 一部分請求變慢時 `dep.latency_avg_s` 不太動 | 靠 trace 每一段的 baseline（`time_ms` 是平均，一樣）；要 p95 得從 `traces-merged` 算 |
| dept／group 要 agent 設 label | APM 沒有 namespace 的 label 注入 | 沒設時告警全進 `unassigned` 的 policy | 各服務的 agent 加 `ELASTIC_APM_GLOBAL_LABELS=dept=…,group=…`，或 engine 加一張服務→部門的表（owners 來源） |
| `logs-apm.app.*` 只有一個服務 | 資料流清單 | `log.lines`、`log.errors` 只學到一個服務 | 其他服務的 agent 開 log shipping；例外靠 `apm.errors` |
| 資料品質檢查、慢性病清單沒有 APM 版 | 範圍外 | quality 的 `ingest_rejected`、`k8s-unassigned` 等全是 OTel 的索引 | 要時另寫 |

- **決定**：trace 的轉換依索引名選、其餘靠 `AIOPS_SOURCE`，不另開「每個欄位都可設」的設定；兩種來源以外（Zipkin、Jaeger 直寫）出現再說。
- **下一輪**：拿一筆真實的 transaction metric、一個完整 trace 的 transaction＋span、一筆 `logs-apm.error` 核對欄位，然後 `detect --window 10m`、`trace-merge` 在公司 ES 上跑一次。
