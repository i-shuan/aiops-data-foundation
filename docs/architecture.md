# 架構

一句話：**每個應用 cluster 一套 OpenTelemetry Collector，把 log、metrics、trace 依 namespace 的部門分流、用各部門自己的 key 寫進中央 Elasticsearch 9；AIOps 用唯讀 key 讀出來，學「平常的樣子」、跑規則、Pod 互比、做根因分析，結果寫進 `aiops-anomalies`。** 舊專案不改程式（Java agent、.NET 自動埋點）。

範圍：一般服務（Web API、worker、Kafka、DB）；工廠之後再做（[factory-later.md](factory-later.md)）。整條線每一步做什麼、程式在哪、怎麼驗：[end-to-end-flow.md](end-to-end-flow.md)。每個階段的細節、判斷方法、收斂、LLM、feedback 見 [system-stages.md](system-stages.md)；現況與待辦見 [pdca/](pdca/README.md)。

## 一張圖看全部

```mermaid
flowchart TB
  subgraph S1["① 收值（每個應用 cluster）"]
    APP["應用 Pod<br/>Java agent／.NET 自動埋點<br/>不改程式"]
    K8S["kubelet、cAdvisor、K8s 狀態<br/>exporter（Kafka）"]
    COL["node Collector（每節點）<br/>cluster Collector（一台）"]
    TS["trace-sampler ×2<br/>尾端取樣"]
    APP -- "log（stdout）、metrics、trace" --> COL
    K8S -- "30 秒抓一次" --> COL
    COL -- "trace 依 trace_id 分配" --> TS
  end

  subgraph S2["② 清資料"]
    C1["Collector：擋冒充、補 K8s 身分、<br/>部門→group、直方圖換增量、<br/>例外事件抄到 span"]
    C2["中央 ES pipeline：log 依格式解析、<br/>統一欄位、去重複"]
  end

  subgraph S3["③ 存（中央 Elasticsearch）"]
    LOG[("log<br/>k8s-*")]
    MET[("metrics<br/>metrics-*.otel-*")]
    TRA[("trace<br/>traces-*.otel-*")]
  end

  subgraph S4["④ 判斷（baseline 家族）"]
    R["一次就算：規則"]
    H["跟自己的歷史比<br/>MAD／卜瓦松／借同組"]
    P["跟兄弟比<br/>同服務的 Pod"]
    T["trace 每一段<br/>＋N+1、吞例外"]
  end

  AN[("⑤ aiops-anomalies<br/>所有異常一個地方")]

  subgraph S6["⑥ 收斂（同服務合成一件 ✅；沿呼叫關係 📝）"]
    CV["同一條連續 → 一段<br/>同服務同時間 → 一件<br/>呼叫關係 → 找上游源頭"]
  end

  subgraph S7["⑦ 根因分析"]
    RCA["metrics 異常 → trace 哪一段 → log 哪個新錯誤"]
    LLM["相似事故 ✅＋LLM 摘要（template ✅、真的模型 ⚠️）<br/>只看收斂後的一件事"]
  end

  OUT["⑧ aiops-alerts → Grafana 依 dept 送 ✅<br/>回饋網頁 ✅"]
  FB["⑨ feedback ✅（模擬資料）<br/>對：進知識庫、3 次提規則草稿<br/>錯：3 次提放寬 → 回測 → 核准"]

  S1 --> S2 --> S3
  LOG & MET & TRA --> S4
  R & H & P & T --> AN
  AN --> CV --> RCA --> LLM --> OUT --> FB
  FB -. "門檻、排除時段、新規則" .-> S4
  FB -. "確認過的事故" .-> LLM
```

**編號只有這一套（①～⑨）**：整個專案的文件、PDCA 紀錄、待辦都用它，不另外編「階段」。

