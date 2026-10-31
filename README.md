# it-aiops

替 AIOps 打資料地基，並在上面做出第一版 AIOps：**讓每筆資料一出生就帶身分證、送進對的地方；再做動態基準線學習 (Dynamic Baseline Learning)、抓異常、從 metrics 串到 trace 和 log 找根因。**

**目前範圍：一般服務**（Web API、worker、Kafka、DB）。工廠（EAP、機台、MES／MCS／AMHS、批號載具）不在目前範圍。

## 文件怎麼分

```
docs/
  pdca/              現況與待辦（依 PDCA 分）、每一輪紀錄、TEMPLATE.md
  architecture.md    架構規格書（從這裡看起）：架構規格＋現況與驗證
                     第一部分：目標與邊界、架構圖、關鍵決策、①～⑨ 職責、關聯與根因定位
                     第二部分：各 phase 與各模組現況、程式與驗證、還沒做的、每份 PDCA 紀錄的結論
  log/               log：收值、格式解析、欄位（①②③的現況與待辦）
  metrics/           metrics：收值、欄位、收了哪些、案例、.NET（①②③的現況與待辦）
  trace/             trace：收值、100% 收與合成一筆、欄位、Kafka 串接、業務 ID（①②③的現況與待辦）
  rca/               判斷與根因分析：baseline、串接、髒資料排除、雜訊壓制
  feedback/          告警出口、事故紀錄、回饋、調整、LLM
  retention.md       保留天數與容量（log、metrics、trace 共用）
```

**編號只有一套：①～⑨ 資料模組**（定義在 [docs/architecture.md](docs/architecture.md)）：① 收值、② 清資料、③ 存、④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback。每個資料夾的 `README.md` 開頭都有「現況與待辦」，每一項標它屬於哪個模組。

閱讀順序：[architecture.md](docs/architecture.md)（先架構規格、再現況與驗證）→ 各資料夾。

| 資料夾 | 主文件 | 其他 |
|---|---|---|
| **log** | [log/README.md](docs/log/README.md)：怎麼收、依格式解析、欄位規格、換 key、Fluent Bit 與 OTel 的比較 | |
| **metrics** | [metrics/README.md](docs/metrics/README.md)：怎麼收、直方圖換增量、欄位、能做到什麼、.NET 不改程式 | |
| **trace** | [trace/README.md](docs/trace/README.md)：100% 收、一個請求合成一筆、欄位、六個場景 | [sdk/standards/tracing.md](sdk/standards/tracing.md)（給應用團隊的埋點指引） |
| **rca** | [rca/README.md](docs/rca/README.md)：根因分析六步、三種資料怎麼串、動態基準線學習、15 個故障案例 | 程式：[aiops-engine/code/configs/baseline](aiops-engine/code/configs/baseline/README.md)、[aiops-engine/code/configs/rca](aiops-engine/code/configs/rca/README.md)（[報告範例](aiops-engine/code/configs/rca/example-report.md)） |
| **feedback** | [feedback/README.md](docs/feedback/README.md)：閉環、LLMOps 的定案、LLM 與 token | |

## 已經定案的

- **log、metrics、trace 都進 Elasticsearch（9.x）**，不用 VictoriaMetrics。
- **收集器都用 OpenTelemetry Collector**：每個節點一個，分部門的規則和 key 共用。log 原本用 Fluent Bit，比較後改用 OTel（[log/README.md](docs/log/README.md)）；中央 pipeline 仍接受 Fluent Bit 的資料，邊緣 VM 可以用。
- **集中式資料寫入憑證 (Shared Ingest Token)**：一把 key，所有 group、log、metrics、trace 共用；資料屬於哪個 group 由 Collector 依 Pod 決定（讀取權限照樣依 group 分）。**換 key 不掉資料、不重啟**（`elasticsearch/code/scripts/rotate-writer-key.sh`）。被 ES 拒絕的文件留在 failure store，`quality.ingest_rejected` 會報。
- **舊專案不改程式**：Java 用 agent，.NET 用自動埋點＋OTLP 推（[metrics/README.md](docs/metrics/README.md)）。
- **trace 100% 收、不取樣**：Collector 全寫（每一段一筆、2 天），engine 每分鐘把每個請求合成一筆（7 天）；製造業的事故檢討不能少資料。
- **應用團隊不需要知道 group、key**：規格由共用 chart 產生（[apps/](sdk)）。
- **namespace 名稱不含組織資訊**（`{cluster}-{app}-{env}` 或 `{app}-{env}`），歸屬另外標。
- **AIOps 以統計＋LLM 為主，不自己訓練模型**（[feedback/README.md](docs/feedback/README.md)）。
- **業務欄位名稱**：`lot.id`、`equipment.id`、`work_order.id`、`recipe.id`（[sdk/standards/tracing.md](sdk/standards/tracing.md)）。工廠用，之後再接。

