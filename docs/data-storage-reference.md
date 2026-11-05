# 資料存哪裡：ES 與 Grafana

> **類型：規格 Reference**　同主題：[aiops-* 的欄位](aiops-data-reference.md)・[回饋與學習](feedback/README.md)

AIOps 與 Grafana 的資料各存在哪裡、誰寫、多久一次、會不會自己消失。**沒有模型檔、沒有本機資料庫**：訓練的結果、RCA 報告、回饋，全部是中央 ES 裡的索引。Grafana 自己幾乎不存資料。

## 一眼看懂

| 問題 | 答案 |
|---|---|
| 訓練（基準線）存哪？ | ES 的 `aiops-baseline`，不是模型檔 |
| RCA 報告存哪？ | ES 的 `aiops-alerts`，一件一筆，報告全文在 `report` 欄位 |
| Grafana 怎麼拿到？ | 直接讀 ES 的 `aiops-*`；通知裡的報告連結由回饋網頁從 `aiops-alerts` 讀出來顯示 |
| 會自己消失的有哪些？ | 原始資料有 ILM（log、metrics、trace）；`aiops-baseline` 每次訓練被取代；`aiops-anomalies`、`aiops-trace-segments`（90 天）、結案的 `aiops-alerts`（180 天）由 `detector prune` 每天刪。人工判斷的三個索引**不刪、每天 snapshot** |
| Grafana 自己存什麼？ | 設定在 repo 的檔案；內部 sqlite 在容器裡、沒掛 PVC，Pod 重建就清掉 |

## ES：AIOps 寫的索引

