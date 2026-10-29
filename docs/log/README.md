# Log 進 Elasticsearch

> **類型：原理 Explanation**　同主題：[規格（欄位、pipeline）](reference.md)・[操作（部署、驗證）](how-to.md)・[Fluent Bit vs OTel（原理）](fluentbit-vs-otel.md)

一句話：**每個節點的 OpenTelemetry Collector 只負責「收」和「分流」，欄位整理一律交給中央 ES 的 pipeline。** 規則改在中央，不用動每個 cluster。

log 和 metrics 用同一個 Collector（[metrics/README.md](../metrics/README.md)），分部門的規則、key 都共用。原本用 Fluent Bit，改用 OTel 的原因與比較見 [log/fluentbit-vs-otel.md](fluentbit-vs-otel.md)。

> [!IMPORTANT]
> **重點：log 格式不一致的處理，見下方「[pipeline](reference.md#pipeline)」。**
> 依服務宣告的格式分派解析規則的機制已經完成並測過；**REST API 的規則還沒寫，等真實 log 樣本**（SECS 屬工廠，不在目前範圍）。

## 現況與待辦（①收值、②清資料、③存）

**已完成 ✅**

| 項目 | 狀態 |
|---|---|
| Collector 讀節點上的 `/var/log/pods`，依 namespace 的部門分流（寫進各部門的 data stream）、集中式資料寫入憑證 (Shared Ingest Token) 所有部門共用、換 key 不掉資料 | ✅ |
| 邊緣身份鑑別與標籤注入 (Edge Identity Sanitization & Injection)：拿掉應用自己填的 K8s、部門欄位，由 Collector 依 Pod 補上 | ✅ |
| 中央 pipeline 依格式解析：logback、ECS、.NET JSON、Serilog、logfmt、純文字（保底抓 level） | ✅ |
| 統一欄位：`service.name`、`log.level`、`trace.id`、時間（UTC）；跟 metrics、trace 同一條路徑的 alias（`resource.attributes.service.name`、`trace_id` 等） | ✅ |
| 去重複（內容指紋當 `_id`）、斷線先寫磁碟再補送 | ✅ |

**已完成的部分，還差一點**

| 項目 | 要做的 |
|---|---|
| ② 欄位路徑跟 metrics、trace 不一致 | ✅ mapping 加 alias：`resource.attributes.service.name`、`resource.attributes.k8s.{namespace,pod,container,node,cluster}.name`、`trace_id`、`span_id` 指到原本的 ECS 欄位。baseline、quality、RCA 一律用 OTel 路徑查 log；原欄位留著，Kibana 現有查詢不受影響。alias 不在 `_source` 裡，程式要用 `fields` 取值。[PDCA](../pdca/phase1-collect/2026-10-field-unify.md) |
| ② REST API 的解析規則 | 需要兩三個服務的真實 log 樣本 |
| ① ES 剛起來回 401、Collector 直接丟 | 修在 ES 入口：健康檢查要用 API key 打 GET /（trace 線實測；log 用同一種 exporter，推論相同）。📝 中央 ES 前面的健康檢查要請管 ES 的人改。[PDCA](../pdca/phase1-collect/2026-10-es-startup-401.md) |
| ③ 保留天數 | ✅ 原文 30 天；另外每 5 分鐘統計各服務 × level × `error.type` 的筆數，留 400 天（[retention.md](../retention.md)） |

**要做的（②清資料）**

| 項目 | 內容 | 驗收 |
|---|---|---|
| ② 收資料延遲（時間對齊） | ✅ `event.created`（Collector 讀到）→ `event.ingested`（寫進 ES），p95 > 1 分鐘報（`quality.log_ingest_lag`）；應用的時間跟節點差 > 5 分鐘報（`quality.log_clock_skew`）。[PDCA](../pdca/phase1-collect/2026-10-time-alignment.md) | 實測：卡 3 分鐘、20 分鐘的都報；時區錯、時鐘快 10 分鐘的都報 |
| ② 時區規範（時間對齊） | ✅ 中央 pipeline：沒帶時區的依 `LOG_DEFAULT_TIMEZONE`（預設 Asia/Taipei）解讀並記 `event.timezone`；多認得空白分隔、逗號毫秒、epoch 秒／毫秒；不認得的記 `log.timestamp_source: collector_unparsed`。📝 規範（寫 UTC 或帶時區）還沒寫進應用標準 | `e2e/pipeline/run-timestamp-cases.sh` |
| ② 從 log 抽業務 ID | 中央 pipeline 依格式把 `txnId=…`、`orderNo: …` 抽成標準欄位，舊系統不用改（業務 ID 的完整規劃見 [trace/README.md「業務 ID」](../trace/README.md#業務-id服務層)） | 舊服務的 log 查得到交易 ID |

## 資料怎麼走

```
container stdout（每個節點的 /var/log/pods）
        ↓ 節點的 OpenTelemetry Collector（DaemonSet，跟 metrics 同一個）：補上 pod label、namespace 的 dept、cluster
        ↓ dept → group（跟 metrics 同一份規則）
        ↓ 集中式資料寫入憑證（ES_WRITER_API_KEY）、一個出口；data_stream.namespace = group
        ↓ OTLP 送到 ES 的 /_otlp/v1/logs → logs-generic.otel-{group}
        ↓ 中央 ES：k8s-logs-otel 整理形狀、用內容算 _id（擋重送的重複）、轉到 k8s-{group}-{cluster}
        ↓ 中央 ES：k8s-logs pipeline 整理欄位（依 log 格式分派）→ data stream（ILM 管保留天數）
        ↓
   AIOps 用唯讀 key 讀 k8s-*；人用各 group 的唯讀角色
```

跟 metrics 共用同一組 key 與部門清單：收一次、每個 group 一條出口、一把 key、一個獨立佇列，每筆只會符合一條，不會送兩份。

**中央 pipeline 也接受 Fluent Bit 送的資料**（沒有 `agent.type` 的就當成 Fluent Bit），以後邊緣 VM 之類的地方用 Fluent Bit 也能直接接。每筆的 `agent.type`（`otelcol`／`fluent-bit`）記錄是哪個收集器送的。

## 還沒做

1. **K8s events**（OOMKilled、probe 失敗、BackOff）。這是 AIOps 很重要的訊號，但 event 本身只帶 namespace 名稱，不帶 dept；要分流得先決定歸屬放哪裡，見 README「還沒定案的」第 1 點。唯讀角色裡已經預留了 `k8s-events-{group}-*`。
2. **保留天數**：已套用 30 天（[retention.md](../retention.md)），各 group 要不要不同還要確認。
3. **⚠️ 重點：SECS、REST API 等格式的解析規則**：架構已經好了（見「[pipeline](reference.md#pipeline)」），等拿到真實的 log 樣本再寫。SECS 還要在 Collector 的 `file_log` 加多行合併。
4. **dept 清單目前要手動同步兩處**：`groups.yaml`、Collector 的 `common.yaml`（log 與 metrics 共用）。group 變多之前要改成從 `groups.yaml` 產生。
