# it-aiops

替 AIOps 打資料地基，並在上面做出第一版 AIOps：**讓每筆資料一出生就帶身分證、送進對的地方；再做動態基準線學習 (Dynamic Baseline Learning)、抓異常、從 metrics 串到 trace 和 log 找根因。**

**目前範圍：一般服務**（Web API、worker、Kafka、DB）。工廠（EAP、機台、MES／MCS／AMHS、批號載具）不在目前範圍。

## 文件怎麼分

```
docs/
  pdca/              現況與待辦（依 PDCA 分）、每一輪紀錄、TEMPLATE.md
  architecture.md    架構規格書（從這裡看起）：目標與邊界、架構圖、關鍵決策、①～⑨ 職責、關聯與根因定位
  log/               log：收值、格式解析、欄位（①②③的現況與待辦）
  metrics/           metrics：收值、欄位、收了哪些、案例、.NET（①②③的現況與待辦）
  trace/             trace：收值、尾端取樣、欄位、Kafka 串接、業務 ID（①②③的現況與待辦）
  rca/               判斷與根因分析：baseline、串接、髒資料排除、雜訊壓制
  feedback/          告警出口、事故紀錄、回饋、調整、LLM
  retention.md       保留天數與容量（log、metrics、trace 共用）
  poc-status.md      各 phase 與各模組現況、程式與驗證、還沒做的、每份 PDCA 紀錄的結論
```

**編號只有一套：①～⑨ 資料模組**（定義在 [docs/architecture.md](docs/architecture.md)）：① 收值、② 清資料、③ 存、④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback。每個資料夾的 `README.md` 開頭都有「現況與待辦」，每一項標它屬於哪個模組。

閱讀順序：[architecture.md](docs/architecture.md) → [poc-status.md](docs/poc-status.md) → 各資料夾。

| 資料夾 | 主文件 | 其他 |
|---|---|---|
| **log** | [log/README.md](docs/log/README.md)：怎麼收、依格式解析、欄位規範、換 key | [fluentbit-vs-otel.md](docs/log/fluentbit-vs-otel.md)（改用 OTel 的比較） |
| **metrics** | [metrics/README.md](docs/metrics/README.md)：怎麼收、直方圖換增量、欄位 | [catalog.md](docs/metrics/catalog.md)（收了哪些、能做到什麼）、[cases.md](docs/metrics/cases.md)（故障案例實測）、[metrics/dotnet.md](docs/metrics/dotnet.md)（.NET 不改程式） |
| **trace** | [trace/README.md](docs/trace/README.md)：兩層 Collector、尾端取樣、欄位、Kafka、業務 ID | [scenarios.md](docs/trace/scenarios.md)（trace 能做的六個場景）、[sdk/standards/tracing.md](sdk/standards/tracing.md)（給應用團隊的埋點指引） |
| **rca** | [rca/README.md](docs/rca/README.md)：三種資料各回答什麼、metrics 怎麼串到 trace 和 log、要哪種「平常」、髒資料、雜訊 | [cases.md](docs/rca/cases.md)（15 個故障案例）；程式：[engine/configs/baseline](engine/configs/baseline/README.md)、[engine/configs/rca](engine/configs/rca/README.md)（[報告範例](engine/configs/rca/example-report.md)） |
| **feedback** | [feedback/README.md](docs/feedback/README.md)：告警出口、事故紀錄、回饋、調整、LLM 與 token | [llm-mlops.md](docs/feedback/llm-mlops.md)（LLM 還是 MLOps 的定案） |

## 已經定案的