## 還沒定案的

1. **歸屬放哪裡**：現在讀 namespace 的 `dept` label，部門 → group 對照在 `collector/code/groups.yaml`。要改成「app → group」的中央清單的話，log、metrics、trace 要一起改。
2. **app 名稱是否全公司唯一**（用中央清單的話，這是前提）。
3. **寫入 key 怎麼自動進新的 cluster**：現在靠 Rancher 手動建 Secret；cluster 一多就不可行，要確認有沒有 External Secrets、Vault 或 sealed-secrets。
4. **K8s events 怎麼歸屬**：還沒收 events，照讀 namespace label 的做法應該能分，要實測。
5. **保留天數**：✅ 已套用建議值（metrics 原始 14 天、降解析度到 400 天；log 原文 30 天＋統計 400 天；trace 隨機 7 天、有問題的 30 天），天數在 `elasticsearch/code/scripts/retention.conf`。還要決定：天數確認、各部門要不要不同、正式環境的副本，以及用正式環境的量重算容量（[docs/retention.md](docs/retention.md#還要決定的)）。
6. **Java 要不要預設加 `-XX:+ExitOnOutOfMemoryError`**：不加的話 OOM 後 Pod 活著但不能用。

## 待辦

依 PDCA 分（Plan 定門檻、Do 收值與判斷、Check 看對不對、Act 調整），整份在 [docs/pdca/](docs/pdca/README.md)。閉環的每一段都有程式、模擬資料跑通；**缺的是真實資料、真的 LLM、K8s 上跑**（[docs/architecture.md「還沒做、還不足的」](docs/architecture.md#10-還沒做還不足的)）。範圍是一般服務。

## 目錄

一個部署元件一個資料夾，裡面固定 `README.md`（怎麼部署）、`code/`（程式與設定檔）、`helmchart/`（Helm chart；測試用 `values-test.yaml`）。

```
collector/       code/（common、node、cluster、groups.yaml）           helmchart/（DaemonSet、Deployment、RBAC）
elasticsearch/   code/storage/、code/scripts/                           helmchart/（只給測試用的 ES，twoNode 開關）
grafana/         code/（規則、contact point、policy、儀表板、外掛說明）  helmchart/（Grafana＋initContainer 裝外掛＋provisioning）
aiops-engine/    code/（Python、configs、Dockerfile）                  helmchart/（排程器＋回饋網頁一隻 pod）
sdk/  e2e/（pipeline/、k8s/）  docs/  tools/
```

| 資料夾 | 模組 | 內容 |
|---|---|---|
| `collector/` | ①② | 每個應用 cluster 一套 OpenTelemetry Collector：收 log、metrics、trace，邊緣身份鑑別與標籤注入、依 namespace 的 dept 分 group |
| `elasticsearch/` | ③ | 中央 ES 的結構設定（mapping、pipeline、ILM）與 setup／換 key 腳本；正式 ES 是公司的，chart 只部署測試用 ES |
| `grafana/` | ⑧ | 讀 `aiops-alerts` 依部門發通知、儀表板、回饋表單外掛 |
| `aiops-engine/` | ④～⑨ | AIOps 分析核心：`core/`（ES、trace、LLM）、`detector/`（動態基準線學習、規則、資料品質、回饋、回測、量化）、`investigator/`（根因分析、事件整合、相似事故）、`feedback_web/`（老師傅的回饋網頁）、`scheduler/`（定期工作的排程器）、`configs/`（baseline、rca、schedule） |
| `sdk/` | ① | 給應用團隊：通用 Helm chart、範例、Java log 設定、trace 埋點指引、CI 檢查 |
| `e2e/` | 測試 | `pipeline/`（ES pipeline 案例，只要 ES）、`k8s/`（端到端：模擬服務與腳本，要 K8s）；不是正式程式 |
| `docs/` | — | 設計文件、規劃、`docs/pdca/` 每一輪的紀錄 |

Python 的執行位置在 `aiops-engine/code/`：`cd aiops-engine/code && python3 -m detector <指令>`、`python3 -m investigator investigate`。
