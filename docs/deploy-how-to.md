# 部署：公司已有 Elastic APM、ES、PostgreSQL，只裝 AIOps

> **類型：操作 How-To**　原理見 [architecture.md](architecture.md)・APM 的欄位與能做到什麼見 [aiops-engine/configs/apm/README.md](../aiops-engine/configs/apm/README.md)

收值（APM agent、apm-server）、Elasticsearch、PostgreSQL 都是公司現有的，**不用部署**。要做的：在公司 ES 上手動補一次設定、跟 DBA 申請 PostgreSQL、部署 aiops-engine、（要發通知時）接 Grafana。

| 順序 | 做什麼 | 誰做 | 必要 |
|---|---|---|---|
| 0 | APM agent 的三個設定 | 應用團隊、公司 APM 團隊 | ✅ |
| 1 | 在公司 ES 補設定：`traces-merged` 的 ILM 與 template、engine 的角色與 key | 有 ES 管理權限的人（Kibana Dev Tools） | ✅ |
| 1b | 申請 PostgreSQL：一個資料庫、engine 與 Grafana 各一個帳號（engine 自己的資料） | DBA | ✅ |
| 2 | 部署 aiops-engine | 我們 | ✅ |
| 3 | 接 Grafana（通知、儀表板、回饋表單） | 我們 | 要發通知時 |

## 0. APM agent 的設定

| 設定 | 為什麼 | 沒做的話 |
|---|---|---|
| transaction sample rate **1.0**（不取樣） | trace-merge 要拿到一個請求的每一段 | 根因分析的 trace 不完整 |
| 全域標籤 `labels.dept`、`labels.group` | 告警依部門分送 | 全部歸 `unassigned`，送平台 |
| 開 log shipping（log 帶 `trace.id`） | 根因分析撈同一個請求在各服務的 log | 只看得到 `logs-apm.error-*` 的例外 |

## 1. 在公司 ES 補設定（Kibana → Dev Tools）

整份貼 [elasticsearch/kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt)，從上往下一段一段執行。全部是新增，不碰 APM 的任何索引。

| 段 | 做什麼 | 沒做的話 |
|---|---|---|
| 1 | `traces-merged` 的保留天數（ILM，7 天） | `traces-merged` 只增不減 |
| 2 | `traces-merged` 的 index template（`spans`、`entries` 是 nested） | 根因分析查詢失敗 |
| 3 | engine 的 API key：讀 APM 的三種資料，只能寫 `aiops-*`、`traces-merged-*` | engine 讀不到資料或寫不進去 |
| 4 | Grafana 的唯讀 key（第 3 節要發通知時才用） | Grafana 讀不到 `aiops-*` |
| 5 | 選用：每天 snapshot 備份 | 回饋、事故紀錄掉了不能重算 |

兩把 key 回傳的 `encoded` 只出現一次；不要貼進 git、聊天或 log。engine 的那把下一步用。

確認：

| 要確認的 | 怎麼看 |
|---|---|
| template 在 | `GET _index_template/traces-merged` |
| ILM 在 | `GET _ilm/policy/traces-merged` |
| key 讀得到 APM | 用這把 key 打 `GET metrics-apm.transaction.1m-*/_count`，大於 0 |

## 1b. PostgreSQL（engine 自己的資料）

engine 自己的資料——異常、基準線、告警（含 RCA 報告）、事故、回饋、調整、欄位對照的覆寫——存在 PostgreSQL，一種資料一張表（[data-storage-reference.md](data-storage-reference.md)；為什麼從 ES 搬過來見 [PDCA 紀錄](pdca/meta/2026-11-postgres-store.md)）。原始資料與 `traces-merged-*` 還在 ES。

**跟 DBA 申請的東西**

