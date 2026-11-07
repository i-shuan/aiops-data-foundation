# engine 自己的資料從 ES 搬到 PostgreSQL，相似事故加向量檢索（2026-11）

**狀態**：已完成（本機 PostgreSQL 16＋ES＋Grafana 13 驗過；公司的 PostgreSQL 還沒申請）　　**日期**：2026-11-07　　**相關**：分支 `claude/postgres-store`（從 `claude/docs-module-map` 開：要改那邊新增的 module-map.md）
**模組**：③ 存（全部 `aiops-*`）、⑦⑨（相似事故 RAG）、⑧（Grafana 資料來源）

## Plan：這一次要達到什麼

- **需求**：使用者要把 engine 的資料全部搬到 PostgreSQL（「未來其他系統要復用也比較簡單」），並加 RAG。三種資料：異常、基準線、告警（量大、依時間查、聚合）；事故、回饋（量小、欄位比對找相似）；調整、對照覆寫（量很小、有狀態與紀錄）。
- **使用者的決定**：①**一次切過去，直接刪 ES 存取**（不留兩套）；②RAG **欄位比對為主、向量輔助**；③PostgreSQL 是**公司現有的、要申請**。
- **範圍**
  - **搬**：engine 自己寫的 `aiops-anomalies`、`aiops-baseline`、`aiops-alerts`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning`、`aiops-mapping`、`aiops-state`、`aiops-chronic`、`aiops-trace-segments` → PostgreSQL 一張表一個。
  - **不搬**：Elastic APM 寫的原始資料（公司的 ES，engine 只讀）；`traces-merged-*`（從原始 trace 合成、跟原始資料一起由 ES 的 ILM 管，根因分析用 ES 的 nested 查詢）。
  - **只用標準 PostgreSQL**（14 以上、jsonb、GIN、BRIN），不用 TimescaleDB；`pgvector` 有就用、沒有也能跑（向量存一般欄位、在程式裡算相似度，事故只有幾十到幾百件）。
  - Grafana 的儀表板與告警規則改讀 PostgreSQL。
  - 既有資料：提供一次性的「從 ES 匯入」指令。
- **設計**
  - 一張表 `id`（文件 id）、`ts`（這張表的時間欄）、`doc jsonb`（整份文件）；常查的欄位加運算式索引。存取類別對外的方法不變，裡面改成 SQL：呼叫端幾乎不用動，`tests/fakes.MemoryRepos` 照用。
  - 表格名、SQL 只在各功能的 `store/` 與 `common/`（跟原本「索引名只在 store」同一條規則）。
  - 事故被確認或匯入後才算向量；embedding 模型用設定的 OpenAI 相容 API，沒設就只用欄位比對。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/common/infra/pg.py`（新） | `DB`（連線，第一次查詢才連、斷了重連一次、`has_vector`）、`PgRepository`（`id`、`ts`、`doc jsonb` 一張表；`_select`、`_mget`、`_put`、`bulk`、`_patch`、`delete_where`、`count`；寫入前 NaN → null、numpy → Python） | 一個基底，11 個存取類別都用它；對外的方法跟原本 ES 版一樣，呼叫端幾乎不用動 |
