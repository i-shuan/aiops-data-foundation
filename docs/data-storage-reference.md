# 資料存哪裡：PostgreSQL、ES 與 Grafana

> **類型：規格 Reference**　同主題：[engine 資料的欄位](aiops-data-reference.md)・[回饋與學習](feedback/README.md)

AIOps 與 Grafana 的資料各存在哪裡、誰寫、多久一次、會不會自己消失。**沒有模型檔、沒有本機資料庫**：engine 自己的資料（訓練的結果、異常、RCA 報告、事故、回饋、調整）2026-11 起存在公司的 **PostgreSQL**，一種資料一張表（[為什麼搬](pdca/meta/2026-11-postgres-store.md)）；收進來的原始資料（Elastic APM 寫的）和 engine 合成的 `traces-merged-*` 還在 **ES**。Grafana 自己幾乎不存資料。

## 一眼看懂

| 問題 | 答案 |
|---|---|
| 訓練（基準線）存哪？ | PostgreSQL 的 `aiops_baseline`，不是模型檔 |
| RCA 報告存哪？ | PostgreSQL 的 `aiops_alerts`，一件一筆，報告全文在 `doc` 的 `report` |
| Grafana 怎麼拿到？ | 用唯讀帳號直接查 PostgreSQL（SQL）；通知裡的報告連結由回饋網頁從 `aiops_alerts` 讀出來顯示 |
| 會自己消失的有哪些？ | 原始資料有 ILM（log、metrics、trace）；`aiops_baseline` 每次訓練被取代；`aiops_anomalies`（90 天）、結案的 `aiops_alerts`（180 天）由 `detector prune` 每天**先壓縮（一天打包成一筆）、再刪**（見下面「過期資料的壓縮」）。人工判斷的三張表**不刪**，備份是 PostgreSQL 的備份（DBA） |
| Grafana 自己存什麼？ | 設定在 repo 的檔案；內部 sqlite 在容器裡、沒掛 PVC，Pod 重建就清掉 |

## PostgreSQL：engine 自己的資料

每張表的欄位都一樣：`id`（文件 id）、`ts`（這張表的時間，見下表）、`doc`（整份文件，jsonb）、`updated_at`。程式在各功能的 `store/`（[common/infra/pg.py](../aiops-engine/src/common/infra/pg.py) 是基底）；建表 `detector db init`，每張表幾筆 `detector db status`。