| 項目 | 內容 |
|---|---|
| 版本 | PostgreSQL **14 以上**；只用標準功能（jsonb、GIN、btree），不用 TimescaleDB |
| 資料庫 | 一個（例如 `aiops`）；要放在某個 schema 而不是 `public` 也可以，engine 設 `pgSchema`（環境變數 `AIOPS_PG_SCHEMA`） |
| 建表的帳號 | 部署時跑一次 `python3 -m main detector db init`（建全部的表與索引）：要那個 schema 的 `CREATE`。可以是 DBA 自己、或部署用的另一個帳號；之後改版加了索引再跑一次 |
| engine 的帳號 | 表的 `SELECT／INSERT／UPDATE／DELETE` 就夠（**不用 `CREATE`**）：engine 先用 `to_regclass` 查表在不在，在就不送 DDL；表還沒建時才會試著建，那時沒有 `CREATE` 會 `permission denied`，請先跑 `db init`（2026-11-07 前的版本每次都送 `CREATE TABLE IF NOT EXISTS`，一定要 `CREATE`） |
| Grafana 的帳號 | **唯讀**：`SELECT`，加 `ALTER DEFAULT PRIVILEGES … GRANT SELECT ON TABLES`（engine 之後建的表也讀得到） |
| 擴充（選用） | `pgvector`：相似事故的向量用 SQL 算。**不給也能跑**：向量存一般欄位、在程式裡算（事故幾十到幾百件，沒差） |
| 連線 | engine、Grafana 的 pod 連得到；`sslmode=require` 以上 |
| 高可用 | 問 DBA 有沒有**主從自動切換**（雲端託管的 Multi-AZ、Patroni＋HAProxy…）與切換要多久。engine 連不上會等 1、2、4、8… 秒重試、最多 `AIOPS_PG_RETRY_SECONDS`（預設 30 秒），切換在這之內就沒感覺。連線位址：切換後位址不變（VIP、HAProxy）最好；不行的話 DSN 寫多台、只連主庫：`postgresql://帳號:密碼@pg1:5432,pg2:5432/aiops?target_session_attrs=read-write` |
| 唯讀副本（選用） | 有的話 **Grafana 連副本**（`grafana/` 的 `AIOPS_PG_HOST`），engine 連主庫：Grafana 查大範圍歷史不會拖慢 engine 的寫入 |
| engine 的連線設定 | 都在 `aiops-engine/src/common/config/settings.py`（環境變數）：`AIOPS_PG_DSN`（必填）、`AIOPS_PG_SCHEMA`、`AIOPS_PG_CONNECT_TIMEOUT`（預設 10 秒）、`AIOPS_PG_STATEMENT_TIMEOUT`（一個查詢最多幾秒，預設 120）、`AIOPS_PG_APP_NAME`（DBA 在 `pg_stat_activity` 看到的名字，預設 `aiops-engine`） |
| 備份 | 事故、回饋、調整是人判斷的，**掉了不能重算**：請 DBA 照一般資料庫備份；異常、告警、基準線由 `detector prune` 自己清舊的（[prune.yaml](../aiops-engine/configs/baseline/prune.yaml)） |
| 大小 | 量最大的是異常、trace 每段的統計、基準線（依序列數 × 對象數，90 天）；其他每天幾十到幾百筆。上線前用正式環境的量估 |

Grafana 唯讀帳號（DBA 執行，`<engine 帳號>` 換成 engine 用的）：

```sql
CREATE ROLE grafana_aiops LOGIN PASSWORD '<密碼>';
GRANT CONNECT ON DATABASE aiops TO grafana_aiops;
GRANT USAGE ON SCHEMA public TO grafana_aiops;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_aiops;
ALTER DEFAULT PRIVILEGES FOR ROLE <engine 帳號> IN SCHEMA public GRANT SELECT ON TABLES TO grafana_aiops;
```

**以前存在 ES 的資料**：已經跑過一陣子、`aiops-*` 索引裡有資料的環境，部署好 engine（第 2 節）之後搬一次：

```bash
kubectl -n aiops exec deploy/aiops-engine -c scheduler -- python3 -m main detector db import-es --dry-run   # 先數 ES 裡有幾筆
kubectl -n aiops exec deploy/aiops-engine -c scheduler -- python3 -m main detector db import-es             # 搬（同 id 覆蓋，可以重跑）
kubectl -n aiops exec deploy/aiops-engine -c scheduler -- python3 -m main detector db status                # 每張表幾筆，跟上面對
```

