# Log 進 Elasticsearch：規格

> **類型：規格 Reference**　同主題：[原理](README.md)・[操作（部署、驗證）](how-to.md)・[Fluent Bit vs OTel（原理）](fluentbit-vs-otel.md)

欄位規範、pipeline 的規則、index 設定、檔案位置。要查「這個欄位是什麼、這種格式怎麼解析」看這份；為什麼這樣設計見 [README.md](README.md)，怎麼部署、怎麼驗見 [how-to.md](how-to.md)。

**目錄**

<!-- toc -->
- [檔案](#檔案)
- [欄位規範](#欄位規範)
- [pipeline](#pipeline)
  - [結構：共通身分 → 依格式分派 → 欄位對應](#結構共通身分--依格式分派--欄位對應)
  - [保底：沒有規則的格式也盡量抓 level](#保底沒有規則的格式也盡量抓-level)
  - [level 正規化](#level-正規化)
  - [trace_id 怎麼取](#trace_id-怎麼取)
- [Index](#index)
<!-- /toc -->

## 檔案

| 檔案 | 用途 |
|---|---|
| [telemetry/groups.yaml](../../telemetry/groups.yaml) | 部門 → group 對照表 |
| [telemetry/scripts/elasticsearch-setup.sh](../../telemetry/scripts/elasticsearch-setup.sh) | 中央 ES：index 結構＋各種 API key 與角色 |
| [telemetry/storage/](../../telemetry/storage) | ILM、mapping、pipeline、index template |
| [telemetry/storage/formats/](../../telemetry/storage/formats) | 各 log 格式的解析規則，一種格式一個檔 |
| [telemetry/storage/pipeline-otel.*](../../telemetry/storage/pipeline-otel.painless)、[logs-otel-custom.json](../../telemetry/storage/logs-otel-custom.json) | `k8s-logs-otel`：OTel 送來的 log 整理形狀、算 `_id`、轉到 `k8s-{group}-{cluster}` |
| [telemetry/collectors/](../../telemetry/collectors) | 每個應用 cluster 的 OpenTelemetry Collector（log 與 metrics，kustomize）；log 的收法在 `node.yaml` |
| [e2e/local-k3s/](../../e2e/local-k3s) | 端到端測試 |
| [e2e/pipeline/](../../e2e/pipeline) | pipeline 案例測試（只需要 ES） |

## 欄位規範

AIOps 只認這些欄位。標準概念用 [ECS](https://www.elastic.co/guide/en/ecs/current/index.html) 名稱，跟 OpenTelemetry 的命名對得上。

| 欄位 | 來源 | AIOps 拿來做什麼 |
|---|---|---|
| `@timestamp` | 應用 log 裡的時間（一律存 UTC；沒帶時區的依 `LOG_DEFAULT_TIMEZONE` 解讀）；沒有、或格式不認得，就用 container runtime 寫下這行的時間 | 時間軸 |
| `message` | 應用的訊息；不是 JSON 就是整行原文 | log 樣板分群 |
| `log.level` | 應用的 level，統一成 trace/debug/info/warn/error/fatal。應用沒寫的話，從原文抓（見「保底」） | 錯誤率 |
| `log.level_inferred` | `true` 表示 `log.level` 是從原文猜的，不是應用自己寫的 | 分辨可信度 |
| `log.logger`、`process.thread.name` | 應用 | 縮小範圍 |
| `error.type` | 從 stack trace 第一行拆出的例外類別 | 例外分群，比對全文便宜很多 |
| `error.stack_trace` | 應用 | 人看 |
| `trace.id`、`span.id` | OpenTelemetry agent 寫進 MDC／scope，或 OTLP 直送的 log record；規則見「[trace_id 怎麼取](#trace_id-怎麼取)」 | 串起同一個請求的 log，也對得到 trace 的 `trace_id`（[trace/README.md](../trace/README.md)）。alias `trace_id`、`span_id` 跟 trace 同名 |
| `app.trace_id_invalid`、`app.span_id_invalid` | 應用寫了、但格式不對的原值 | 找 trace_id 印錯的服務 |
| `log.source` | 從哪收的：`file`（容器 log 檔）或 `otlp`（應用 agent 直送） | 開了 `otel.logs.exporter: otlp` 的服務同一行有兩份，用它分 |
| `resource.attributes.service.name`、`resource.attributes.k8s.*.name`、`trace_id`、`span_id` | alias（`component-mappings.json`），指到上面的 ECS 欄位 | 跟 metrics、trace 同一條路徑查；不佔空間、不在 `_source`（用 `fields` 取） |
| `service.name` | pod label `app.kubernetes.io/name`；沒有就用 Collector 給的（Deployment 名稱，跟 metrics、trace 一樣）；Fluent Bit 送的用 container 名稱 | 分析單位 |
| `service.version` | pod label `app.kubernetes.io/version` | 「換版之後才開始錯」 |
| `service.namespace` | pod label `app.kubernetes.io/part-of` | 所屬系統 |
| `labels.group`、`labels.dept` | 分流結果、namespace label | 歸屬 |
| `orchestrator.cluster.name` | Collector 的 `CLUSTER_NAME` | 哪一廠 |
| `agent.type` | 哪個收集器送的：`otelcol` 或 `fluent-bit` | 搬遷時比對、查收集器的問題 |
| `kubernetes.namespace`、`.pod.name`、`.node.name`、`.container.name` | K8s | 對回 metrics |
| `kubernetes.labels` | 全部 pod label（flattened） | 備用 |
| `app` | 應用自帶的其他欄位，例如 MDC 的 `lot_id`（flattened） | 業務維度 |
| `log.format` | 服務宣告的 log 格式（pod label `log-format`），沒宣告是 `default` | 追蹤各格式的覆蓋率 |
| `log.parser` | 實際用來解析的規則。和 `log.format` 不同，代表那個格式還沒有規則，退回 default | 找出還沒寫規則的格式 |
| `event.created` | container runtime 寫下這行的時間（節點的時鐘），Collector 讀到的就是這個 | 收資料延遲、應用時鐘偏差 |
| `event.ingested` | ES 收到的時間 | 收資料延遲 |
| `event.timezone` | 應用的時間沒帶時區時，用哪個時區解讀 | 找出沒帶時區的服務 |
| `log.timestamp_source` | `@timestamp` 從哪來：`app`（應用的，帶時區）、`app_assumed_tz`（應用的，沒帶時區）、`collector`（應用沒寫）、`collector_unparsed`（應用寫了但格式不認得） | 找出時間不可信的服務 |

和 metrics 的 label 對應：`service.name` ↔ `app.kubernetes.io/name`、`kubernetes.namespace` ↔ `namespace`、`labels.group` ↔ `group`、`labels.dept` ↔ `dept`。

目前認得的 log 格式見下一節「pipeline」。

**為什麼自帶欄位收進 `app` 而不是攤平在最上層：** 幾百個服務共用一個 index，每個服務多幾個 MDC 欄位，欄位數很快就撞到上限，整個 index 拒收。`flattened` 型別只佔一個欄位，仍然可以用 `app.lot_id: L123` 查詢。

## pipeline

pipeline 是 **Elasticsearch 內建的「寫入前加工」**（ingest pipeline）。收集器送來的資料，在存進 index 之前，先跑過一串處理步驟整理好再存。

例如一筆 logback JSON：

```
送進來：  log = "{\"level\":\"ERROR\",\"message\":\"order 5 failed\",\"stack_trace\":\"java.lang.IllegalStateException: ...\"}"
          kubernetes.labels = {app.kubernetes.io/name: mes-api, app.kubernetes.io/version: 2.3.1}
存進去：  message = "order 5 failed"、log.level = "error"、error.type = "java.lang.IllegalStateException"
          service.name = "mes-api"、service.version = "2.3.1"、labels.group = "it"…
```

**為什麼放在中央 ES，不放在收集器：** 收集器每個 cluster 都有一套，規則放那裡，每改一次就要每廠重新部署。放在中央只要改一處，跑一次 `elasticsearch-setup.sh` 就全廠生效。

> [!IMPORTANT]
> **這一節是 log 端的重點。** 格式拆不出欄位，AIOps 就只能看到一串文字。
>
> | 狀態 | 項目 |
> |---|---|
> | ✅ 完成 | 依格式分派的機制、`default`（JSON／純文字）、`logfmt`、沒有規則時退回並保留原文、`log.format`／`log.parser` 追蹤、純文字保底抓 level |
> | ⏳ 待辦 | **SECS**：解析規則＋收集器多行合併。需要真實樣本：一般事件（S6F11）、告警（S5F1）、逾時（S9F9）、多行 body，以及 EAP 用的軟體或函式庫 |
> | ⏳ 待辦 | **REST API**：只統一 method、路徑（換成樣板）、狀態碼、耗時、trace_id，body 不統一。需要兩三個服務的 log 範例，以及有沒有 ingress／API gateway 的存取紀錄 |

### 結構：共通身分 → 依格式分派 → 欄位對應

log 格式不可能全廠統一，例如 Java 的 JSON、Go 的 key=value、設備通訊的 SECS、各家 REST API 自己的格式。所以分三層：

```
k8s-logs（主 pipeline）
  1. 共通身分：服務、版本、group、dept、cluster、K8s 資訊        ← 跟 log 內容長怎樣無關，每筆一定有
     原文先放進 message
  2. 看 pod label log-format，交給 k8s-logs-format-<格式>
       ├─ default（沒宣告）：JSON 就拆，不是 JSON 就保留原文
       ├─ logfmt：level=info msg="..." key=value
       └─ 宣告了但中央還沒有這個格式的規則 → 退回 default，原文完整保留
     各格式只負責「把原文拆成欄位」，拆出來的交給 ↓
     k8s-logs-fields：對應成標準欄位（level/lvl → log.level、msg → message…），所有格式共用
                      還是沒有 level 的，從原文行首附近抓全大寫的 ERROR/WARN/INFO…（保底）
  3. 時間改用應用自己的時間；蓋上 ES 收到的時間
  任何一步出錯：原文照樣入庫，錯誤寫在 error.message，不丟資料
```

### 保底：沒有規則的格式也盡量抓 level

不管什麼格式，只要最後還沒有 `log.level`，就從原文抓。兩條規則，依序：

| 規則 | 條件 | 例子 |
|---|---|---|
| A. 行首前綴 | 一行**最開頭**的字（可包在 `[` `]` 裡），後面緊接 `:` 或 `]`；不分大小寫 | `[WARN] …`、`Warning: …`、`error: …`、`[CRITICAL] …` |
| B. 前 80 字內的全大寫關鍵字 | 前 80 個字元內，**獨立、全大寫**的關鍵字 | `2026-10-08 12:00:00 ERROR [main] …`、`ERROR - …`、`… SEVERE [main] …` |

- 找到的話設 `log.level`，並標 `log.level_inferred: true`。應用自己寫了 level 的不會被覆蓋。
- **刻意不算的：**
  - 小寫散在句中：`0 errors`、`no error found`。
  - 首字母大寫但不在行首：`Errors: 0` 的 `Errors` 不是 level 關鍵字。
  - 連在別的字上：`ERROR_CODE=5`。
  - 在第 80 個字元之後。
- **仍可能誤判**，例如訊息開頭剛好是 `NO ERROR`。所以 AIOps 對 `log.level_inferred: true` 的資料要降低權重。

### level 正規化

不管是應用自己寫的，還是猜出來的，一律統一成六種：

| 原本的寫法（不分大小寫） | 統一成 |
|---|---|
| `trace`、`verbose` | trace |
| `debug` | debug |
| `info`、`information`、`notice` | info |
| `warn`、`warning` | warn |
| `error`、`err`、`severe`（Java 內建 logging、Tomcat） | error |
| `fatal`、`critical`、`crit`、`panic`、`emerg`、`emergency`、`alert` | fatal |

不認得的值，例如 `fine`，轉小寫後原樣保留，不硬塞進六種。

案例測試見 [e2e/pipeline/level-cases.json](../../e2e/pipeline/level-cases.json)，不用 K8s，幾秒跑完：

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> e2e/pipeline/run-level-cases.sh
```

**目前的格式：**

| `log-format` | 給誰用 | 例子 |
|---|---|---|
| （不填） | Java 標準（logback JSON）、ECS JSON（Spring Boot 3.4 `logging.structured.format.console=ecs`）、**.NET 內建 JSON console**（`Message`、`LogLevel`、`Category`、`Exception`、`Scopes` 裡的 `TraceId`）、**Serilog compact JSON**（`@m`、`@l`、`@x`、`@tr`）、純文字 | `{"level":"INFO","message":"..."}`、`{"LogLevel":"Information","Message":"...","Scopes":[{"TraceId":"..."}]}` |
| `logfmt` | Go 等語言常見的 key=value | `level=warn msg="lot hold requested" lot_id=L123` |

### trace_id 怎麼取

| 項目 | 規則 |
|---|---|
| 認得的欄位 | `trace_id`、`traceId`、`trace.id`（扁平或巢狀）、`TraceId`、`@tr`、.NET `Scopes` 裡的 `TraceId` |
| span 的欄位 | `span_id`、`spanId`、`span.id`、`SpanId`、`@sp`、`Scopes` 裡的 `SpanId` |
| 不認的欄位 | `dd.trace_id`（Datadog，十進位，對不到 OTel 的 trace）：留在 `app` |
| 大寫、前後空白 | 轉小寫、去空白（trace 的 `trace_id` 是小寫，keyword 分大小寫） |
| 16 個 hex 的 trace_id | 前面補 16 個 0（B3 短 id；OTel 的 B3 propagator 也這樣補） |
| 空字串、全 0 | 當作沒有 trace，不設、不算錯 |
| 長度不對、非 hex | 不設 `trace.id`／`span.id`，原值放 `app.trace_id_invalid`／`app.span_id_invalid` |
| 只有 trace、或 span 格式錯 | trace 照設，span 各自判斷 |
| 原文和 OTLP record 都有 | 原文的合法就用原文；原文的不合法才用 record 的 |
| 純文字、訊息裡的 hex | 不拿：只從欄位取，不從訊息內文猜 |

案例：[e2e/pipeline/run-trace-id-cases.sh](../../e2e/pipeline/run-trace-id-cases.sh)（`_simulate`）、[e2e/local-k3s/run-trace-id-e2e.sh](../../e2e/local-k3s/run-trace-id-e2e.sh)（經過 Collector）。[PDCA](../pdca/phase1-collect/2026-10-log-trace-bind.md)

## Index

- 命名：`k8s-{group}-{cluster}`。每個 group、每個 cluster 一條 data stream。
  - 權限照 group 切，所以名稱要帶 group；這就是現有寫入 key 的 `k8s-it-*` 樣式。
  - 名稱帶 cluster，某一廠出狀況要整批清掉或調保留天數時，不會動到別廠。
- `CLUSTER_NAME` 要小寫，ES 的 index 名稱不接受大寫。
- shard：每條 data stream 1 個 primary，每天或 30GB 滾一次。
- 保留天數：30 天，天數在 [retention.conf](../../telemetry/scripts/retention.conf)，見 [retention.md](../retention.md)。
- 這套用到的功能都在免費授權（Basic）內：API key、ILM、data stream、flattened。
