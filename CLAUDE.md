# it-aiops 專案慣例

給每一個在這個 repo 寫程式或寫文件的人（含 Claude）。程式與架構見 [docs/README.md](docs/README.md) 的文件地圖、[docs/architecture.md](docs/architecture.md)。

## 結構

| 資料夾 | 內容 |
|---|---|
| `elasticsearch/` | ③ 在公司 ES 上補的東西：一份貼進 Kibana Dev Tools 的檔 `kibana-dev-tools.txt`（`traces-merged-*` 的 ILM 與 template、engine 與 Grafana 的 key、以前存在 ES 的 `aiops-*` 搬完之後怎麼刪）；PostgreSQL 要申請什麼見 `docs/deploy-how-to.md` |
| `grafana/` | ⑧ 通知、儀表板、回饋表單（`code/` provisioning、`helmchart/`） |
| `aiops-engine/` | ④～⑨ AIOps。資料夾本身就是專案根（`Dockerfile`、`requirements.txt`、`configs/`、`tests/`、`helmchart/`），沒有 `code/` 這層；Python 程式放在 `aiops-engine/src/`（沒有套件資料夾），一個功能一個資料夾，依賴只能往 `interface → learn → incident → llm → detect → data → common` 走（`tests/test_layers.py` 鎖著，表名只能在各功能的 `store/` 與 `common/`）；engine 自己的資料（異常、基準線、告警、事故、回饋、調整、對照覆寫）存**公司的 PostgreSQL**，一種一張表（`id`、`ts`、`doc jsonb`，基底 `common/infra/pg.py` 的 `PgRepository`，`detector db init／status／import-es`），ES 只放原始資料與 `traces-merged-*`：`data/`（② 清資料：raw 的 trace span（OTel 或 Elastic APM）→ 統一格式、一個請求一筆；`source/` 來源適配器：一種原始資料格式一個（`otel.py`、`apm.py`，介面 `SourceAdapter` 在 `adapter.py`、登記表在 `__init__.py`；是不是 apm、欄位怎麼轉都只在這裡，`tests/test_source.py` 用 AST 鎖著；對照見 `docs/data-sources-reference.md`；`check.py` 自我檢查宣告的來源跟資料對不對，只回報不擋；OTel 與 APM 可並存：`AIOPS_SOURCE=otel,apm`、`trace.yaml` 的 `raw_index` 寫清單，見 `configs/mixed/`；`mapping_service.py` 欄位對照的覆寫：網頁 `/mapping` 提、`detector mapping approve` 核准，存 PostgreSQL 的 `aiops_mapping`，`common/scenario/mapping.py` 讀時蓋在情境的 owner、log 上，trace 的 raw 索引另外蓋在 `trace.yaml` 上，trace 的部門、群組跟著 owner）；`trace/` 的 `span_merge`（合成 traces-merged-*）、`path_builder`／`path_service`（工廠履歷合成）；`store/` 的 `models.py` 是 trace 的資料物件與 raw→統一 span 的轉換，另有 `trace_repo`、`series_repo`（讀原始序列）、`owner_repo`（歸屬）；`data/config/paths.py` 是工廠履歷的設定）、`detect/`（③④⑤ 異常分析：`strategy/` 四種判斷方法 `threshold_rule`、`peer_compare`、`dynamic_baseline`、`chronic_issue`；`series_ops/` 序列的前處理與修正 `quality_check`、`level_shift`、`manual_adjust`；`trace/` 每段統計與偵測（`latency_stats`）；`fab_ingest/` 工廠接 ML／FDC 判好的異常；`detect_service` train／detect／tune 流程；`housekeeping` 清過期文件；`store/` 的 `models.py` 是異常與基準線的資料物件，另有各索引的存取）、`llm/`（LLM 摘要 `summary.py`、過去相似事故 `similar.py`：分類、歷史線索、先查清單；`embedding.py` 向量輔助，pgvector 選用）、`incident/`（⑥⑦⑧ 告警與根因：`alert_service` 主流程、`alert_grouper` 併件、`alert_severity` 嚴重度、`rca_analyzer` 根因計算、`rca_report` 報告；`strategy/` 是情境的跨對象併件與換版策略 `it_topology`／`it_deployment`／`fab_process_flow`／`fab_recipe_change`；`store/` 的 `models.py` 是 Alert，另有告警、事故、換版、log 的存取）、`learn/`（⑨ 學習迴圈：`feedback_service` 收集與觸發、`tuning_service` 審核與套用、`param_suggester` 參數建議、`rule_backtester` 回測、`evaluation_metrics` 評估指標、`history_importer` 歷史事故單匯入；`store/` 的 `models.py` 是調整與回饋的資料物件，另有兩個存取）、`interface/`（`cli/` typer 子命令，`fab.py` 是工廠情境的 paths-merge、ingest；`web/` FastAPI 路由＋`dto`＋`views.py`＋`templates/` 模板、`jobs/` 排程器與佇列 worker）、`common/`（`common/config/settings.py` 環境變數集中成有型別的 `Settings`（程式裡只有它讀 `os.environ`，`tests/test_settings.py` 鎖著）；`clock/` 時間只在這裡解析（`parse_duration`、`parse_instant`、`TimeWindow`）；`infra/`：`env.py`、`es.py` ES 連線＋文件基底 `Doc`＋原始資料的 repository 基底、`pg.py` PostgreSQL 連線＋表的 repository 基底、`repos.py` `Repos(es, scenario, db)` 全部 repository；`common/types/incident.py` 事故與指紋（`llm/` 與 `incident/` 都要用，所以放這）；`common/blocks.py` 功能積木：④⑥⑦⑨ 每一段一張登記表（名字 → `模組:名字`），情境資料檔的 `blocks:` 選要哪幾塊、可以填自己寫的，`detector blocks` 列出全部；`scenario/`：`base.py` 與 `*.yaml` 情境資料檔 `extends` 可疊——`it_otel`、`it_apm`、`factory`、`metrics_only`，`load_scenario()` 在組合根載入、往下傳，`AIOPS_PLUGIN` 選——，值在 `configs/` 的設定檔，欄位的定義：共用的在 `common/config/`（`common.py` 共用型別、`loader.py` 讀檔驗證、`settings.py` 環境變數），各功能自己的放自己的 `config/`——`detect/config/`（`series.py`、`rules.py`、`peer.py`、`trace.py`…、`types.py` 是 detect 內共用的小段）、`incident/config/rca.py`、`interface/config/schedule.py`）。入口只有一個，在 `aiops-engine/` 底下跑 `python3 -m main <detector|investigator|scheduler|tasks|api> …`（`main.py` 把 `src/` 加進 import 路徑；排程表、helm 的 `args` 第一個字都是 `main`）；configs（`apm/` Elastic APM、`factory/`、`metrics_only/`、`baseline/` `rca/` `schedule/` OTel）、Dockerfile；`helmchart/`） |
| `docs/` | 原理、案例、PDCA 紀錄 |