| 索引 | 存什麼 | 誰寫、多久一次 | 會不會自己消失 |
|---|---|---|---|
| `aiops-baseline` | 動態基準線：每條序列 × 星期幾 × 小時的中位數、MAD、p05、p95 | `detector train`，每天 02:00 | **被取代**：先寫新的，再刪這次沒有的舊版（改了粒度、4 週內沒出現的 API）；ES 查不到任何資料時不刪 |
| `aiops-trace-segments` | trace 每一段的統計：每個 API × 每一段 × 每 5 分鐘的呼叫次數、耗時、錯誤率、吞例外比例；另有時鐘差 | `detector trace-collect`，每 5 分鐘 | `detector prune` 每天刪超過 **90 天**的（[prune.yaml](../aiops-engine/code/configs/baseline/prune.yaml)） |
| `aiops-anomalies` | 異常總表：每個異常一筆（序列、對象、值、平常值、z、status） | `detect`（每分鐘）、`rules`（每分鐘）、`peer`、`trace-detect`、`quality` | `detector prune` 每天刪超過 **90 天**的；同一個對象同一時槽的 `_id` 固定，重跑是覆蓋 |
| `aiops-alerts` | 事故（一件一筆）：標題、摘要、**RCA 報告全文**、LLM 摘要、相似事故、連帶的上游服務；另有 `heartbeat`、`detect-status` 兩筆狀態 | `investigator alert`，每分鐘；開著的件每輪覆蓋同一筆 | `detector prune` 每天刪**結案超過 180 天**的；開著的件、心跳、`detect-status` 不刪。每天的 snapshot 也帶它 |
| `aiops-incidents` | 事故庫：老師傅確認過的事故、根因分類、怎麼看出來的、指紋 | 回饋網頁、`incident` 指令、`import-incidents` | **不會**，刻意不刪；每天 snapshot（[備份與還原](../elasticsearch/README.md#備份與還原aiops--的人工判斷)） |
| `aiops-feedback` | 值班回饋：真的／誤報／根因錯／漏報，一句話與抽取結果 | 回饋網頁、`feedback` 指令 | **不會**，刻意不刪；每天 snapshot |
| `aiops-tuning` | 偵測的調整與建議（override、排除時段、週期放寬、`rule_suggestion`），含 `status`、誰核准 | `tune`、`adjust`、`feedback suggest`、回饋網頁 | **不會**；`expires_at` 到期只是不再生效，文件還在；每天 snapshot |
| `aiops-state` | `trace-merge` 合到哪個時間（就一筆） | `detector trace-merge`，每分鐘 | 每次覆蓋同一筆 |
| `aiops-log-stats` | log 長期統計：每 5 分鐘 × 每個服務 × level × error.type 的筆數 | ES 的 transform，不是 engine | **會**，ILM 400 天 |

欄位細節：`aiops-anomalies`、`aiops-alerts`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning` 見 [aiops-data-reference.md](aiops-data-reference.md)；`aiops-baseline`、`aiops-trace-segments`、`aiops-state` 的 mapping 在 [detector/es.py](../aiops-engine/code/detector/es.py)、[detector/trace.py](../aiops-engine/code/detector/trace.py)、[detector/trace_merge.py](../aiops-engine/code/detector/trace_merge.py)。誰在什麼時候跑見 [schedule.yaml](../aiops-engine/code/configs/schedule/schedule.yaml)。

## ES：收進來的原始資料

| 資料 | 資料流 | 保留 |
|---|---|---|
| log | `logs-apm.app.<服務>-*`、`logs-apm.error-*`（Elastic APM 寫） | 公司 ES 的 ILM 決定（2026-11 看到 `logs-apm.app.*` 10 天） |
| metrics | `metrics-apm.transaction.1m-*`、`metrics-apm.service_destination.1m-*` 等（Elastic APM 寫，每分鐘一點） | 公司 ES 的 ILM 決定（2026-11 看到 `.1m` 的 90 天） |
| trace raw（transaction、span 各一筆） | `traces-apm-*`（Elastic APM 寫） | 公司 ES 的 ILM 決定 |
| trace merged（一個請求一筆，engine 合成） | `traces-merged-<group>-<日期>` | 7 天（[retention.conf](../elasticsearch/code/scripts/retention.conf)） |

APM 寫的三種資料 engine 只讀，保留天數不在這個 repo 管；`traces-merged-*` 要比 raw 留得久才有意義。

## RCA 報告：從產生到 Grafana

| 步驟 | 寫在哪、讀自哪 |
|---|---|
| 1. 根因分析、LLM 摘要、相似事故 | `investigator alert` 算，寫進 `aiops-alerts` 那一件：`report`（全文，不建索引）、`llm_summary`、`findings`、`similar`（結構化，不建索引） |
| 2. 通知 | Grafana 告警規則讀 `aiops-alerts`，帶 `title`、`summary`、`report_url`、`feedback_url` 這些 keyword 欄位（告警只能帶 labels） |
| 3. 看報告全文 | 通知裡的 `report_url` → 回饋網頁 `/report?alert=…` 從 `aiops-alerts` 讀出來顯示 |
| 4. 儀表板 | Grafana 的資料來源直接讀 `aiops-alerts`、`aiops-anomalies`、`aiops-tuning`、`aiops-feedback`（[datasource.yaml](../grafana/helmchart/files/datasource.yaml)） |

## 回饋表單的資料流

回饋資料**不存在 Grafana，也不是 Grafana 寫的**：值班人員在瀏覽器填表，瀏覽器直接把資料送給回饋網頁，由它寫進 ES。Grafana 只畫表單、把填的內容送出去。

```
值班人員 ──瀏覽器──> Grafana 的表單（Business Forms 外掛）
                        │ 1. 開表單：GET  回饋網頁 /api/alert?alert=…    （預填告警資訊）
                        │ 2. 按送出：POST 回饋網頁 /api/feedback
                        ▼
                   engine 的 api（mode single 是同一隻 pod 的另一個 container；distributed 是 aiops-api）
                        │ 讀  aiops-alerts、aiops-anomalies
                        │ 寫  ↓
                        ▼
                   中央 ES 的三個索引
```

| 一次送出寫到 | 寫幾筆 | 內容 |
|---|---|---|
| `aiops-feedback` | 這件告警的**每個異常各一筆**（最多 200 筆） | 真的／誤報／根因錯、誰填、怎麼看出來的、兩題評分、一句話與 LLM 抽取結果。每個異常一筆，因為 `feedback summary`、`suggest` 是依「序列加對象」算的 |
| `aiops-incidents` | 這件一筆（用 `alert_id` 當 id，更新或補建） | 狀態 `confirmed`（真的）、`corrected`（根因報錯）、`false_positive`；真的時附根因分類、根因、處置、怎麼看出來的、查了哪裡、下次怎麼更快 |
| `aiops-tuning` | 有填一句話、且 LLM 抽得到調整種類時才寫一筆 | `status: proposed`，要人核准才生效（[architecture.md 3.5](architecture.md#35-人在迴圈中系統只建議人決定-human-in-the-loop)） |

- **漏報面板**走另一條：`POST /api/missed`，只寫 `aiops-feedback` 一筆（`verdict: missed`），沒有告警、不寫事故。
- **寫進去之後誰用**：`aiops-incidents` 的 `confirmed`、`corrected` 給相似事故檢索（自動開的 `auto` 事故不會拿來教人）；`aiops-feedback` 給 `feedback summary`、`feedback suggest`、`metrics`；`aiops-tuning` 的建議給 `adjust list`、回測、核准。
- **寫入用的是回饋網頁自己的 ES key，不是使用者的**：所以沒有登入也寫得進去；`by` 是自己填的名字，空的記成 `anonymous`，同一個人可以填多次，系統不知道是誰。
- **抽取失敗不影響回饋本身**：LLM 掛了或格式不對，只存原文（`extract.ok: false`）；建議寫不進去也一樣，回應多一個 `tuning_error`。
- 程式在 [api/app.py](../aiops-engine/code/api/app.py) 的 `submit`、`submit_missed`；欄位對照見 [grafana/README.md](../grafana/README.md#回饋表單放在-grafana-裡定案用這個)。

## Grafana 自己存什麼

| 項目 | 放哪 | Pod 重建後 |
|---|---|---|
| 資料來源、告警規則、通知對象、通知路由、儀表板（`aiops-rca`、`aiops-feedback`、`aiops-learning`） | repo 的 [grafana/helmchart/files/](../grafana/helmchart/files/)，部署時 provisioning 載入 | **回來**（在檔案裡） |
| 告警目前的狀態與歷史、使用者、在畫面上手動改的東西 | Grafana 內部 sqlite，**在容器裡，chart 沒掛 PVC** | **清掉** |
| 回饋表單外掛（Business Forms） | `emptyDir` | 由 initContainer 重新下載安裝 |

所以改儀表板、告警規則要改 repo 的檔案，不要在畫面上改。repo 的 chart 是測試用；正式環境 Grafana 是否外接資料庫、告警歷史留多久，**這份沒有資料**（要問管 Grafana 的人）。

## engine 的暫存檔

排程器 pod 有一個 `emptyDir`，放心跳檔（`touch`）、台灣行事曆的快取。Pod 重建就沒了，兩者都能重新產生。

## 保留與備份的現況

| 項目 | 現況 |
|---|---|
| `aiops-feedback`、`aiops-incidents`、`aiops-tuning`（人工判斷） | 不自動刪；setup 給了儲存庫設定就每天 snapshot 一次、留 30 天（SLM `aiops-daily`，也帶 `aiops-baseline`、`aiops-alerts`）。**儲存庫由管 ES 的人決定**，沒給就沒有備份（setup 會印提醒）。還原步驟：[elasticsearch/README.md](../elasticsearch/README.md#備份與還原aiops--的人工判斷) |
| `aiops-anomalies`、`aiops-trace-segments`、結案的 `aiops-alerts` | 排程 `prune` 每天 03:30 刪超過 90／90／180 天的（[prune.yaml](../aiops-engine/code/configs/baseline/prune.yaml)，天數是估的）。異常刪了之後，事故的 `anomaly_ids` 對不到的那幾筆，回饋退回「一件一筆」，其他不受影響 |
| `aiops-baseline` | 每天重寫的衍生資料；snapshot 有帶，還原後不用重 train。重建 ES 沒還原的話要手動跑一次 `train`，否則等隔天 02:00（期間基準線這一路停擺，Grafana 的 `aiops-baseline-missing` 會響） |

測試叢集 2026-11-01 的大小（`aiops-alerts` 32 筆 1 MB、`aiops-anomalies` 157 筆 0.5 MB、`aiops-baseline` 1,512 筆 0.5 MB、`aiops-trace-segments` 3,431 筆 1.2 MB）只是量級參考，不代表正式環境。
