# 系統總覽

一句話：**每個應用 cluster 一套 OpenTelemetry Collector，把 log、metrics、trace 依 namespace 的部門分流、用各部門自己的 key 寫進中央 Elasticsearch；AIOps 用唯讀 key 讀出來，學「平常的樣子」、跑規則、Pod 互比、做根因分析，結果寫進 `aiops-anomalies`。** 舊專案不改程式（Java agent、.NET 自動埋點）。目前範圍是一般服務，工廠之後再做（[factory-later.md](factory-later.md)）。

## 架構圖

```mermaid
flowchart TB
  subgraph APP["應用 cluster"]
    direction TB
    subgraph PODS["應用 Pod（不改程式：Java agent／.NET 自動埋點）"]
      LOGS["stdout log"]
      JM["Java metrics<br/>開 /metrics"]
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

  subgraph ES["中央 Elasticsearch 9（OTLP 入口，各 group 用自己的寫入 key）"]
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

## 三種資料怎麼收

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

## 各部分的文件

| 主題 | 文件 |
|---|---|
| **全部串起來看（收值 → 判斷 → 串接 → 收斂 → LLM → feedback）** | [system-map.md](system-map.md) |
| log 怎麼收、格式解析 | [elasticsearch.md](elasticsearch.md) |
| metrics 怎麼收 | [metrics.md](metrics.md)；收了哪些、能做什麼：[aiops-metrics.md](aiops-metrics.md)、案例 [aiops-metrics-cases.md](aiops-metrics-cases.md) |
| trace 怎麼收、尾端取樣 | [traces.md](traces.md)；能做什麼：[aiops-trace.md](aiops-trace.md) |
| .NET | [dotnet.md](dotnet.md) |
| baseline、規則、跟兄弟比 | [aiops/baseline/README.md](../aiops/baseline/README.md) |
| 根因分析 | [aiops-rca.md](aiops-rca.md)、[aiops/rca/README.md](../aiops/rca/README.md) |
| PoC 現況與下一步（簡報） | [poc-status.md](poc-status.md) |
| 落地五階段：階段一現況、階段二規劃 | [stage2-plan.md](stage2-plan.md) |
| 後期接 LLM 還是 MLOps | [aiops-llm-mlops.md](aiops-llm-mlops.md) |
| 保留天數與容量 | [retention.md](retention.md) |
| 工廠部分（之後再做） | [factory-later.md](factory-later.md) |
| 全部文件、閱讀順序、待辦 | [README](../README.md) |