| # | 模組 | 做什麼 | 在哪裡 | 狀態 |
|---|---|---|---|---|
| ① | 收值 | log、metrics、trace 三種資料，舊專案不改程式 | `telemetry/collectors/` | ✅ |
| ② | 清資料 | Collector 補身分、擋冒充；中央 pipeline 統一格式、去重複 | 同上、`telemetry/storage/` | ✅ |
| ③ | 存 | 中央 ES，資料依部門 group 分 data stream、一把寫入 key；被拒的進 failure store；保留天數、降解析度 | `telemetry/scripts/` | ✅ |
| ④ | 判斷 | 規則、跟歷史比、跟兄弟比、trace 每一段 | `engine/detector/`、`engine/configs/baseline/*.yaml` | ✅（正式環境還沒排程） |
| ⑤ | 異常總表 | 所有判斷結果寫同一個 index | `aiops-anomalies` | ✅ |
| ⑥ | 收斂 | 同一個服務、30 分鐘內的異常合成一件 | `engine/investigator/alerts.py` | ✅（沿呼叫關係合併 📝） |
| ⑦ | 根因分析 | metrics → trace → log；相似事故檢索；LLM 摘要 | `engine/investigator/`、`engine/core/llm.py` | ✅（真的 LLM ⚠️ 沒跑過） |
| ⑧ | 出口 | `aiops-alerts` 一件一筆、Grafana 依 dept 發通知、回饋網頁 | `engine/configs/grafana/`、`engine/feedback_web/` | ✅（K8s 排程 ⚠️ 沒跑過） |
| ⑨ | feedback | 回饋 → 建議 → 回測 → 核准；確認的事故進知識庫；量化 | `engine/detector/`（feedback、backtest、metrics） | ✅（模擬資料；真實數字 ⚠️） |

## 收值側細節：一個 cluster 裡資料怎麼走

```mermaid
flowchart TB
  subgraph APP["應用 cluster"]
    direction TB
    subgraph PODS["應用 Pod（不改程式：Java agent／.NET 自動埋點）"]
      LOGS["stdout log"]
      JM["Java metrics<br/>OTel agent 以 Prometheus 格式開 /metrics"]
      NM[".NET metrics<br/>OTLP 推"]
      TR["trace<br/>OTLP 推（全部送）"]
      EXP["exporter<br/>kafka_exporter"]
    end

    subgraph NODE["node Collector（每個節點一台）"]
      direction TB
      N1["1. 擋冒充：拿掉應用自己填的 K8s、部門欄位"]
      N2["2. 依是哪個 Pod 補上 namespace、Deployment、版本"]
      N3["3. namespace 的 dept → group（it／imc／unassigned）"]
      N1 --> N2 --> N3
    end

    KUBE["kubelet、cAdvisor<br/>CPU、記憶體、磁碟、限流"]
    CC["cluster Collector（一台）<br/>K8s 物件狀態：重啟、副本、節點"]
    TS["trace-sampler × 2（尾端取樣）<br/>等 30 秒整個 trace 到齊再決定<br/>錯誤／5xx／例外／超過 1 秒 全留<br/>正常的隨機留 5%"]

    LOGS -- 讀節點上的 log 檔 --> NODE
    JM -- 照 prometheus.io 標註來抓 --> NODE
    EXP -- 照標註來抓 --> NODE
    NM --> NODE
    TR --> NODE
    KUBE --> NODE
    NODE -- trace 依 trace_id 分配 --> TS
  end

  subgraph ES["中央 Elasticsearch 9（OTLP 入口，一把寫入 key）"]
    direction LR
    EL["log<br/>k8s-&lt;group&gt;-&lt;cluster&gt;<br/>（中央 pipeline 統一格式）"]
    EM["metrics<br/>metrics-*.otel-&lt;group&gt;"]
    ET["trace<br/>traces-generic.otel-&lt;group&gt;"]
  end

  NODE -- log --> EL
  NODE -- metrics --> EM
  CC -- metrics --> EM
  TS -- 留下的 trace --> ET

  subgraph AI["AIOps（Python，唯讀 key）"]
    direction TB
    B1["跟平常比（baseline）<br/>metrics 7 條：請求數、錯誤率、p95、CPU、記憶體、log 量<br/>trace 每一段：次數、時間、錯誤、吞掉例外、Kafka 排隊"]
    B2["規則（不用學）<br/>狀態變化、離極限多遠、Kafka 落後<br/>N+1、吞掉的例外、沒見過的呼叫"]
    B3["跟兄弟比<br/>同服務的 Pod 互比：錯誤率、p95、CPU、記憶體"]
    RCA["根因分析<br/>metrics 發現 → trace 找哪一段 → log 找新的錯誤樣式"]
    AN[("aiops-anomalies")]
    B1 --> AN
    B2 --> AN
    B3 --> AN
    AN --> RCA
  end

  EL --> AI
  EM --> AI
  ET --> AI
  AN -.-> OUT["告警、合併（還沒做）"]
```

