# aiops-data-foundation

替 AIOps 打資料地基，並在上面做出第一版 AIOps：**讓每筆資料一出生就帶身分證、送進對的地方；再學「平常的樣子」、抓異常、從 metrics 串到 trace 和 log 找根因。**

**目前範圍：一般服務**（Web API、worker、Kafka、DB）。工廠（EAP、機台、MES／MCS／AMHS、批號載具）之後再做，相關內容都集中在 [docs/factory-later.md](docs/factory-later.md)。

## 從哪裡開始讀

1. [docs/overview.md](docs/overview.md)：架構圖，log、metrics、trace 怎麼收（10 分鐘）。
2. [docs/system-map.md](docs/system-map.md)：整條線一份看完：收值 → 清資料 → 時間顆粒 → 四種判斷 → 串接 → 收斂 → LLM → 根因 → feedback，每一節標已做／還沒做。
3. [docs/stage2-plan.md](docs/stage2-plan.md)：接下來要做什麼：階段一現況、階段二五項規劃、三波順序。

其他依主題查下面的索引。

## 文件索引

**收資料（地基）**

| 文件 | 內容 |
|---|---|
| [docs/elasticsearch.md](docs/elasticsearch.md) | log 怎麼進 ES：依 namespace 的部門分流、中央 pipeline 依格式解析、欄位規範、換 key |
| [docs/metrics.md](docs/metrics.md) | metrics 怎麼進 ES：kubelet、K8s 狀態、應用的指標，直方圖換增量 |
| [docs/traces.md](docs/traces.md) | trace 怎麼進 ES：兩層 Collector、尾端取樣、擋冒充、跟 log 串 |
| [docs/dotnet.md](docs/dotnet.md) | 應用是 .NET 時：不改程式的自動埋點，.NET 8／9 指標名稱、Kafka |
| [apps/standards/tracing.md](apps/standards/tracing.md) | 給應用團隊：trace 埋點指引、命名規則、業務欄位 |
| [docs/retention.md](docs/retention.md) | 保留天數與容量（方案，還沒套用） |

**判斷與根因（AIOps）**

| 文件 | 內容 |
|---|---|
| [aiops/baseline/README.md](aiops/baseline/README.md) | baseline：中位數＋MAD、預設 30 秒一點並依量自己往粗調、跳一次 warning、連續才 anomaly、卜瓦松、借同組、使用者調整；規則；跟兄弟比；trace 每一段 |
| [aiops/rca/README.md](aiops/rca/README.md) | 根因分析：Metrics → Trace → Log 自動查，產出報告（[範例](aiops/rca/example-report.md)） |
| [docs/aiops-rca.md](docs/aiops-rca.md) | 根因分析的觀念：三種資料各回答什麼、六步、metrics 怎麼串到 trace 和 log、要哪種「平常」 |
| [docs/aiops-metrics.md](docs/aiops-metrics.md) | metrics 收了哪些、哪些有 baseline、能做到什麼 |
| [docs/aiops-metrics-cases.md](docs/aiops-metrics-cases.md) | 基礎架構、應用、DB 與佇列的故障案例，在 k3s 實測（機台案例之後再做） |
| [docs/aiops-trace.md](docs/aiops-trace.md) | trace 能做什麼：慢在哪、程式壞味道、錯誤源頭、非同步、架構治理 |
| [docs/aiops-cases.md](docs/aiops-cases.md) | 15 個真實故障案例要哪種「平常」才抓得到 |

**決策與規劃**

| 文件 | 內容 |
|---|---|
| [docs/stage2-plan.md](docs/stage2-plan.md) | 落地五階段：階段一現況、階段二（時間對齊、Kafka 串接、業務 ID、髒資料排除、雜訊壓制） |
| [docs/aiops-llm-mlops.md](docs/aiops-llm-mlops.md) | 後期接 LLM 還是 MLOps：定案 LLMOps 為主軸、不自己訓練模型、feedback 閉環 |
| [docs/poc-status.md](docs/poc-status.md) | PoC 現況與下一步（簡報文字） |
| [docs/factory-later.md](docs/factory-later.md) | 工廠部分：已備好的、還沒做的、什麼時候開始 |

**測試紀錄與比較**

| 文件 | 內容 |
|---|---|
| [docs/elasticsearch-testing.md](docs/elasticsearch-testing.md) | log 路線測試：53 項檢查、level 案例、key 失效、大流量、ES 停機、去重複 |
| [docs/otel-logs-compare.md](docs/otel-logs-compare.md) | log 從 Fluent Bit 改用 OpenTelemetry Collector 的比較與決定 |
| [test/local-k3s/tail-sampling/README.md](test/local-k3s/tail-sampling/README.md) | 尾端取樣壓測 |

## 已經定案的