對過筆數、Grafana 改讀 PostgreSQL 也正常之後，才刪 ES 的舊索引（[kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 第 6 段）。

## 2. 部署 aiops-engine

```bash
cd aiops-engine && docker build -t <nexus>/aiops-engine:<tag> . && docker push <nexus>/aiops-engine:<tag>   # 內網：Dockerfile 的 pip 指到 Nexus
```

```bash
kubectl create namespace aiops
```

```bash
kubectl -n aiops create secret generic aiops-secrets --from-literal=ES_API_KEY=<1-4 的 encoded> \
  --from-literal=AIOPS_PG_DSN='postgresql://<engine 帳號>:<密碼>@<host>:5432/aiops?sslmode=require'
```

```bash
cp aiops-engine/helmchart/values-local.example.yaml aiops-engine/helmchart/values-local.yaml   # 填 LLM 的 provider、model、baseUrl、apiKey；不進 git
```

```bash
helm upgrade --install aiops aiops-engine/helmchart -n aiops -f aiops-engine/helmchart/values-local.yaml \
  --set source=apm --set image.repository=<nexus>/aiops-engine --set image.tag=<tag> --set esUrl=https://<公司 ES>:9200
```

- **`--set source=apm` 一定要帶**：預設是 `otel`（讀 Collector 寫的格式），不帶就讀不到 APM 的資料。
- 排程器預設 **stage 1**：rules、detect、alert、trace-merge、train。跑穩再 `--set scheduler.stage=2` 開 Pod 同儕比對、trace 每一段的基準線。
- 預設 `mode: single`（一隻 pod）。要 api 跟排程分開、`train` 不拖慢每分鐘的工作、或 ML／FDC 要即時推異常：`--set mode=distributed`（api、beat、worker 依佇列、redis；[aiops-engine/README.md「部署模式」](../aiops-engine/README.md#部署模式一隻-pod或-apibeatworker)）。
- 回饋網頁的網址要放進通知時：`configs/apm/rca.yaml` 的 `alerts.feedback_url`、`report_url`，用 `--set-file configOverrides.apm.rca=<改好的 rca.yaml>` 蓋掉。

確認：

| 要確認的 | 怎麼看 |
|---|---|
| Pod 起來 | `kubectl -n aiops get pod`：`aiops-engine-…` 2/2（distributed：`aiops-api`、`aiops-beat`、三個 `aiops-worker-*`、`aiops-redis` 都 Running） |
| 排程在跑 | `kubectl -n aiops logs deploy/aiops-engine -c scheduler -f`：每分鐘有 rules、detect、alert、trace-merge（distributed：`deploy/aiops-beat` 每分鐘 `[beat] … → 佇列`，`deploy/aiops-worker-realtime` 有 `[scheduler] detect … ok`） |
| 讀得到資料 | Kibana 看 `traces-merged-*` 開始有資料；`detector db status` 的 `aiops_alerts` 有筆數（`heartbeat` 那筆） |
| 基準線 | 每天 02:00 train；資料要累積 2 週才有「同星期幾 × 同小時」，之前只有靜態臨界值規則在報 |

## 3. 接 Grafana（要發通知時）

engine 只把告警寫進 PostgreSQL 的 `aiops_alerts`，**不自己發通知**。先只寫入、不通知跑 2～4 週，期間看 Grafana 的儀表板（或 SQL 查 `aiops_alerts`）；要通知時再接 Grafana。細節見 [grafana/README.md](../grafana/README.md)。

**3-1. 準備的東西**

| 項目 | 內容 | 哪來 |
|---|---|---|
| Grafana 的 PostgreSQL 唯讀帳號 | 讀 engine 的表（告警、異常、調整、回饋）：`AIOPS_PG_HOST`、`AIOPS_PG_DATABASE`、`AIOPS_PG_SSLMODE`、`GRAFANA_PG_USER`、`GRAFANA_PG_PASSWORD` | 第 1b 節 |
| Grafana 的 ES 唯讀 key（整合儀表板的上半） | 讀 APM 的三種原始資料，加 cluster `monitor`（Grafana 會先打 `GET /` 看版本） | [kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 第 4 段 |
| 各部門的通知管道 | `NOTIFY_IT_URL`、`NOTIFY_IMC_URL`、`NOTIFY_PLATFORM_URL`（範例是 webhook；Teams、Email 改 `contact-points.yaml`） | 各部門、平台團隊 |
| dept 清單 | `policies.yaml` 的 dept 要跟 APM agent 的 `labels.dept` 值一樣 | 第 0 節的標籤 |
| 回饋表單外掛（選用） | 預設不用：回饋走 engine api 的網頁。要在 Grafana 裡直接填才裝 Business Forms（`volkovlabs-form-panel`）**6.3.1**；Grafana 12.2、13.0.7 實測過 | Nexus |
| 回饋網頁的網址 | 使用者**瀏覽器**連得到的 engine 回饋網頁（Service `aiops-api:8080` 前面加 Ingress） | 我們 |

**3-2. 送進 Grafana**：內容都在 `grafana/helmchart/files/`（data source、3 條告警規則、contact point、policy、3 個儀表板），依公司有沒有 Grafana 選一種：

| 情況 | 怎麼做 |
|---|---|
| 公司已有 Grafana | `grafana/helmchart/files/` 的檔（`dashboards/forms/` 除外，那是外掛版）交給管 Grafana 的人放進 provisioning 資料夾；Grafana 要有環境變數 `ES_URL`、`GRAFANA_ES_API_KEY`、`AIOPS_PG_*`、`GRAFANA_PG_*`、`NOTIFY_*_URL` |
| 沒有 Grafana | 用 `grafana/helmchart/` 裝一台（檔都在 chart 裡，只要 Secret `grafana-env` + `helm upgrade`，指令在 [grafana/README.md「部署」](../grafana/README.md#部署)） |

**3-3. 跟 engine 接起來的三個網址**

| 改哪裡 | 改成 |
|---|---|
| 儀表板 `aiops-feedback.json` 裡寫死的 `http://localhost:18080` | 用我們的 chart：`--set feedbackUrl=<回饋網頁的 Ingress 網址>`；其他方式：改檔 |
| engine 部署加 `--set api.corsOrigin=https://<Grafana 網址>` | 表單、「重跑根因分析」按鈕從瀏覽器直接打 api，要開 CORS |
| `alert-rules.yaml` 的 `feedback` annotation、engine `configs/apm/rca.yaml` 的 `alerts.feedback_url`、`report_url` | Grafana 與回饋網頁的網址（通知裡的連結） |

確認：Grafana 的 AIOps 資料夾有 `aiops-rca`、`aiops-learning`、`aiops_feedback` 三個儀表板；Alerting 有 `aiops-incident`、`aiops-heartbeat`、`aiops-baseline-missing` 三條規則；data source 測試通過。

## 過期資料的壓縮讀回

`detector prune` 對超過保留天數的資料，先一天打包成一筆 gzip（bytea 欄）放進 PostgreSQL 的 `aiops_packed`，才刪細筆（放哪、大小、留多久見 [data-storage-reference.md](data-storage-reference.md#過期資料的壓縮)）。不用 PVC、不用資料夾。要查舊的：

```bash
# 有哪些天、各幾筆
psql "$AIOPS_PG_DSN" -c "select doc->>'table' t, doc->>'day' d, doc->>'n' n from aiops_packed order by 1,2"
# 讀回原表（同 id 覆蓋，可以重跑）：--table 只讀某張表，--from／--to 限日期（含），--dry-run 只數
kubectl -n aiops exec deploy/aiops-engine -c scheduler -- python3 -m main detector db restore --table aiops_anomalies --from 2026-07-01 --to 2026-07-31 --dry-run
kubectl -n aiops exec deploy/aiops-engine -c scheduler -- python3 -m main detector db restore --table aiops_anomalies --from 2026-07-01 --to 2026-07-31
```

- distributed 把 `deploy/aiops-engine -c scheduler` 換成 `deploy/aiops-worker-batch`。
- 讀回來的資料還是比保留天數舊，**下一次 prune（每天 03:30）會再打包、刪掉**。要查久一點，先把排程的 prune 拿掉。
- 不想打包：`prune.yaml` 寫 `pack: null`，超過天數直接刪。

## 可以晚點做的

| 項目 | 什麼時候需要 | 怎麼做 |
|---|---|---|
| snapshot 備份 | 有老師傅回饋、事故紀錄之後（掉了不能重算） | [kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 第 5 段 |
| 開通知 | 只寫入跑 2～4 週、誤報率可接受之後 | 第 3 節 |
| stage 2 | stage 1 跑穩之後 | `--set scheduler.stage=2` |
