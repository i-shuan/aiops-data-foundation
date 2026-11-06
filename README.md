# it-aiops

在公司已經有的 Elastic APM 資料上做 AIOps：**動態基準線學習 (Dynamic Baseline Learning)、抓異常、從 metrics 串到 trace 和 log 找根因，告警交給 Grafana 發出去，資深維運專家（老師傅）的回饋再回頭調整門檻。**

**不收值。** 應用用 Elastic APM agent 埋點，apm-server 把 log、metrics、trace 寫進公司的 Elasticsearch（`traces-apm*`、`logs-apm*`、`metrics-apm*`）；這個 repo 只讀它們，寫自己的 `aiops-*` 索引。

**目前範圍：一般服務**（Web API、worker、Kafka、DB）。工廠（EAP、機台、MES／MCS／AMHS、批號載具）不在目前範圍。

## 架構

```
應用 + APM agent ──► Elastic APM ──► 公司 ES（traces-apm*、logs-apm*、metrics-apm*）      ① ② ③ 公司現有，不動
                                          │ 只讀
                                          ▼
                          aiops-engine（④ 判斷 ⑥ 收斂 ⑦ 根因分析）
                                          │ 寫
                                          ▼
                       ES：aiops-*、traces-merged-*（⑤ 異常總表、告警）
                                          │ 讀
                                          ▼
                    Grafana（⑧ 通知、儀表板、回饋表單）──► 派工系統／值班（webhook）
                                          │
                                          ▼
                  ⑨ 老師傅回饋 ──► aiops-feedback ──► 調整門檻（回到 ④）
```

**編號只有一套：①～⑨ 資料模組**（定義在 [docs/architecture.md](docs/architecture.md)）：① 收值、② 清資料、③ 存、④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback。①②③ 是公司的 Elastic APM 與 ES；④～⑨ 是這個 repo。

## 文件怎麼分

```
docs/
  architecture.md    架構規格書（從這裡看起）：目標與邊界、架構圖、關鍵決策、①～⑨ 職責、關聯與根因定位、現況
  rca/               判斷與根因分析：baseline、串接、髒資料排除、雜訊壓制
  feedback/          告警出口、事故紀錄、回饋、調整、LLM
  aiops-data-reference.md   aiops-* 索引的欄位
  data-storage-reference.md 資料存哪裡
  pdca/              現況與待辦（依 PDCA 分）、每一輪紀錄、TEMPLATE.md
```

要部署看 [docs/deploy-how-to.md](docs/deploy-how-to.md)。閱讀順序：[architecture.md](docs/architecture.md) → [aiops-engine/configs/apm/README.md](aiops-engine/configs/apm/README.md)（怎麼接 Elastic APM 的資料）→ 各主題。

| 資料夾 | 主文件 |
|---|---|
| **rca** | [rca/README.md](docs/rca/README.md)：根因分析六步、三種資料怎麼串、動態基準線學習、15 個故障案例；程式：[aiops-engine/configs/baseline](aiops-engine/configs/baseline/README.md)、[aiops-engine/configs/rca](aiops-engine/configs/rca/README.md)（[報告範例](aiops-engine/configs/rca/example-report.md)） |
| **feedback** | [feedback/README.md](docs/feedback/README.md)：閉環、LLMOps 的定案、LLM 與 token |

## 已經定案的

- **收值用公司現有的 Elastic APM**，這個 repo 不放 Collector、不收值、不改 APM 寫入的索引。engine 只讀 `traces-apm*`、`logs-apm*`、`metrics-apm*`。
- **trace 100% 收、不取樣**（APM 端的要求）：engine 每分鐘把每個請求的 transaction 與 span 合成一筆寫進 `traces-merged-*`（7 天）；製造業的事故檢討不能少資料。
- **AIOps 以統計＋LLM 為主，不自己訓練模型**（[feedback/README.md](docs/feedback/README.md)）。
- **告警一個服務一件；通知交給 Grafana；部門決定誰看得到。**
- **業務欄位名稱**：`lot.id`、`equipment.id`、`work_order.id`、`recipe.id`（APM 用 `labels.*` 帶）。工廠用，之後再接。

## 還沒定案的

1. **歸屬放哪裡**：部門（dept、group）現在讀 APM agent 設的 `labels.dept`、`labels.group`；沒設的告警全進 `unassigned`。要改成 engine 自己維護「服務 → 部門」的對照表，或各服務的 agent 都加 label。
2. **Pod 資源**：公司的 APM 沒有 `metrics-apm.app.*`（agent 的 system metrics），Pod CPU／記憶體、記憶體洩漏規則做不了；要不要開 agent 的 system metrics，或另外收 kubelet。
3. **例外吞噬檢測**：APM 的例外是 `logs-apm.error-*` 的獨立文件，還沒接回 span。
4. **保留天數**：`traces-merged-*` 7 天；aiops-* 的保留與備份見 [data-storage-reference.md](docs/data-storage-reference.md)。
5. **engine 的 OTel 來源**：engine 同時支援 OTel Collector 寫的格式（`source: otel`）與 Elastic APM（`source: apm`）。收值已刪，OTel 來源是否一併刪除待決定。

## 待辦

依 PDCA 分（Plan 定門檻、Do 收值與判斷、Check 看對不對、Act 調整），整份在 [docs/pdca/](docs/pdca/README.md)。**缺的是真實 APM 資料上的驗證、真的 LLM、K8s 上跑**（[docs/architecture.md「還沒做、還不足的」](docs/architecture.md#10-還沒做還不足的)）。範圍是一般服務。

## 目錄

一個部署元件一個資料夾，裡面固定 `README.md`（怎麼部署）、程式與設定檔、`helmchart/`（Helm chart）；`grafana/`、`elasticsearch/` 的程式與設定檔放在 `code/`，`aiops-engine/` 直接放在資料夾裡。

```
elasticsearch/   kibana-dev-tools.txt（貼進 Kibana Dev Tools 的一份檔）
grafana/         code/（規則、contact point、policy、儀表板、外掛說明）  helmchart/（Grafana＋initContainer 裝外掛＋provisioning）
aiops-engine/    code/（Python、configs、Dockerfile）                  helmchart/（mode：一隻 pod，或 api＋beat＋worker＋redis）
docs/  tools/
```

| 資料夾 | 模組 | 內容 |
|---|---|---|
| `elasticsearch/` | ③ | 在公司 ES 上補的東西：一份貼進 Kibana Dev Tools 的檔（`traces-merged-*` 的 ILM 與 template、engine 與 Grafana 的 key、選用的 snapshot）。不部署 ES、不碰 APM 的索引 |
| `grafana/` | ⑧ | 讀 `aiops-alerts` 依部門發通知、儀表板、回饋表單外掛 |
| `aiops-engine/` | ④～⑨ | AIOps 分析核心：Python 程式在 `src/`：`schema/`（設定欄位定義）、`core/`（ES 連線、資料物件、repository）、`scenario/` 情境；`service/` 偵測、收斂、根因分析、回饋；`interface/` 命令列、API、排程器與 worker），指令一律 `python3 -m main <detector|investigator|scheduler|tasks|api> …`；`configs/`（`apm/` Elastic APM 版、`baseline/` `rca/` `schedule/` OTel 版） |
| `docs/` | — | 設計文件、規劃、`docs/pdca/` 每一輪的紀錄 |

Python 的執行位置在 `aiops-engine/`：`cd aiops-engine && python3 -m main detector <指令>`、`python3 -m main investigator investigate`。
