# engine 拆成 api／beat／worker 三種角色（2026-11）

**狀態**：已完成　　**日期**：2026-11-05　　**相關**：分支 `claude/api-beat-worker`（從 `claude/plugin-architecture` 開）
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
  - 做：`tasks/`（Celery app、三個佇列 `realtime`／`alert`／`batch`、`run_job` 任務）、`scheduler --enqueue`（beat 模式）、`feedback_web` 改名 `api` 並加三個端點（`/api/anomalies`、`/api/rca`、`/api/alerts`）、`alert` 支援 `rca_requested`、`schedule.yaml` 每個工作加 `queue`、helm 兩種 mode、Grafana 按鈕、文件。
  - 不做：`alert` 改成逐筆事件驛動（收斂邏輯是「一輪看所有新異常」，改逐筆要重驗併件、嚴重度、結束判斷）；多個 release 共用一個 ES 的索引前綴；API 的正式身分驗證（先用一把 token）；把 HTML 回饋頁拿掉（留著當 Grafana 外掛裝不起來時的備用）。

### 設計

| Superset | it-aiops | 怎麼做 |
|---|---|---|
| web | **`api`**（`python -m api`） | 原 `feedback_web` 改名，加 `POST /api/anomalies`（要 `Authorization: Bearer $AIOPS_API_TOKEN`）、`POST /api/rca`（Grafana 按鈕打，CORS 限 Grafana 網址）、`GET /api/alerts`；HTML 頁留著 |
| beat | **`scheduler --enqueue`** | 沿用 `scheduler/cron.py`、`load_schedule`、stage；每分鐘醒來把該跑的工作 `enqueue`，不開子行程。`--run`、`--list` 照舊 |
| worker | **`python -m tasks worker --queue <佇列>`** | 任務 `tasks.run_job(name, args, timeout, retries)`：開子行程 `python -m <args>`，逾時砍掉；就是今天 `scheduler.runner.run_once` |
| Redis | `redis` | chart 可以裝一個，或 `redis.url` 指公司現有的 |

**佇列**（`schedule.yaml` 每個工作加 `queue`；沒寫照 `runner.default_queue` 自動選）：

| 佇列 | 工作 | worker 設定 | 為什麼分開 |
|---|---|---|---|
| `realtime` | `rules`、`detect`、`trace-merge`、`ingest`、`peer`、`trace-collect`、`trace-detect`、`quality`、`paths-merge` | 可以多份、concurrency > 1 | 每幾分鐘的，寫入都是固定 `_id`、重跑覆蓋，同時跑兩份不會錯只是浪費 |
| `alert` | `alert`、API 丟進來的 `alert` | **1 份、concurrency 1**（chart 跟 `tasks worker` 都擋著） | 收斂要單一消費者，跟今天一樣安全 |
| `batch` | `train`、`trace-train`、`chronic`、`prune`、`shift` | 1 份、記憶體給大 | `train` 要 4Gi，跟每分鐘的分開，不再拖慢 `detect` |

**「上一次沒跑完就跳過」怎麼保留**：beat 丟工作時帶 `expires`＝下一次排定前 5 秒（`Cron.next_after`）；worker 忙到過期的工作直接丟掉，不會堆一排舊的 `detect` 追著跑。`worker_prefetch_multiplier=1`、`acks_late`：一次只拿一個、跑到一半被砍會回佇列。

**helm `mode`**：