- **log、metrics、trace 都進 Elasticsearch（9.x）**，不用 VictoriaMetrics。
- **收集器都用 OpenTelemetry Collector**：每個節點一個，分部門的規則和 key 共用。log 原本用 Fluent Bit，比較後改用 OTel（[docs/log/fluentbit-vs-otel.md](docs/log/fluentbit-vs-otel.md)）；中央 pipeline 仍接受 Fluent Bit 的資料，邊緣 VM 可以用。
- **集中式資料寫入憑證 (Shared Ingest Token)**：一把 key，所有 group、log、metrics、trace 共用；資料屬於哪個 group 由 Collector 依 Pod 決定（讀取權限照樣依 group 分）。**換 key 不掉資料、不重啟**（`telemetry/scripts/rotate-writer-key.sh`）。被 ES 拒絕的文件留在 failure store，`quality.ingest_rejected` 會報。
- **舊專案不改程式**：Java 用 agent，.NET 用自動埋點＋OTLP 推（[docs/metrics/dotnet.md](docs/metrics/dotnet.md)）。
- **trace 用尾端取樣**：錯的、慢的、有例外的全留，正常的留 5%。
- **應用團隊不需要知道 group、key**：規格由共用 chart 產生（[apps/](sdk)）。
- **namespace 名稱不含組織資訊**（`{cluster}-{app}-{env}` 或 `{app}-{env}`），歸屬另外標。
- **AIOps 以統計＋LLM 為主，不自己訓練模型**（[docs/feedback/llm-mlops.md](docs/feedback/llm-mlops.md)）。
- **業務欄位名稱**：`lot.id`、`equipment.id`、`work_order.id`、`recipe.id`（[sdk/standards/tracing.md](sdk/standards/tracing.md)）。工廠用，之後再接。

## 還沒定案的

1. **歸屬放哪裡**：現在讀 namespace 的 `dept` label，部門 → group 對照在 `telemetry/groups.yaml`。要改成「app → group」的中央清單的話，log、metrics、trace 要一起改。
2. **app 名稱是否全公司唯一**（用中央清單的話，這是前提）。
3. **寫入 key 怎麼自動進新的 cluster**：現在靠 Rancher 手動建 Secret；cluster 一多就不可行，要確認有沒有 External Secrets、Vault 或 sealed-secrets。
4. **K8s events 怎麼歸屬**：還沒收 events，照讀 namespace label 的做法應該能分，要實測。
5. **保留天數**：✅ 已套用建議值（metrics 原始 14 天、降解析度到 400 天；log 原文 30 天＋統計 400 天；trace 隨機 7 天、有問題的 30 天），天數在 `telemetry/scripts/retention.conf`。還要決定：天數確認、各部門要不要不同、正式環境的副本，以及用正式環境的量重算容量（[docs/retention.md](docs/retention.md#還要決定的)）。
6. **Java 要不要預設加 `-XX:+ExitOnOutOfMemoryError`**：不加的話 OOM 後 Pod 活著但不能用。

## 待辦

依 PDCA 分（Plan 定門檻、Do 收值與判斷、Check 看對不對、Act 調整），整份在 [docs/pdca/](docs/pdca/README.md)。閉環的每一段都有程式、模擬資料跑通；**缺的是真實資料、真的 LLM、K8s 上跑**（[docs/poc-status.md「還沒做、還不足的」](docs/poc-status.md#還沒做還不足的)）。範圍是一般服務。

## 目錄

```
telemetry/   收值與存（①②③）：collectors/（各 cluster 的 OpenTelemetry Collector）、storage/（中央 ES：mapping、pipeline、ILM）、
             scripts/（setup、換 key）、groups.yaml（部門 → group）
engine/      AIOps 分析核心（④～⑨，一個 Python 專案）：core/（共用：ES、trace、LLM）、detector/（動態基準線學習、規則、資料品質、回饋、回測、量化）、
             investigator/（根因分析、事件整合、相似事故）、feedback_web/（老師傅的回饋網頁）、configs/（baseline、rca、grafana 的 yaml）、
             deploy/（K8s CronJob）、Dockerfile
sdk/         給應用團隊：通用 Helm chart、範例、Java log 設定、trace 埋點指引、CI 檢查
e2e/         本機 k3s 端到端測試、ES pipeline 案例
docs/        設計文件、規劃、測試紀錄（docs/pdca/ 每一輪的紀錄）
```

Python 的執行位置在 `engine/`：`cd engine && python3 -m detector <指令>`、`python3 -m investigator investigate`。
