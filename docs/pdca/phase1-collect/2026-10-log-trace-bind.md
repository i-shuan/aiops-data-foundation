# log 的 trace_id／span_id：各種格式、各種值都取對、對得到 trace（2026-10）

**狀態**：已完成（cluster 那段未驗證，見 Act）　　**日期**：2026-10-27　　**相關**：分支 `claude/p1-logtrace-59c3aa`；[ingest-keys-check](2026-10-ingest-keys-check.md)（log↔trace 綁定的第一輪）、[docs/log/README「trace_id 怎麼取」](../../log/reference.md#trace_id-怎麼取)
**模組**：① 收值、② 清資料、⑦ 根因分析

## Plan：這一次要達到什麼

- **需求**：上一輪只驗了「各格式拿不拿得到 trace_id」。值本身不對（大寫、全 0、長度錯）、檔案和 OTLP 同時開、log 時間跟 span 差很多時，綁出來的對不對沒驗過。
- **目的**：每種格式、每種值都有預期結果和可重跑的案例；綁錯的修掉。
- **範圍**：中央 pipeline（`k8s-logs-otel` → `k8s-logs` → `k8s-logs-fields`）、Collector 的 `file_log`／`logs/otlp`、investigator 依 trace_id 撈 log。不做 Sleuth 風格純文字（`[trace,span]`）的解析規則。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `telemetry/storage/pipeline-fields.painless` | trace_id／span_id 正規化（規則見 log/README） | 改前大寫、空白、全 0、格式錯的都原樣寫進 `trace.id`：對不到、或對到不存在的 trace |
| 同上 | 格式錯的原值放 `app.trace_id_invalid`／`app.span_id_invalid` | 不丟資料，查得到哪個服務印錯 |
| 同上 | OTLP record 的 trace_id 也走同一套；原文的不合法才用 record 的 | 改前原文的錯值會蓋掉 record 的正確值 |
| `telemetry/storage/pipeline-otel.painless`、`pipeline.painless`、`component-mappings.json` | 新欄位 `log.source`：`file`／`otlp` | 開 otlp 時同一行兩份；`agent.type` 兩份都是 `otelcol`，分不出來 |
| `engine/investigator/es.py` | `logs_of_traces` 的時間窗前後多放 30 分鐘 | log 時間是應用的；離 span 10 分鐘的 log 撈不到 |
| `e2e/pipeline/trace-id-cases.json`、`run-trace-id-cases.sh`（新） | 33 個案例＋檔案／OTLP 兩份，走 `k8s-logs-otel` 的 `_simulate` | 不用 cluster，可重跑 |
| `e2e/pipeline/run-log-trace-skew.py`（新） | 早／晚 10 分鐘的 log 寫進 ES，用 investigator 撈 | 時間差要真的寫進 ES、真的查才測得到 |
| `e2e/local-k3s/logtrace-app.sh`、`run-trace-id-e2e.sh`（新） | 經過真的 Collector：大寫、超過 16KB、格式錯、背景、兩份 | containerd 切段、otlp 兩份要 Collector 才測得到 |
| `docs/log/README.md`、`docs/trace/README.md`、`docs/troubleshooting.md` | trace_id 規則表、`log.source`、排錯 | |

## Check：測試的目的與結果

`_simulate` 在 nerdctl 的 ES 9.5.3（`elasticsearch-setup.sh` 裝這個分支的 storage/）；cluster 是 Rancher Desktop 的 es-test（ES 9.5.5）＋master 版 Collector。

### 格式／值 → 預期 → 實際（`run-trace-id-cases.sh`）

| 格式／值 | 預期 | 改前 | 改後 |
|---|---|---|---|
| Java logstash＋MDC、.NET Scopes、Serilog、logfmt、ECS 扁平／巢狀 | 原值 | 對 | 對 |
| JSON 裡多行 stack trace | 原值 | 對 | 對 |
| 純文字、純文字 stack 續行 | 沒有 | 沒有 | 沒有 |
| OTLP 直送（body 純文字） | record 的值 | 對 | 對 |
| 大寫 hex | 轉小寫 | 原樣大寫 | 小寫 |
| 前後空白 | 去掉 | 帶空白 | 去掉 |
| 全 0（file、OTLP） | 沒有 | 全 0 | 沒有 |
| 16 個 hex | 補 16 個 0 | 16 個 | `0000000000000000a3ce…` |
| 31 個、含 `zg`、GUID 帶減號 | 不設，記 invalid | 原樣寫入 | 不設，`app.trace_id_invalid` 有原值 |
| MDC 空字串 | 沒有 | `""` | 沒有 |
| `traceId`、最外層 `TraceId` | 原值 | 對 | 對 |
| `dd.trace_id`（十進位） | 不拿，留 `app` | 不拿 | 不拿 |
| 只有 trace | trace 有、span 沒有 | 對 | 對 |
| span 15 個、span 填成 32 個 | span 不設，記 invalid | 原樣寫入 | 不設，記 invalid |
| log 早／晚 10 分鐘 | trace 照設，`@timestamp` 用應用的 | 對 | 對 |
| 訊息內文有 hex、欄位叫 `request_id` | 沒有 | 沒有 | 沒有 |
| OTLP：body 錯、record 對 | record 的 | `not-a-trace` | record 的 |
| OTLP：body 對、record 另一個 | body 的 | 對 | 對 |
| 同一行 file＋OTLP | 兩份、`_id` 不同、`log.source` 分得出 | 兩份，分不出 | `file`、`otlp` |

- 改前：33 個裡 trace 相關的錯 11 個（全部 34 個都 FAIL，因為改前沒有 `log.source`）。
- 改後：34 個全 PASS。
- 舊案例（同一個 ES）：`run-trace-cases.sh` 7 個、`run-level-cases.sh` 24 個、`run-timestamp-cases.sh` 12 個都過。
- `run-alert-flap.py` 要 Grafana，跟 log pipeline 無關，沒跑。

### 時間差（`run-log-trace-skew.py`）

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| log 離 span 10 分鐘，investigator 撈得到嗎 | 同 trace_id 三筆：同時、早 10 分、晚 10 分；事故窗 01:00–01:05 ± 5 分 | 改前 1 筆（只有同時的）；改後 3 筆 |
| engine 沒被改壞 | `pytest engine/tests` | 318 passed |

### 經過真的 Collector（`run-trace-id-e2e.sh`）

未驗證。腳本寫好了：大寫、超過 16KB 的一行、格式錯、背景工作、檔案＋OTLP 兩份、alias 交叉查。共用的 es-test 不裝這個分支的 pipeline，要跑見 Act。

### 沒測到的

- 真的 Java agent／.NET 自動埋點開 otlp 的兩份：用 curl 送 OTLP log 模擬（agent 那條上一輪驗過）。
- Sleuth 風格純文字 `[trace,span]`：沒有規則，綁不起來（上一輪已列）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 開 otlp 的服務 log 量兩倍 | 兩份案例 | 存量兩倍；統計（`aiops-log-stats`）推論也算兩次 | 試點看量；要省就讓 `file_log` 不收設了 otlp 的 Pod |
| `log.source` 只有新資料有 | mapping 新欄位 | 舊資料查 `log.source` 是空的 | 舊資料保留 30 天，自然換完 |
| 16 個 hex 補 0 是推論 | B3 規格與 OTel B3 propagator 的做法 | 用 Zipkin／Brave 自己送 trace 的服務，trace 不在 ES，本來就對不到 | 有這種服務再看 |
| [①] cluster 那段（Collector 接回 16KB 長行、file＋OTLP 兩份）未驗證 | 共用 es-test 不能裝未合的 pipeline | `_simulate` 驗了 pipeline，Collector 那段沒驗 | 合進 phase1 分支後跑（見下一輪） |
| investigator 撈 log 多放 30 分鐘 | 時間差案例 | 更大的偏差（例如時區錯 8 小時）還是撈不到 | 時區錯由 `quality.log_clock_skew` 報（[time-alignment](2026-10-time-alignment.md)） |

- **決定**：格式錯的 trace_id 不硬塞（寧可綁不到，不要綁錯）；原值留著。
- **下一輪**：cluster 那段要在合進 phase1 分支後，跟 `run-e2e.sh` 一起跑（先拿 cluster 鎖）：

```bash
source e2e/local-k3s/cluster-lock.sh; cluster_lock "log-trace-bind"
e2e/local-k3s/run-e2e.sh
kubectl -n es-test port-forward --address 127.0.0.1 svc/elasticsearch <port>:9200 &
ES_URL=http://127.0.0.1:<port> e2e/local-k3s/run-trace-id-e2e.sh
cluster_unlock
```

- 預期每項 PASS：大寫轉小寫、16KB 一行接回一筆、格式錯不設、背景不設、`log.source` 是 file、otlp。
- alias 交叉查：log 用 `trace_id` 查到 4 筆。
