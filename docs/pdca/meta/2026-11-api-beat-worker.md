# engine 拆成 api／beat／worker 三種角色（2026-11）

**狀態**：進行中　　**日期**：2026-11-05　　**相關**：分支 `claude/api-beat-worker`（從 `claude/plugin-architecture` 開）
**模組**：⑤⑥⑦⑧⑨（部署方式；④ 的演算法不動）

## Plan：這一次要達到什麼

- **需求**：engine 現在是一隻 pod 兩個 container（`scheduler` 自己每分鐘開子行程跑 13 個工作、`feedback_web` 回饋網頁）。全部擠在一起：不能單獨擴某個工作、`train` 吃資源會拖慢每分鐘的 `detect`、排程器只能 1 份、網頁跟排程一起掛。工廠要讓 ML／FDC **即時推異常進來**、值班要能在 Grafana 上**按一下重跑根因分析**，現在都只能等排程去撈。使用者要的是 Superset 那種結構：**一個 image、一份 values.yaml，用不同啟動指令分成 web／beat／worker 幾種角色，用佇列串起來；畫面全部交給 Grafana。**
- **目的**：
  - 一份 values.yaml 選 `mode: distributed` 就裝出 `api`（沒有頁面的薄 API）、`beat`（只丟工作、自己不跑，1 份）、`workers.<佇列>`（真正跑，各自 replicas／resources）、`redis`（佇列）；`mode: single` 還是今天那一隻 pod（開發、離線廠區沒 Redis 時用）。
  - 所有 detector／investigator 子命令**不改一行**就能在 worker 跑：worker 的工作就是「開子行程跑 `python -m <args>`」，跟今天的排程器一樣，只是觸發從「排程器自己跑」變成「從佇列拿」。
  - `POST /api/anomalies`：ML／FDC 把判好的異常推進 ⑤（走 `detector/ingest.py` 同一套轉換），推完立刻丟一個 `alert` 工作，幾秒內開件；不用等每分鐘的 `ingest`。
  - `POST /api/rca {alert_id}`：在開著的那件上標 `rca_requested`，丟一個 `alert` 工作，下一輪 `alert` 強制重跑根因分析（含相似事故、LLM、摘要、事故單，跟新開件走同一條路）。Grafana 回饋儀表板加一個「重跑根因分析」按鈕打它。
  - 收斂（`alert`）仍然**同時只有一個在跑**：`alert` 佇列固定 1 個 worker、concurrency 1，不會兩筆異常同時進來各開一件。
- **範圍**：
  - 做：`tasks/`（Celery app、三個佇列 `realtime`／`alert`／`batch`、`run_job` 任務）、`scheduler --enqueue`（beat 模式）、`feedback_web` 改名 `api` 並加三個端點（`/api/anomalies`、`/api/rca`、`/api/alerts`）、`alert` 支援 `rca_requested`、`schedule.yaml` 每個工作加 `queue`、helm 兩種 mode、Grafana 按鈕、文件、PDCA。
  - 不做：`alert` 改成逐筆事件驛動（收斂邏輯是「一輪看所有新異常」，改逐筆要重驗併件、嚴重度、結束判斷）；多個 release 共用一個 ES 的索引前綴；API 的正式身分驗證（先用一把 token）；把 HTML 回饋頁拿掉（留著當 Grafana 外掛裝不起來時的備用）。

### 設計

| Superset | it-aiops | 怎麼做 |
|---|---|---|
| web | **`api`**（`python -m api`） | 原 `feedback_web` 改名，加 `POST /api/anomalies`（要 `Authorization: Bearer $AIOPS_API_TOKEN`）、`POST /api/rca`（Grafana 按鈕打，CORS 限 Grafana 網址）、`GET /api/alerts`；HTML 頁留著 |
| beat | **`scheduler --enqueue`** | 沿用 `scheduler/cron.py`、`load_schedule`、stage；每分鐘醒來把該跑的工作 `enqueue`，不開子行程。`--run`、`--list` 照舊 |
| worker | **`celery -A tasks worker -Q <佇列>`** | 任務 `tasks.run_job(name, args, timeout, retries)`：開子行程 `python -m <args>`，逾時砍掉；就是今天 `scheduler.runner.run_once` |
| Redis | `redis` | chart 可以裝一個，或 `redis.url` 指公司現有的 |

**佇列**（`schedule.yaml` 每個工作加 `queue`）：

| 佇列 | 工作 | worker 設定 | 為什麼分開 |
|---|---|---|---|
| `realtime` | `rules`、`detect`、`trace-merge`、`ingest`、`peer`、`trace-collect`、`trace-detect`、`quality`、`paths-merge` | 可以多份、concurrency > 1 | 每幾分鐘的，寫入都是固定 `_id`、重跑覆蓋，同時跑兩份不會錯只是浪費 |
| `alert` | `alert`、API 丟進來的 `alert` | **1 份、concurrency 1** | 收斂要單一消費者，跟今天一樣安全 |
| `batch` | `train`、`trace-train`、`chronic`、`prune`、`shift` | 1 份、記憶體給大 | `train` 要 4Gi，跟每分鐘的分開，不再拖慢 `detect` |

**「上一次沒跑完就跳過」怎麼保留**：beat 丟工作時帶 `expires`＝下一次排定前（每分鐘的 55 秒、每 5 分鐘的 4 分 55 秒…）；worker 忙到過期的工作直接丟掉，不會堆一排舊的 `detect` 追著跑。

**helm `mode`**：

| | `single`（預設，＝今天） | `distributed` |
|---|---|---|
| Deployment | 1 隻：`scheduler`＋`api` 兩個 container | `aiops-api`（N 份）、`aiops-beat`（1 份）、`aiops-worker-realtime`／`-alert`／`-batch`、`aiops-redis`（選） |
| 需要 Redis | 不用 | 要 |
| API 的 `/api/anomalies`、`/api/rca` | 能用（寫進 ES，下一分鐘排程接手） | 能用，而且立刻丟 `alert` 工作 |

### 步驟

1. 開 branch、這份 Plan、commit。
2. `tasks/`：Celery app（`AIOPS_BROKER_URL`）、`run_job`、`enqueue(job, at)` 算 `expires`；`scheduler --enqueue`；`schedule.yaml` ×4 加 `queue`。單元測試：佇列預設規則、expires、`run_job` 跑子行程、beat 模式只丟不跑。
3. `feedback_web` → `api`：三個端點、token、`rca_requested`；`cmd_alert.run` 認 `rca_requested`（強制重跑、跳過「trace 沒就緒不換報告」）。單元測試用 FakeES。
4. helm：`mode`、`api`／`beat`／`workers`／`redis`；`helm template` 兩種 mode × 三個 plugin。Dockerfile、requirements 加 celery、redis。
5. Grafana：`aiops-feedback.json` 加「重跑根因分析」面板；chart 的 `feedbackUrl` 註解改成 api。
6. 煙霧測試：測試 cluster 跑一個 Redis，本機起 beat＋三個 worker 對測試 ES 跑幾分鐘，看工作有被丟、被跑、過期的被丟掉；`POST /api/anomalies` → 幾秒內 `aiops-alerts` 有件；`POST /api/rca` → 報告重寫。
7. 文件：`aiops-engine/README.md`、`docs/architecture.md`（部署拓撲、4.3 角色）、`docs/deploy-how-to.md`、`docs/feedback/README.md`、`grafana/README.md`、`docs/README.md`、`CLAUDE.md`、root README；PDCA Do／Check／Act；`check-docs.py`、`pytest`；commit、push，不開 PR、不合 master。

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