| | `single`（預設，＝今天） | `distributed` |
|---|---|---|
| Deployment | 1 隻：`scheduler`＋`api` 兩個 container | `aiops-api`（N 份）、`aiops-beat`（1 份）、`aiops-worker-realtime`／`-alert`／`-batch`、`aiops-redis`（選） |
| 需要 Redis | 不用 | 要 |
| API 的 `/api/anomalies`、`/api/rca` | 能用（寫進 ES，下一分鐘排程接手） | 能用，而且立刻丟 `alert` 工作 |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `aiops-engine/tasks/__init__.py`、`__main__.py`（新） | Celery app（`AIOPS_BROKER_URL`，沒設就是 `configured()=False`）；任務 `run_job` 開子行程跑 `python -m <args>`（就是 `scheduler.runner.run_once`）；`enqueue(job, at)` 算 `expires`、丟進 `job.queue`；`trigger(name)` 照 plugin 的排程表找工作丟（api 用）；`worker_main` 起 threads pool 的 worker，`alert` 佇列 concurrency ≠ 1 直接拒絕 | 工作本身不改、只換觸發方式，detector／investigator 一行不動；`configured()` 讓同一份程式沒 Redis 時退回一隻 pod 的行為 |
| `scheduler/cron.py` | `frequent`（每幾分鐘的）、`next_after(t)`（下一次排定，一分鐘一分鐘比、最多 8 天） | 預設佇列要分「每幾分鐘」跟「每天／每週」；`expires` 要知道下一次是幾點 |
| `scheduler/runner.py` | `Job.queue`（排程表的 `queue`，沒寫 `default_queue`：alert→alert、frequent→realtime、其餘→batch；不是三個之一報錯）；`tick`／`serve` 多一個 `run` callback：給了就只呼叫它（beat），不開執行緒、不看有沒有在跑 | beat 跟排程器共用同一套 cron、stage、alive file；只是「跑」換成「丟」 |
| `scheduler/__main__.py` | `--enqueue`（沒設 broker 回 2）；`--list` 印 `queue`；多接 `SIGBREAK`（Windows 的 Ctrl-Break） | beat 就是排程器的另一個開法；本機 e2e 要能乾淨停它 |
| `scheduler/runner.py` 的 `serve` | 等下一分鐘改成一秒醒一次看 `stop` | Windows 的 `Event.wait` 不會被訊號打斷，一次等 60 秒會晚一分鐘才停（e2e 第二次跑發現）；Linux 本來就會被打斷，行為不變 |
| `configs/{schedule,apm,factory,metrics_only}/schedule.yaml` | 每個工作加 `queue`（31 個） | 寫明白，不靠預設規則 |
| `feedback_web/` → `api/`（`git mv`） | 改名；`__main__.py` 加 `POST /api/anomalies`（token、`A.ingest`、成功就 `tasks.trigger("alert")`）、`POST /api/rca`（`A.request_rca`、trigger）、`GET /api/alerts`；環境變數 `FEEDBACK_*` → `API_PORT`／`API_CORS_ORIGIN`／`API_TLS_*`；`app.py` 加 `ingest_config_path`（`AIOPS_INGEST_CONFIG` 或從 plugin 排程表的 `ingest` 工作找 `--config`）、`ingest`、`request_rca`（開著的件才標；結束的 ValueError→409）、`list_alerts` | 它已經不只是回饋網頁，是 engine 的 API；畫面在 Grafana，它只收請求 |
| `detector/cmd_ingest.py` | 抽出 `_prepare`（validate＋mapping）、`ingest_rows(es, cfg, name, rows, now)` | API 推進來的列跟排程 `ingest` 走同一套對照、同一個 `_id` 規則（重送不重複） |
| `investigator/cmd_alert.py` | `wants_rerun(al, now, acfg) → (要不要, 是不是人要求的)`：pop 掉 `rca_requested`／`rca_requested_at`（不寫回去）、人要求的不管 trace；人要求時跳過「trace 沒就緒不換報告」那條 | 重跑走新開件同一條路（相似事故、LLM、摘要、事故單），不另寫一條 RCA 路徑 |
| `Dockerfile`、`requirements.txt` | `COPY api/ tasks/`；`celery>=5.4`、`redis>=5.0` | |
| `helmchart/values.yaml` | `mode`；`scheduler.beatResources`；`feedback:` → `api:`（`replicas`、`token`）；`workers.{realtime,alert,batch}`；`redis.{enabled,image,url}` | 一份 values 兩種裝法 |
| `helmchart/templates/_helpers.tpl` | `aiops.mode`（只能 single／distributed）、`aiops.schedule`（原本在 deployment.yaml）、`aiops.brokerUrl`、`aiops.envFrom`、`aiops.apiContainer` | 兩個 mode 共用 api container、排程表、envFrom |
| `helmchart/templates/deployment.yaml` | 包在 `mode == single`；container `feedback` → `api`；Service `aiops-feedback` → `aiops-api` | |
| `helmchart/templates/distributed.yaml`（新） | `aiops-api`＋Service、`aiops-beat`（1 份、Recreate、`--enqueue`）、`range .Values.workers` 各一個 Deployment（key 不是三個之一 fail；`alert` 不是 1×1 fail；`alert` Recreate）、`aiops-redis`＋Service（`--save "" --appendonly no`） | |
| `helmchart/templates/env.yaml` | distributed 時 `AIOPS_BROKER_URL`；Secret `aiops-api`（`AIOPS_API_TOKEN`，空的也建） | |
| `helmchart/Chart.yaml`、`values-local.example.yaml` | 描述；`api.token` | |
| `grafana/helmchart/files/dashboards/aiops-feedback.json` | 第三個面板「重跑根因分析」：預填 `GET /api/alert?alert=${alert}`、按鈕 `POST /api/rca`、成功／失敗提示 | 畫面在 Grafana |
| `grafana/helmchart/values.yaml`、`templates/configmaps.yaml` | `feedbackUrl` 的註解改成「engine 的 api」 | |
| 測試：`tests/test_tasks.py`（新 12）、`tests/test_api_endpoints.py`（新 11）、`tests/test_deploy.py`（single 改名＋`DistributedChart` 8 個）、`tests/test_feedback_web*.py` → `test_api*.py`、`tests/distributed_e2e.py`（新） | 見 Check | |
| 文件：`aiops-engine/README.md`（部署模式、API 兩節）、`docs/architecture.md`（部署拓撲、4.3 部署角色）、`docs/deploy-how-to.md`、`docs/feedback/README.md`、`grafana/README.md`、`docs/data-storage-reference.md`、`docs/aiops-data-reference.md`、`configs/*/rca.yaml` 註解、`docs/README.md`、root `README.md`、`CLAUDE.md` | `feedback_web` → `api`、`FEEDBACK_*` → `API_*`、`feedback.corsOrigin` → `api.corsOrigin`、Service `aiops-api`；兩種 mode 怎麼選、三個端點 | |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 既有行為不變（排程器自己跑、回饋表單、收斂） | `python -m pytest -q` 全部 | 714 過（原 682＋新 32） |
| 工作進對的佇列、預設規則對 | `test_tasks.QueueOfJob`：規則表；四張排程表每個工作 `queue` 合法、`alert` 一定在 `alert`、每分鐘的在 `realtime` | 過；寫錯的 `queue: fast` 報錯 |
| `expires`＝下一次排定前 5 秒 | `NextRunAndExpiry`：每分鐘 → 55 秒、`*/5` → 4 分 55 秒、每天 02:00 → 隔天 01:59:55、週日 → 週一；2 月 31 日永遠沒有 → None | 過 |
| beat 只丟不跑；沒 broker 不丟 | `Enqueue`：`tick(run=cb)` 不開執行緒、不符合 cron 的不丟；`enqueue` 的 `apply_async` 帶 `queue`、`expires`、`kwargs`；`AIOPS_BROKER_URL` 空 → `enqueue`／`trigger` 回 False、`apply_async` 沒被叫 | 過 |
| api 的 `trigger("alert")` 照 plugin 排程表找工作 | mock `apply_async`：進 `alert` 佇列、`expires` 300、args 是 `investigator alert …`；沒這個工作回 False | 過 |
| worker 的任務就是跑子行程 | `run_job` 跑 `tests._run_code`：`pass` → True、`SystemExit(3)` → False 且 log 有 `失敗 rc=3` | 過 |
| `alert` 佇列擋住 concurrency ≠ 1 | `worker_main(["--queue","alert","-c","2"])` 回 2、不起 worker | 過 |
| `POST /api/anomalies` | `test_api_endpoints.Http`：沒 token／錯 token → 401、沒設 token → 401 說明；對的 token → 200、`written 1 skipped 1`、文件 `keys.equipment`＝`ETCH-01`（照 `configs/factory/ingest.yaml`）、`level`／`source`／`source_id` 對、mapping 有補、`apply_async` 進 `alert`；body 不對 → 400；`input` 不存在 → 404 列出有的；`AIOPS_PLUGIN=observability` → 404「沒有 ingest」；沒 broker → 200 但 `alert_queued false` | 過 |
| `POST /api/rca` | 開著的件 → 200 `queued`、ES `_update` 寫 `rca_requested`、`apply_async` 進 `alert`；不存在 → 404；結束的 → 409 且沒寫；OPTIONS 帶 Grafana Origin → 204 CORS header | 過 |
| `alert` 認 `rca_requested` | `wants_rerun`：有標記、自動重試關了、次數用完 → `(True, True)` 且標記被 pop；沒標記照 `needs_rerun`；結束的件或 `rca: false` → 不跑但標記還是拿掉 | 過 |
| `GET /api/alerts` | open 預設只回開著的、不含 `report`；`status=all`；`status=weird` → 400 | 過 |
| chart 兩種 mode | `test_deploy.EngineChart`（single：`scheduler`＋`api`、Secret ×2、`api.enabled=false` 只剩 scheduler、沒 `AIOPS_BROKER_URL`、`mode=weird` fail）、`DistributedChart`（6 個 Deployment；beat `--enqueue`、1 份 Recreate、256Mi；三個 worker 的 args、batch 6Gi、alert Recreate、realtime replicas 可調；`workers.alert.replicas=2`／`concurrency=2`／`workers.fast` 都 fail；`redis.enabled=false`＋`url` → 沒 redis Deployment、`AIOPS_BROKER_URL` 用那個 url；`plugin=factory` beat 讀 factory 排程表；`configOverrides` 五個角色都掛到） | 過；`helm template` 兩種 mode × 三個 plugin 都 render |
| 分散模式真的跑得起來（本機） | `python -X utf8 -m tests.distributed_e2e`：測試 cluster 跑一個 `redis:7-alpine`（port-forward 16379）、測試 ES；本機起 beat（factory 排程表、stage 2、`--enqueue`）、`realtime`（c=2）／`alert`／`batch` 三個 worker、api；跑約 4 分鐘 | **21 項：第一次 20 過 1 敗**（下一列）；beat 兩輪都把 `ingest`、`paths-merge` 丟進 `realtime`、`alert` 丟進 `alert`，自己沒跑；realtime worker 跑了 `ingest`（`[scheduler] ingest … ok`）、alert worker 跑了 `alert`，batch worker 活著沒事做，沒有跨佇列；佇列長度 realtime／alert／batch 都 ≤ 1（沒堆積）；`POST /api/anomalies` 沒 token 401、有 token 200 寫 1 筆、`aiops-anomalies` 有 `level ml`；**3 秒後** `aiops-alerts` 就開了 `SMOKE-01` 的件（有報告、沒 `rca_requested`）；`GET /api/alerts` 看得到；`POST /api/rca` 200 → 件的 `rca_attempts` +1、`report_at` 換新、標記拿掉、alert worker 的 log 有「人要求重跑根因分析」；不存在的件 404 |
| beat 收到停止乾淨結束 | 同上第 5 段：對 beat 送 `CTRL_BREAK_EVENT`（Windows；K8s 是 SIGTERM）、等它結束、log 要有「結束」 | 第一次 **FAIL**：Windows 的 Ctrl-Break 是 `SIGBREAK`，排程器只接 `SIGTERM`、`SIGINT`，30 秒沒停被 kill。加 `SIGBREAK` 後第二次還是 **FAIL**：handler 有接到，但主執行緒卡在 `stop.wait(到下一分鐘)`，Windows 的 `Event.wait` 不會被訊號打斷，要等到下一分鐘才看到 `stop`。`serve` 改成一秒醒一次後第三次：見下 |
| 文件 | `python tools/check-docs.py` | 連結、索引、舊說法、類型 0 問題 |