- **log、metrics、trace 都進 Elasticsearch（9.x）**，不用 VictoriaMetrics。
- **收集器都用 OpenTelemetry Collector**：每個節點一個，分部門的規則和 key 共用。log 原本用 Fluent Bit，比較後改用 OTel（[docs/otel-logs-compare.md](docs/otel-logs-compare.md)）；中央 pipeline 仍接受 Fluent Bit 的資料，邊緣 VM 可以用。
- **每個 group 一把寫入 key**，log、metrics、trace 共用；某把失效只影響那個 group，寫錯 group 會被 ES 擋下。**換 key 不掉資料、不重啟**（`platform/central/rotate-writer-key.sh`）。
- **舊專案不改程式**：Java 用 agent，.NET 用自動埋點＋OTLP 推（[docs/dotnet.md](docs/dotnet.md)）。
- **trace 用尾端取樣**：錯的、慢的、有例外的全留，正常的留 5%。
- **應用團隊不需要知道 group、key**：規格由共用 chart 產生（[apps/](apps)）。
- **namespace 名稱不含組織資訊**（`{cluster}-{app}-{env}` 或 `{app}-{env}`），歸屬另外標。
- **AIOps 以統計＋LLM 為主，不自己訓練模型**（[docs/aiops-llm-mlops.md](docs/aiops-llm-mlops.md)）。
- **業務欄位名稱**：`lot.id`、`equipment.id`、`work_order.id`、`recipe.id`（[apps/standards/tracing.md](apps/standards/tracing.md)）。工廠用，之後再接。

## 還沒定案的

1. **歸屬放哪裡**：現在讀 namespace 的 `dept` label，部門 → group 對照在 `platform/groups.yaml`。要改成「app → group」的中央清單的話，log、metrics、trace 要一起改。
2. **app 名稱是否全公司唯一**（用中央清單的話，這是前提）。
3. **寫入 key 怎麼自動進新的 cluster**：現在靠 Rancher 手動建 Secret；cluster 一多就不可行，要確認有沒有 External Secrets、Vault 或 sealed-secrets。
4. **K8s events 怎麼歸屬**：還沒收 events，照讀 namespace label 的做法應該能分，要實測。
5. **保留天數**：metrics、trace 現在**只有 rollover、不會刪**，硬碟遲早滿。方案見 [docs/retention.md](docs/retention.md)，要決定天數、副本，並用正式環境的量重算容量。
6. **Java 要不要預設加 `-XX:+ExitOnOutOfMemoryError`**：不加的話 OOM 後 Pod 活著但不能用。

## 待辦

範圍是一般服務。完整規劃在 [docs/stage2-plan.md](docs/stage2-plan.md)，這裡列重點。

**上線**

| 項目 | 內容 |
|---|---|
| 正式環境排程 | train 每天、detect 每 30 秒、rules 每分鐘、peer 每分鐘（程式都好了，還沒部署） |
| 告警出口 | 現在只寫進 `aiops-anomalies`，要接通知 |
| 收斂 | 連續的點合成一段、同服務合成一件、沿呼叫關係找源頭、排優先與靜音（[system-map.md 第 7 節](docs/system-map.md)） |
| 事故紀錄、回饋 | 確認過的事故、值班回饋「真的／誤報」；之後給 LLM 和調門檻用（[system-map.md 第 10 節](docs/system-map.md)） |
| 用真實資料校正 | 正式環境跑 2～4 週後，看誤報率、調門檻 |

**階段一的小尾巴**：統一 Java、.NET 的指標名稱；統一 log 和 metrics、trace 的欄位路徑；trace-sampler 改磁碟佇列。

**階段二**：時間對齊、Kafka 串接、業務 ID（交易 ID）、髒資料排除、雜訊壓制（[docs/stage2-plan.md](docs/stage2-plan.md)）。

**baseline 補強**

| 項目 | 內容 |
|---|---|
| 補序列 | p99、慢請求比例（超過 N 秒的比例）、往外呼叫的耗時（`http.client.request.duration`） |
| 移動式／變化點偵測（輔助） | 標出「哪個時間點突然變了」：補新服務前幾天、換版後跳一階 |
| 跟兄弟比 → trace | 只有一個 Pod 慢時，拿它的 trace 跟其他 Pod 逐段比 |
| 趨勢 | 記憶體洩漏、磁碟幾天後滿：算斜率、預測 |
| `groups` | 依公司服務命名定義組，新 API 才借得到 baseline |

**.NET 還沒驗證的**：SQL Server（SqlClient）連線池指標、.NET 6、Serilog／NLog、Alpine（musl）image。

**log 解析**：REST API 的 log 解析規則，需要兩三個服務的真實樣本。

**之後**

- **LLM**：從根因報告摘要開始，只加註、不擋告警（[docs/aiops-llm-mlops.md](docs/aiops-llm-mlops.md)）。
- **Elastic 付費 AIOps 功能**：Basic 授權實測全部不能用，要 Platinum 以上。可以開 30 天試用評估，但試用只能開一次，等資料累積夠再開。
- **工廠**：[docs/factory-later.md](docs/factory-later.md)。

## 目錄

```
aiops/       AIOps（一個 Python 專案）：baseline（學平常的樣子、抓異常，metrics 與 trace 每一段）、rca（根因分析）
apps/        應用端：共用 chart、範例、Java log 設定、trace 埋點指引、CI 檢查
platform/    平台端：groups.yaml（部門 → group）、中央 ES 設定、各 cluster 的 OpenTelemetry Collector
docs/        設計文件、規劃、測試紀錄
test/        本機 k3s 端到端測試
```