| `aiops-engine/src/common/config/settings.py`、`requirements.txt` | `AIOPS_PG_DSN`、`AIOPS_PG_SCHEMA`、`EMBEDDING_*`；`psycopg[binary]` | 設定只有這裡讀環境變數 |
| `aiops-engine/src/common/infra/repos.py`、`es.py` | `Repos(es, scenario, db)`，db 沒給用環境變數；ES 版的 repository 也帶 db（讀原始資料的要查欄位對照的覆寫） | |
| 11 個存取類別（`detect/store/`、`incident/store/`、`learn/store/`、`common/scenario/mapping.py`） | 改成 SQL；拿掉 ES 的 mapping；`upsert_auto` 改成 `INSERT … ON CONFLICT … WHERE status 是 auto`；相似事故的候選照權重在 SQL 裡排序 | 使用者選「一次切過去、刪 ES 存取」 |
| `aiops-engine/src/detect/housekeeping.py`、`detect/config/prune.py`、`configs/baseline/prune.yaml` | prune 改成各表的 `prune(cutoff)`；設定從索引名改成 `tables` | 告警只刪已結案的規則放在 repository 裡 |
| `aiops-engine/src/llm/embedding.py`（新）、`incident/store/vector_repo.py`（新）、`llm/similar.py`、`incident/config/rca.py` | OpenAI 相容的 embedding；事故向量表；`search` 欄位比對為主、向量最近的也當候選、相似度加分並寫出來；`alerts.similar_vector` | 使用者選「欄位比對為主、向量輔助」；公司的 PostgreSQL 不一定准裝 pgvector，所以沒裝也能跑 |
| `aiops-engine/src/common/infra/es_import.py`（新）、`interface/cli/db.py`（新） | `detector db init／status／import-es` | 一次切過去：既有資料一次搬完 |
| `grafana/helmchart/files/`（`datasource.yaml`、`alert-rules.yaml`、3 個儀表板） | 一個 PostgreSQL 資料來源 `aiops-db`；4 條規則與 17 個面板、2 個變數改 SQL | Grafana 也要讀新的地方 |
| `aiops-engine/helmchart/`、`grafana/helmchart/values.yaml`、`elasticsearch/kibana-dev-tools.txt` | engine 的 `pgDsn`、`pgSchema`、`embedding.*`；Grafana 的 PostgreSQL 環境變數；ES 的 key 拿掉寫 `aiops-*`、只留搬資料用的唯讀，加「搬完怎麼刪」 | |
| 測試：`tests/pg.py`、`tests/conftest.py`（新）、22 個單元測試改寫、`tests/test_pg_repos.py`、`tests/test_vectors.py`（新）、`tests/aiops_es.py`（新）＋16 個 e2e | 測試自動開一個私用的 PostgreSQL、每個測試清空；原本假裝成 ES 存資料的測試改成寫進資料庫再看結果；e2e 的 `aiops-*` 請求轉到資料庫 | SQL 寫錯，假的抓不到 |
| 文件 | `deploy-how-to.md` 第 1b 節（跟 DBA 申請什麼、Grafana 唯讀帳號、搬資料）、`data-storage-reference.md`、`aiops-data-reference.md`、`architecture.md` 3.1、`grafana/README.md`、`module-map.md`、`CLAUDE.md` 等；舊名字 `aiops-xxx` 改成表名 `aiops_xxx` | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 每個查詢條件的語意跟 ES 版一樣：時間區間 [start, end)、term、terms、exists（null 不算）、regexp 整串比對、排序、只回找得到的 | `tests/test_pg_repos.py`（真的 PostgreSQL） | 都對；`rule.http…` 不會被 `http\..*` 比到 |
| 同 id 覆蓋、部分更新只合併頂層、NaN／numpy／datetime 寫得進去、表不存在時讀是空的 | 同上 | 都對 |
| 只更新還是 auto 的事故（老師傅確認過的一個欄位都不動）；`tag_alert` 對不到的略過 | `tests/test_cmd_alert.py` | 對 |
| 相似事故候選不被截掉：同一段變慢的那件最舊、25 件只同服務的比較新，只要 20 件也找得到 | `tests/test_similar.py` | 對（以前 ES 要 200 件才找得到，現在 SQL 照權重排） |
| 向量：OpenAI 相容格式、說法不同意思一樣的找得到、門檻與分數、文字沒變不重算、API 掛了退回欄位比對；**有 pgvector 與沒有兩條路結果一樣** | `tests/test_vectors.py`（本機裝了 pgvector 測有的那條） | 都對 |
| 其他單元測試 | 全部（含 helm） | 915 個過、0 個略過 |
| 既有資料搬得過來、重跑不重複 | 本機 ES 的 `aiops-*` → `detector db import-es` 兩次 → `db status` | 10 個索引筆數完全一樣（baseline 3,024、trace 段 1,328…），第二次筆數不變 |
| **真的 ES＋PostgreSQL 的 e2e** | `mapping_e2e`、`mapping_trace_e2e`、`cases_tier_drop_e2e`、`synth_e2e`、`fine_e2e`、`adaptive_e2e`、`mixed_e2e`、`chain_e2e`、`factory_e2e`、`peer_edge_e2e` | 全部通過 |
| 在 master 本來就有失敗的 e2e，有沒有變差 | `rules_edge_e2e` 在 master 與這個分支各跑一次比失敗清單；`adjust_edge`、`trace_quality_edge` 也比 | `rules_edge` 175 過、失敗的 3 項跟 master 一字不差；另外兩個停在同一個地方（設定格式、測試資料被 ES 拒收）；`loop_e2e` 43 過，失敗的 2 項是 master 就有的舊斷言 |
| Grafana 真的讀得到 | 本機 Grafana 13.0.7 載入新的 provisioning、PostgreSQL 唯讀帳號：資料來源健康檢查、每個面板與變數用 `/api/ds/query` 查、看告警規則的狀態；寫一件開著的告警與心跳進去 | 健康檢查 OK；17 個面板、2 個變數 0 個失敗；4 條規則 health ok；「事件」規則照那一列發出一則，labels 帶 `dept`、`group`、`severity`、`summary`；寫入心跳後「AIOps 停了」恢復 |

## Act

- **公司的 PostgreSQL 還沒申請**：要的東西在 [deploy-how-to.md 第 1b 節](../../deploy-how-to.md#1b-postgresqlengine-自己的資料)。正式環境的量（異常、trace 段統計 90 天）要用真的資料估；量大到慢再改分區表。
- **`distributed_e2e` 沒跑**：要 Redis，這個環境沒有。`baseline_edge_e2e` 太久，沒跑。
- **embedding 沒接真的模型**：測試用假的向量；正式要公司的 LLM 閘道給 embedding API，門檻 0.8、權重 5 是估的，要用真實事故調。
- **e2e 的 `tests/aiops_es.py` 只支援 e2e 用到的查詢**：寫新的 e2e 要讀 engine 的資料，直接用 repository 或 SQL 比較好。
- **ES 的舊索引沒有自動刪**：搬完、對過、Grafana 正常之後，照 `kibana-dev-tools.txt` 第 6 段手動刪。
- 這個環境為了測試開了本機 PostgreSQL（`aiops`、`grafana_ro` 帳號）、裝了 pgvector、把本機 ES 的磁碟水位改成絕對值；只影響這台測試機。