## 三種資料各自怎麼收

```mermaid
flowchart LR
  subgraph L["log"]
    L1["應用寫 stdout"] --> L2["node Collector 讀<br/>/var/log/pods"] --> L3["logs-generic.otel-&lt;group&gt;"] --> L4["中央 pipeline<br/>依格式解析<br/>（logback、ECS、.NET JSON、<br/>Serilog、logfmt、純文字）"] --> L5["k8s-&lt;group&gt;-&lt;cluster&gt;"]
  end
  subgraph M["metrics"]
    M1["Java：Collector 來抓（pull）<br/>.NET：應用推（push）<br/>kubelet、cAdvisor、K8s 狀態、exporter"] --> M2["直方圖換成<br/>每段時間多少"] --> M3["metrics-*.otel-&lt;group&gt;"]
  end
  subgraph T["trace"]
    T1["應用全部送"] --> T2["node Collector<br/>例外事件抄到 span 屬性"] --> T3["trace-sampler<br/>尾端取樣"] --> T4["traces-generic.otel-&lt;group&gt;<br/>（約剩 15%）"]
  end
```

## log、metrics、trace 共用什麼

| 共用的東西 | 說明 | 在哪 |
|---|---|---|
| 同一種收集器 | 都是 OpenTelemetry Collector：每個節點一台（node），K8s 物件狀態一台（cluster），trace 多兩台 trace-sampler | `telemetry/collectors/` |
| 同一份分部門規則 | 擋冒充、讀 namespace 的 `dept`、對應成 group；同一個 Pod 的三種資料一定落在同一個 group | `common.yaml` 的 `transform/group` |
| 同一把寫入 key | 一把，所有 group、三種資料共用；資料屬於哪個 group 由 Collector 依 Pod 決定，不靠 key 擋（沒有部門間寫入隔離的要求；少了「key 權限漏了 → ES 回 200 默默拒收」的機會）（[pdca/phase1-collect/2026-10-ingest-simplify.md](pdca/phase1-collect/2026-10-ingest-simplify.md)） | `es-writer-keys`、`telemetry/scripts/elasticsearch-setup.sh` |
| 同一套身分欄位 | `service.name`、namespace、Pod、`dept`、`group`，由 Collector 補，三種資料對得上；其中 `dept`、`group` 是**歸屬**（誰的資料），其餘是**共同鑰匙**（接起三種資料），兩者用途不同，見 [rca/README.md](rca/README.md#歸屬-label-和共同鑰匙是兩種東西)。異常總表也帶 `dept`、`group` | Collector、`engine/detector/owner.py` |
| 串接鑰匙 | trace 的 `trace_id` ↔ log 的 `trace.id`；metrics 沒有 trace_id（ES 不存 exemplar，見 [rca/README.md「為什麼不用 exemplar」](rca/README.md#為什麼不用-exemplar用服務路由時間撈一批)），改用服務＋時間 | — |
| 同一把 AIOps 唯讀 key | 讀三種資料；只寫 `aiops-*` | `elasticsearch-setup.sh` |

**代價**：`dept` 清單目前要手動同步兩處（`telemetry/groups.yaml`、`common.yaml`）；歸屬方式（讀 namespace label 或改成 app → group 清單）一改，三條線要一起改（[README「還沒定案的」](../README.md#還沒定案的)）。