| 表 | 存什麼 | 誰寫、多久一次 | 會不會自己消失 |
|---|---|---|---|
| `aiops_baseline`（`ts` = `trained_at`） | 動態基準線：每條序列 × 星期幾 × 小時的中位數、MAD、p05、p95 | `detector train`，每天 02:00 | **被取代**：先寫新的，再刪這次沒有的舊版（改了粒度、4 週內沒出現的 API）；原始資料查不到任何資料時不刪 |
| `aiops-trace-segments`（**ES**，別名） | trace 每一段的統計：每個 API × 每一段 × 每 5 分鐘的呼叫次數、耗時、錯誤率、吞例外比例；另有時鐘差 | `detector trace-collect`，每 5 分鐘 | ES 的 ILM 刪超過 **35 天**的（[kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 1b；trace-train 最多回看 4 週）。只有可觀測性用得到 trace，所以不放 PostgreSQL。筆數固定：API × 段 × 每 5 分鐘，跟流量、有沒有出事無關 |
| `aiops_anomalies` | 異常總表：每個異常一筆（序列、對象、值、平常值、z、status） | `detect`（每分鐘）、`rules`（每分鐘）、`peer`、`trace-detect`、`quality` | `detector prune` 每天打包、刪超過 **90 天**的；同一個對象同一時槽的 `_id` 固定，重跑是覆蓋 |
| `aiops_alerts` | 事故（一件一筆）：標題、摘要、**RCA 報告全文**、LLM 摘要、相似事故、歷史線索、連帶的上游服務；另有 `heartbeat`、`detect-status`、`source-check-*` 幾筆狀態 | `investigator alert`，每分鐘；開著的件每輪覆蓋同一筆 | `detector prune` 每天打包、刪**結案超過 180 天**的；開著的件、狀態文件不刪 |
| `aiops_packed` | 超過保留天數的資料，一天打包成一筆 gzip（見下面「過期資料的壓縮」） | `detector prune`，每天 03:30 | 超過 **365 天**的由 prune 刪（`pack.keep_days`） |
| `aiops_incidents`（`ts` = `start`） | 事故庫：老師傅確認過的事故、根因分類、怎麼看出來的、指紋 | 回饋網頁、`incident` 指令、`import-incidents` | **不會**，刻意不刪；備份靠 PostgreSQL 的備份 |
| `aiops_incident_vectors` | 確認過的事故的向量（相似事故的向量輔助；有設 embedding 才有） | 相似事故檢索時補算（事故改了就重算） | 跟著事故；pgvector 有裝另有 `embedding` 欄 |
| `aiops_feedback` | 值班回饋：真的／誤報／根因錯／漏報，一句話與抽取結果 | 回饋網頁、`feedback` 指令 | **不會**，刻意不刪 |
| `aiops_tuning`（`ts` = `at`） | 偵測的調整與建議（override、排除時段、週期放寬、`rule_suggestion`），含 `status`、誰核准 | `tune`、`adjust`、`feedback suggest`、`shift`、回饋網頁 | **不會**；`expires_at` 到期只是不再生效，文件還在 |
| `aiops_mapping`（`ts` = `created_at`） | 欄位對照的覆寫（提的、核准的、停用的） | 網頁 `/mapping`、`detector mapping` | **不會** |
| `aiops_chronic` | 慢性病清單，每週一份 | `detector chronic`，每週 | 不會 |
| `aiops_state` | 偵測的進度：`detect-watermark:<設定檔>` detect 看到哪、還有哪段空白沒補（停過之後補算用） | `detector detect`，每分鐘 | 每次覆蓋同一筆 |
| `aiops-trace-state`（**ES**） | `trace-merge` 合到哪個時間（一筆） | `detector trace-merge`，每分鐘 | 每次覆蓋同一筆 |

沒寫 `ts` 的表 `ts` 是 `@timestamp`（`aiops_state` 沒有）。欄位細節：`aiops_anomalies`、`aiops_alerts`、`aiops_incidents`、`aiops_feedback`、`aiops_tuning` 見 [aiops-data-reference.md](aiops-data-reference.md)；`aiops_baseline` 的欄位在 [detect/store/models.py](../aiops-engine/src/detect/store/models.py)；`aiops-trace-segments`、`aiops_state` 的在 [detect/store/segment_repo.py](../aiops-engine/src/detect/store/segment_repo.py)、[detect/store/state_repo.py](../aiops-engine/src/detect/store/state_repo.py)。誰在什麼時候跑見 [schedule.yaml](../aiops-engine/configs/schedule/schedule.yaml)。

## ES：收進來的原始資料

| 資料 | 資料流 | 保留 |
|---|---|---|
| log | `logs-apm.app.<服務>-*`、`logs-apm.error-*`（Elastic APM 寫） | 公司 ES 的 ILM 決定（2026-11 看到 `logs-apm.app.*` 10 天） |
| metrics | `metrics-apm.transaction.1m-*`、`metrics-apm.service_destination.1m-*` 等（Elastic APM 寫，每分鐘一點） | 公司 ES 的 ILM 決定（2026-11 看到 `.1m` 的 90 天） |
| trace raw（transaction、span 各一筆） | `traces-apm-*`（Elastic APM 寫） | 公司 ES 的 ILM 決定 |
| trace merged（一個請求一筆，engine 合成） | `traces-merged-<group>-<日期>` | 7 天（[kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 第 1 段） |

APM 寫的三種資料 engine 只讀，保留天數不在這個 repo 管；`traces-merged-*` 要比 raw 留得久才有意義。另有 `aiops-log-stats`（log 長期統計：每 5 分鐘 × 每個服務 × level × error.type 的筆數）：ES 的 transform 寫、不是 engine，ILM 400 天。

## RCA 報告：從產生到 Grafana

| 步驟 | 寫在哪、讀自哪 |
|---|---|
| 1. 根因分析、LLM 摘要、相似事故 | `investigator alert` 算，寫進 `aiops_alerts` 那一件：`report`（全文）、`llm_summary`、`findings`（含 `case_hints`）、`similar` |
| 2. 通知 | Grafana 告警規則用 SQL 查 `aiops_alerts` 開著的件，一列一則，`alert_id`、`dept`、`group`、`severity`、`summary`、`report_url` 變成 labels（告警只能帶 labels） |
| 3. 看報告全文 | 通知裡的 `report_url` → 回饋網頁 `/report?alert=…` 從 `aiops_alerts` 讀出來顯示 |
| 4. 儀表板 | Grafana 的資料來源 `aiops-db`（PostgreSQL，唯讀帳號）用 SQL 讀 `aiops_alerts`、`aiops_anomalies`、`aiops_tuning`、`aiops_feedback`（[datasource.yaml](../grafana/helmchart/files/datasource.yaml)） |

## 回饋表單的資料流

回饋資料**不存在 Grafana，也不是 Grafana 寫的**：值班人員在瀏覽器填表，瀏覽器直接把資料送給回饋網頁，由它寫進 PostgreSQL。Grafana 只畫表單、把填的內容送出去。

```
值班人員 ──瀏覽器──> engine api 的回饋網頁（Grafana 儀表板、通知的連結點進來；外掛版是 Grafana 裡的表單）
                        │ 1. 開表單：GET  /feedback?alert=…   （外掛版：GET /api/alert）
                        │ 2. 按送出：POST /feedback           （外掛版：POST /api/feedback）
                        ▼
                   engine 的 api（mode single 是同一隻 pod 的另一個 container；distributed 是 aiops-api）
                        │ 讀  aiops_alerts、aiops_anomalies
                        │ 寫  ↓
                        ▼
                   PostgreSQL 的三張表
```

| 一次送出寫到 | 寫幾筆 | 內容 |
|---|---|---|
| `aiops_feedback` | 這件告警的**每個異常各一筆**（最多 200 筆） | 真的／誤報／根因錯、誰填、怎麼看出來的、兩題評分、一句話與 LLM 抽取結果。每個異常一筆，因為 `feedback summary`、`suggest` 是依「序列加對象」算的 |
| `aiops_incidents` | 這件一筆（用 `alert_id` 當 id，更新或補建） | 狀態 `confirmed`（真的）、`corrected`（根因報錯）、`false_positive`；真的時附根因分類、根因、處置、怎麼看出來的、查了哪裡、下次怎麼更快 |
| `aiops_tuning` | 有填一句話、且 LLM 抽得到調整種類時才寫一筆 | `status: proposed`，要人核准才生效（[architecture.md 3.5](architecture.md#35-人在迴圈中系統只建議人決定-human-in-the-loop)） |

- **漏報**走另一條：網頁 `POST /missed`（外掛版 `POST /api/missed`），只寫 `aiops_feedback` 一筆（`verdict: missed`），沒有告警、不寫事故。
- **寫進去之後誰用**：`aiops_incidents` 的 `confirmed`、`corrected` 給相似事故檢索（自動開的 `auto` 事故不會拿來教人）；`aiops_feedback` 給 `feedback summary`、`feedback suggest`、`metrics`；`aiops_tuning` 的建議給 `adjust list`、回測、核准。
- **寫入用的是 engine 自己的資料庫帳號，不是使用者的**：所以沒有登入也寫得進去；`by` 是自己填的名字，空的記成 `anonymous`，同一個人可以填多次，系統不知道是誰。
- **抽取失敗不影響回饋本身**：LLM 掛了或格式不對，只存原文（`extract.ok: false`）；建議寫不進去也一樣，回應多一個 `tuning_error`。
- 程式在 [learn/feedback_service.py](../aiops-engine/src/learn/feedback_service.py) 的 `submit`、`submit_missed`（網頁與命令列共用同一個 service；HTTP 層在 [interface/web/routes.py](../aiops-engine/src/interface/web/routes.py)）；欄位對照見 [grafana/README.md](../grafana/README.md#回饋表單預設用-engine-api-的網頁要在-grafana-裡直接填才裝外掛)。

## Grafana 自己存什麼

| 項目 | 放哪 | Pod 重建後 |
|---|---|---|
| 資料來源、告警規則、通知對象、通知路由、儀表板（`aiops-rca`、`aiops_feedback`、`aiops-learning`） | repo 的 [grafana/helmchart/files/](../grafana/helmchart/files/)，部署時 provisioning 載入 | **回來**（在檔案裡） |
| 告警目前的狀態與歷史、使用者、在畫面上手動改的東西 | Grafana 內部 sqlite，**在容器裡，chart 沒掛 PVC** | **清掉** |
| 回饋表單外掛（Business Forms，選用；預設不裝） | `emptyDir` | 由 initContainer 重新下載安裝 |

所以改儀表板、告警規則要改 repo 的檔案，不要在畫面上改。repo 的 chart 是測試用；正式環境 Grafana 是否外接資料庫、告警歷史留多久，**這份沒有資料**（要問管 Grafana 的人）。

## engine 的暫存檔

排程器 pod 有一個 `emptyDir`，放心跳檔（`touch`）、台灣行事曆的快取。Pod 重建就沒了，兩者都能重新產生。

## 過期資料的壓縮

`detector prune` 對超過保留天數的資料**不直接刪**：一批一批讀出來，**一天打包成一筆 gzip**，寫進 PostgreSQL 的 `aiops_packed`，才刪原本的細筆（[common/infra/pack.py](../aiops-engine/src/common/infra/pack.py)）。不寫檔、不用資料夾、不用 PVC。讀回的步驟見 [deploy-how-to.md](deploy-how-to.md#過期資料的壓縮讀回)。

| 項目 | 現況 |
|---|---|
| 放哪 | `aiops_packed`：一筆 = 某張表某一天的一批。`doc` 是說明（`table`、`day`、`n` 幾筆），`data` 是 **bytea 欄**：gzip 的二進位內容（PostgreSQL 的「檔案」），解開是一行一筆的 `{id, ts, doc}`；`ts` = 那一天 |
| 大小 | 約壓成 1/8（本機實測 612 筆 trace 統計：jsonb 196 KB → 23.4 KB）；中型規模（trace 統計每天 72 萬筆）約 27 MB／天（推論，照實測比例算） |
| 留多久 | `pack.keep_days`（附的設定 365 天）：那一天超過就刪；`null` = 不刪。`pack: null` = 不打包、超過天數直接刪 |
| 安全 | 先寫 `aiops_packed`，才刪細筆（一次 `pack.batch` 筆，預設 20,000）。寫不進去（資料庫磁碟滿、權限）這一批不刪、指令回 1，排程標失敗、下一次再試。寫完當掉沒刪：下一次會再打包一次，讀回是同 id 覆蓋，不會重複 |
| 不在裡面的 | 事故、回饋、調整、對照覆寫（本來就不刪）；基準線（每天重算） |

## 本機日誌（寫進資料庫前的那一份）

engine 每一筆寫進 PostgreSQL 的動作（INSERT／UPDATE／DELETE），先記在本機的 PVC，再寫資料庫（[common/infra/journal.py](../aiops-engine/src/common/infra/journal.py)）。

| 項目 | 現況 |
|---|---|
| 放哪 | `AIOPS_JOURNAL_DIR`（helm 的 `journal.enabled` 建 PVC `aiops-journal`，掛在 `/var/lib/aiops/journal`；沒設 = 不記）。一個程式一個檔 `<資料庫代號>-<主機>-<pid>-<開始時間>.jsonl`，一行一個寫入動作 |
| 平常 | 寫進資料庫就清空，所以檔案幾乎都是空的；程式結束刪掉 |
| 資料庫寫不進去 | 那一筆留在檔案、工作照樣看到錯誤；同一個程式下一次寫入先補它（照原本的順序）；程式結束了還沒補完，排程器的看守每分鐘、別的程式第一次寫入前會認領、照順序補寫 |
| 本機寫不進去 | 照樣寫資料庫，只印一行警告（本機那一份是額外的保險，不能變成新的故障點） |
| 演練 | 本機停 PostgreSQL 150 秒，每 2 秒寫一筆：停的期間 21 筆寫入失敗，恢復後全部補寫，110 筆一筆不少（[pdca/meta/2026-11-pg-resilience.md](pdca/meta/2026-11-pg-resilience.md)） |

## 保留與備份的現況

| 項目 | 現況 |
|---|---|
| `aiops_feedback`、`aiops_incidents`、`aiops_tuning`、`aiops_mapping`（人工判斷） | 不自動刪；備份是公司 PostgreSQL 的備份（**DBA 決定頻率與保留**，申請時要講，見 [deploy-how-to.md](deploy-how-to.md#1b-postgresqlengine-自己的資料)）。以前在 ES 時的 snapshot（[kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 第 5 段）搬完就不用了 |
| `aiops_anomalies`、結案的 `aiops_alerts` | 排程 `prune` 每天 03:30 打包、刪超過 90／180 天的（[prune.yaml](../aiops-engine/configs/baseline/prune.yaml)，天數是估的）。異常刪了之後，事故的 `anomaly_ids` 對不到的那幾筆，回饋退回「一件一筆」，其他不受影響 |
| `aiops_baseline` | 每天重寫的衍生資料；資料庫還原後不用重 train。重建資料庫沒還原的話要手動跑一次 `train`，否則等隔天 02:00（期間基準線這一路停擺，Grafana 的 `aiops-baseline-missing` 會響） |

測試叢集 2026-11-01 的大小（當時在 ES：`aiops_alerts` 32 筆 1 MB、`aiops_anomalies` 157 筆 0.5 MB、`aiops_baseline` 1,512 筆 0.5 MB、`aiops_trace_segments` 3,431 筆 1.2 MB）只是量級參考，不代表正式環境。量最大的是 `aiops_anomalies`、`aiops-trace-segments`（ES）、`aiops_baseline`（依序列數 × 對象數），其他每天幾十到幾百筆。