**收值（①②③）是公司現有的 Elastic APM 與 ES，不在這個 repo**（2026-11 自建的 Collector 已刪）；engine 自己的資料存公司現有的 PostgreSQL（2026-11 從 ES 搬過來）。模組編號只用 ①～⑨：① 收值 ② 清資料 ③ 存 ④ 判斷 ⑤ 異常總表 ⑥ 收斂 ⑦ 根因分析 ⑧ 出口 ⑨ feedback。

## 術語對照表

寫文件一律用左欄。右欄是舊說法，**不要再寫**（PDCA 紀錄是當時的措辭，不回頭改）。

| 正式術語 | 舊說法 | 檢查腳本會擋 |
|---|---|---|
| 動態基準線學習 (Dynamic Baseline Learning) | 學平常的樣子、學「平常」、跟自己的歷史比 | |
| Pod 同儕橫向比對 (Peer Comparison) | 跟兄弟比、兄弟比、Pod 互比（程式裡的名字 `peer` 照舊） | 跟兄弟比 |
| 靜態臨界值規則 (Static Threshold Rules) | 規則（一次就算）、保底規則、固定門檻 | 保底規則 |
| 單點即判定 (Instant Anomaly) | 單次就報、一次就算（`confirm_points: 1`、`instant_z`） | |
| 例外吞噬檢測 (Exception Swallowing Detection) | 吞例外、吞掉的例外（規則 `swallowed`） | |
| 資深維運專家回饋 (Expert Feedback) | 老師傅回饋 | |