- **第二次 `distributed_e2e`**（加 `SIGBREAK` 後）：21 項 17 過 3 敗，三個都是測試本身或 Windows 的問題，不是功能：(1) 「paths-merge → realtime」——`paths-merge` 每 5 分鐘，等 150 秒不一定碰到 :x5，第一次剛好碰到；改成有丟才檢查。(2) 「alert worker 的 log 有『人要求重跑』」——ES 已經看到 `rca_attempts +1`，但子行程的輸出要等它結束才 flush 進 log，一次性檢查太早；改成等 30 秒。(3) 停 beat：上一列。功能面 17 項（丟對佇列、worker 跑、不堆積、推異常 3 秒開件、重跑根因、404）跟第一次一樣過。
- **第三次 `distributed_e2e`**（`serve` 一秒醒一次、e2e 兩處改成等）：**20 項全過**（paths-merge 那項併進佇列檢查，所以 21 → 20）。推異常到開件 2 秒；beat 收到 Ctrl-Break 在 1 秒內停、log 有「結束」。單元測試 714 過。
- **helm 實裝進測試 cluster（rancher-desktop，namespace `aiops`，`mode=distributed`、`plugin=factory`，image 用 `nerdctl --namespace k8s.io build`）**：第一次 6 個 Deployment 有 3 個 CrashLoop——`kombu/transport/redis.py` 要 `packaging`，本機剛好有、`python:3.13-slim` 沒有；`requirements.txt` 補 `packaging>=23` 重建後 6 個全 Running、零重啟。beat 把 `ingest`、`alert` 丟進 `realtime`／`alert`，兩個 worker 各自 `[scheduler] … ok 1.0s`；port-forward 進 `aiops-api`：`/health` ok、沒 token 401、有 token 推 1 筆 `written 1 alert_queued true`，數秒後 `/api/alerts` 看得到 `a-20261105-1128-K8S-01`。沒測：`single` mode 沒裝、`workers.*.replicas>1`、滾動更新。
- **Grafana 實測（新 namespace `grafana-new`，用 `grafana/helmchart` 裝、Business Forms 6.3.1、Grafana 13.0.7；瀏覽器打 port-forward 的 `aiops-api`）**：三個面板都載入，回饋表單從叢集裡的 `aiops-api` 預填了 `a-20261105-1128-K8S-01`。發現三個問題：(1) **`policies.yaml` 的 `weekdays: ["monday:sunday"]` 在 Grafana 13.0.7 讓整個 Grafana 起不來**（`start day cannot be before end day`，CrashLoop）——嚴重度分級那次加進去的，舊的 demo pod 是更早起的所以沒發現；改成 `["sunday:saturday"]`。(2) 重跑按鈕一直灰的：外掛的 `updateEnabled` 只有 `auto`（有改動才亮）、`manual`、`disabled`，沒有 `always`；面板上唯一的欄位是唯讀的，永遠沒有「改動」。加一個「原因」欄位，要填才能按。(3) 新欄位的標題被擠到跟按鈕重疊：面板加高、標題縮短、`labelWidth` 12。修完後按鈕：`OPTIONS /api/rca` 204（CORS 通）→ `POST /api/rca` 200 → alert worker 的 log「人要求重跑根因分析（第 4 次）」。
- **沒測到的**：Redis 掉線時 beat／worker 的重連（Celery `broker_connection_retry_on_startup` 設了，沒拔線試）；`expires` 真的丟掉過期工作（佇列沒堆積是間接證據；沒刻意把 worker 卡住看它丟）；多個 realtime worker 同時跑同一個 `detect` 的寫入（設計上固定 `_id` 覆蓋，沒實測）；真的 LLM；Grafana 按鈕按成功後的提示訊息（截圖時已消失）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `POST /api/rca` 沒有身分驗證（只有 CORS） | 設計時：按鈕在 Grafana 的網頁裡，token 放進儀表板 json 等於給所有看得到儀表板的人 | 知道網址的人可以讓任一開著的件重跑根因（只是重算，不會壞資料、不會通知） | 要鎖再加：Ingress 層驗證，或 Grafana 的 service account 代打 |
| `POST /api/anomalies` 的 token 是一把共用的 | 範圍 | ML、FDC 系統共用；換 token 要兩邊一起換 | 多個來源各一把、或改 mTLS，等真的要接再做 |
| `expires` 跟 beat 的時鐘綁在一起 | 設計時 | beat 跟 worker 時鐘差太多（> 5 秒）會把還沒過期的當過期丟掉 | K8s 節點都走 NTP，先不管；要的話 `expires` 改成「下一次排定前 30 秒」 |
| 重跑根因只能對開著的件 | `request_rca` 讀 `_open_alerts` 只拿 `status: open` | 結束的件按了回 409；要看舊報告只能 `/report` | 真的需要時另開「對結束的件重算、不改狀態」的路 |
| [⑨] Grafana 回饋表單的分類選項仍是 IT 的 | 之前就有（plugin-architecture 的 Act） | 工廠裝了也是 IT 的選項 | 另案 |
| `ingest.yaml` 的 `inputs` 名字（`ml`、`fdc`）就是 API 的 `input` | 設計時 | ML 系統要知道自己叫 `ml`；改名要兩邊一起 | 文件寫明；欄位名本來就是假設，拿到樣本一起定 |

- **決定**：採用 Celery＋Redis（跟 Superset 同一套，維運熟；重試、逾時、`expires`、單一消費者都現成），不自己拿 ES 當佇列。`alert` 不改逐筆事件驛動：收斂是「一輪看所有新異常」，單一消費者就夠；事件驛動只放在「外部推異常」「人按重跑」兩處。HTML 回饋頁留著當備用。
- **下一輪**：`mode=distributed` 在測試 cluster 跑一天（已裝起來、驗過一輪）；拿到 ML／FDC 樣本後定 `POST /api/anomalies` 的欄位；Grafana 裡實際按一次「重跑根因分析」。