- 「老師傅」是產品用語（賣點之一），使用者介面和對話可以用；正式文件第一次出現寫「資深維運專家（老師傅）」。「一件」（一件事故）、「值班」、「一把 key」（量詞）都是正常用語，不在表內。
- 檢查腳本只擋「檢查腳本會擋」欄的片語，其餘靠人看。要加新片語：先加進這張表，再加進 [tools/check-docs.py](tools/check-docs.py) 的 `FORBIDDEN`。

## 文件擺放

| 寫什麼 | 放哪裡 |
|---|---|
| 為什麼這樣設計、原理、方法的限制 | `docs/`（log、metrics、trace、rca、feedback） |
| 怎麼跑、參數、輸出在哪、怎麼驗 | 跟程式放一起：`aiops-engine/configs/<名>/README.md`（操作）、`reference.md`（規格）、`verification.md`（實測）、各元件的 `README.md`（部署）、`e2e/<環境>/README.md`；原理用連結指回 `docs/`，不重寫 |
| 每次工作的紀錄 | `docs/pdca/phase<N>-…/`；不屬於某個 phase 的放 `docs/pdca/meta/`；更新 [docs/pdca/records.md](docs/pdca/records.md)（現況與待辦在 [README.md](docs/pdca/README.md)）；描述的程式已不在 repo 的移到 `docs/pdca/archive/` |
| 構想、還沒做的 | 檔頭加一行 `> ⚪ 構想，尚未實作`；部分做了用 `> 🟡`；在 docs/README.md 標狀態 |

### Diátaxis：每份共用文件只做一種事

`docs/` 底下的共用文件（不含 PDCA 紀錄）開頭都有一行 `> **類型：…**`，四種之一，**不要把兩種寫在同一份**：

| 類型 | 回答 | 檔名慣例 |
|---|---|---|
| 原理 Explanation | 為什麼這樣設計？ | 主題的 `README.md`、`architecture.md`、`<主題>.md` |
| 操作 How-To | 我要做 X，怎麼做？ | `how-to.md`、`troubleshooting.md`、`*-how-to.md` |
| 規格 Reference | 查欄位、參數、現況 | `reference.md`、`catalog.md`、`*-reference.md` |
| 案例 Cases | 實際跑一遍長什麼樣？ | `cases.md`、`scenarios.md` |

- 一個主題要寫的東西超過一種，就在**同一個資料夾**拆成不同檔案（不另開 `explanation/`、`how-to/` 資料夾），並在 `docs/README.md` 標類型。
- 「現況與待辦」「還沒做」留在該主題的原理文件（Diátaxis 沒有「狀態」這一類，放在原理最接近）。
- 步驟（部署、驗證、遇到問題怎麼辦）不要寫進原理或規格；原理（為什麼）不要寫進操作，用連結指過去。
- `aiops-engine/`、`grafana/` 旁邊的 README 本來就貼著程式（操作、規格），多半不拆，只在 `docs/README.md` 標類型。**例外：`aiops-engine/configs/baseline/` 已依類型拆開**——`README.md` 操作、`reference.md` 規格、`verification.md` 案例（實測），原理在 [rca/README.md](docs/rca/README.md)；這個資料夾的 md 同樣要標類型。

- 新增、刪除、改名 md：同一個 commit 更新 `docs/README.md`（PDCA 紀錄更新 `docs/pdca/records.md`）。
- 長文（超過約 300 行）在第一個 `##` 前放 `<!-- toc -->` 和 `<!-- /toc -->`，用 `python3 tools/check-docs.py --toc` 產生，不要手寫目錄。
- 提交前：`python3 tools/check-docs.py`（連結、索引、舊說法、類型、反引號裡的程式路徑要存在），改了程式再跑 `cd aiops-engine && python3 -m pytest -q`。

## 工作方式

- **新工作先開 branch、先寫 PDCA 計畫**，再動手。PDCA 四段：**Plan**（需求、目的、範圍）、**Do**（改了哪些程式、為什麼）、**Check**（測試的目的與結果，不寫「N 個測試通過」就算）、**Act**（沒解決的，在什麼情況下發現的）。範本 [docs/pdca/TEMPLATE.md](docs/pdca/TEMPLATE.md)。
- **只有被明確說「合回 master」才合進 master**；「好」不算。
- 告警一個服務一件；通知交給 Grafana；部門決定誰看得到。
