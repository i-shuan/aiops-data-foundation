# 目標架構：core／scenario／service／interface 四層，與逐檔搬遷表（2026-11）

**狀態**：計畫（還沒動 code，等使用者確認）　　**日期**：2026-11-06　　**相關**：分支 `claude/target-architecture`（從 master `58fa7ce` 開）；承接 [clean-code](2026-11-clean-code.md) 沒解決的結構問題
**模組**：②～⑨（程式結構，不改任何階段的行為）

<!-- toc -->
<!-- /toc -->

## Plan：這一次要達到什麼

- **需求**：clean-code 把檔案搬到「一個階段一個資料夾」、拆了大函式、鎖了依賴方向，但使用者一層一層問下來（plugin、core、app），發現架構本身還是怪的。盤點（grep 與讀程式）的事實：
  1. **資料存取散在每一層**：直接打 ES 的呼叫 `detector` 36、`feedback` 36、`sources` 23、`investigator` 16、`api` 11、`plugins` 4；ES 查詢語法在 7 個資料夾；`aiops-alerts`、`aiops-anomalies`、`aiops-incidents` 各被 5 個資料夾直接碰。改一個索引的欄位要動 5 個資料夾。
  2. **`core/` 是雜物間**（17 檔 2,356 行）：ES 連線、設定、擴充機制（plugin、domain、source）、LLM、trace 處理、五個資料契約、歸屬、命令列轉接混在一起，共同點只有「兩邊都要用」。
  3. **「情境客製化」沒有邊界**：`plugin`／`domain`／`source` 三層薄皮加全域單例 `plugin.current()`（15 個檔案直接 import），而實際差異大多只是資料（索引名、欄位名、分類表）。工廠專屬的程式散在 26 個檔案（`detector/ingest.py`、`api` 的 `POST /api/anomalies`…）。
  4. **業務邏輯與 HTTP、表單渲染混在 `api/app.py`**（382 行）：回饋的寫入邏輯（`submit`）和 `feedback/cmd_feedback.py`（命令列版）做同一件事、寫了兩份；表單用 Python 字串拼接。
  5. **沒人用的通用化**：`sources/` 的檔案、HTTP、記憶體來源與 `shape.py`，`models/` 的 re-export 門面（共約 1,100 行）；全部 `configs/` 沒有任何一個設定用 `kind: file` 或 `kind: http`，正式環境永遠是 ES。
  6. **資料物件沒有行為**：`Alert`、`Incident`、`Anomaly` 只有欄位與 `to_doc()`；併件、標題、歸屬還是 `alerts.py` 裡的函式在改它們的屬性。
- **目的**：照 Java 的分層把程式分成四層，每一層只做一件事，依賴只往一個方向：
  - **core**：連線、entity（資料物件，含它自己的行為）、repository（**唯一**懂 ES 的地方：索引名、mapping、查詢語法）。
  - **scenario**：情境客製化。一份 yaml（索引、欄位、分類表…）加兩個策略（跨對象併件依據、換版判斷）加該情境專屬的命令與路由。服務拿到的是明確傳進來的 `Scenario` 物件，沒有全域單例。
  - **service**：業務邏輯，一個階段一個資料夾（偵測、收斂、根因、回饋）；只呼叫 repository，不寫 ES 查詢、不認得索引名。
  - **interface**：命令列、HTTP、排程；只收參數、呼叫 service、回應。
- **範圍**：
  - 做：下面的目標樹與搬遷表；按步驟（S0～S5）執行，每步測試全綠才下一步。
  - 不做：改任何階段的演算法與行為；ES 查詢的語意；文件格式（`aiops-*` 的欄位）；`configs/` 的 yaml 內容（只新增 `scenario/` 的資料檔）；Grafana、helm 的設定（靠相容入口讓 `python -m detector …` 照舊能跑）。

## Do：目標架構與搬遷表

### 一、依賴規則（`tests/test_layers.py` 改成鎖這個）

```
interface  →  service  →  scenario  →  core
  (cli、web、jobs)  (偵測、收斂、根因、回饋)  (yaml＋策略)  (connection、entity、repository)
config 是葉節點：每一層都可以讀，它不 import 別人
```

- `service` 只認得 `scenario` 的**介面**（`Scenario` 物件、策略 Protocol），不 import `scenario/it`、`scenario/factory`。
- `core` 不 import 任何別的層；`repository` 是 `core` 裡唯一出現索引名與 ES 查詢語法的地方（`test_layers` 的「字面值只能在這裡」規則，擴大到所有 `aiops-*` 索引與 `"bool"`、`"terms"` 這類 DSL）。
- `Trace` 這類「純資料變換」的行為放 entity（例如 `Trace.edges()`、`Trace.to_merged_doc()`），讓 `scenario/it` 的併件依據不必 import 根因分析。

### 二、目標樹（單一套件 `aiops/`）

```
aiops-engine/code/
├── aiops/
│   ├── config/                 設定（pydantic）：loader.py（原 core/config.py）、models/（原 config_models.py 716 行按設定種類拆開）
│   ├── core/
│   │   ├── connection.py       ES 連線（原 core/es.py）
│   │   ├── entity/             資料物件，帶自己的行為
│   │   │     base.py anomaly.py alert.py incident.py feedback.py adjustment.py baseline.py trace.py
│   │   └── repository/         一個實體一個，唯一懂 ES
│   │         anomaly_ repository.py alert_ incident_ feedback_ tuning_ baseline_ trace_ series_ owner_ log_
│   ├── scenario/
│   │   ├── base.py             Scenario（pydantic）＋ ChainStrategy、ChangeStrategy 的 Protocol
│   │   ├── loader.py           load_scenario(name)：讀 data/*.yaml、組策略
│   │   ├── data/               it_otel.yaml  it_apm.yaml  factory.yaml  metrics_only.yaml
│   │   ├── it/                 chain.py（trace 呼叫關係）、change.py（版號第一次出現）
│   │   └── factory/            chain.py（履歷前後站）、change.py（recipe 變更）、paths.py、ingest.py、commands.py
│   ├── service/
│   │   ├── detect/             ②③④⑤：baseline、rules、peer、quality、trace_stats、trace_merge、shift、chronic、prune、adjustments、base（Detector）
│   │   ├── converge/           ⑥⑧：merge（併件演算法）、severity、alert_service（一輪流程）
│   │   ├── rca/                ⑦：analyze、report、similar、llm、investigate
│   │   └── feedback/           ⑨：feedback_service、incident_service、adjust_service、suggest、backtest、metrics、importer
│   └── interface/
│       ├── cli/                main.py（組合根）、detect.py、converge.py、rca.py、feedback.py、common.py
│       ├── web/                app.py、routes.py（一個動作一條路由）、views/（feedback.html、missed.html、report.html）、dto.py
│       └── jobs/               cron.py、runner.py、local.py（原 scheduler）、worker.py（原 tasks）、sleep.py
├── detector/__main__.py        相容入口（2 行，轉給 aiops.interface.cli）：排程表、helm 的 `python -m detector …` 不用改
├── investigator/__main__.py    同上；scheduler/__main__.py、tasks/__main__.py、api/__main__.py 同
├── configs/                    不動（另新增 scenario 資料檔在 aiops/scenario/data/）
└── tests/                      照新樹對應；假 ES 換成 tests/fakes.py 的 InMemory*Repository
```

### 三、逐檔搬遷表

動作：**搬**＝`git mv` 加改 import；**拆**＝一檔分成多處；**併**＝併進另一檔；**刪**＝不再需要；**重寫**＝內容換掉。行數是現在的行數。

#### core/（17 檔）

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `core/es.py`（92） | `core/connection.py` | 搬 | `ES` 類別（`req`、`search`、`bulk_index`、`ensure_index`）；`doc_id`、`get_field`、`time_range` 這類小工具留同檔 |
| `core/anomaly.py`（106） | `core/entity/anomaly.py` ＋ `core/repository/anomaly_repository.py` | 拆 | `Anomaly` → entity；`ANOMALY_INDEX`、`ANOMALY_MAPPING`、`KEYS_MAPPING` → repository |
| `core/alert.py`（126） | `core/entity/alert.py` ＋ `core/repository/alert_repository.py` | 拆 | `Alert` → entity（併件行為進來，見 alerts.py）；`ALERT_INDEX`、`ALERT_MAPPING`、`stamp_of` → repository |
| `core/incident.py`（254） | `core/entity/incident.py` ＋ `core/repository/incident_repository.py` | 拆 | `Incident`、`Fingerprint`、`classify` → entity；`INCIDENT_MAPPING`、`Fingerprint.es_query` → repository 的 `search_similar` |
| `core/tuning.py`（78） | `core/entity/adjustment.py` ＋ `core/repository/tuning_repository.py` | 拆 | `Adjustment`、`validated` → entity；`TUNING_INDEX`、`TUNING_MAPPING` → repository |
| `core/doc.py`（25） | `core/entity/base.py` | 搬 | `Doc` 基底 |
| `core/traces.py`（193） | `core/entity/trace.py` ＋ `core/repository/trace_repository.py` | 拆 | `Span`／`Trace` 的 `group`、`root`、`self_times`、`error_origins`、`segment` → entity（變成 `Trace` 的方法）；`to_span`／`_apm_span`／`from_merged`（ES 文件 → span 的轉換）→ repository |
| `core/owner.py`（54） | `core/repository/owner_repository.py` ＋ `service/detect/owner.py` | 拆 | `load_owners` 的查詢 → repository；`owner_of` 的比對 → service |
| `core/llm.py`（354） | `service/rca/llm.py` | 搬 | 它是 ⑦ 的功能，不是核心 |
| `core/config.py`（121） | `config/loader.py` | 搬 | |
| `core/config_models.py`（716） | `config/models/*.py` | 拆 | 按設定種類拆：series、rules、peer、quality、trace、rca、schedule、ingest、adjustments、prune、chronic、calendar、paths |
| `core/plugin.py`（121） | `scenario/base.py`（`Chain` 與 Protocol）＋ `scenario/loader.py` | 重寫 | 全域單例 `current()` 刪掉；改成 `load_scenario(name)` 回 `Scenario`，在組合根建一次、明確傳下去 |
| `core/domain.py`（56） | `scenario/base.py` | 併 | 全部是 `plugin.current().domain()` 的轉手；變成 `Scenario` 的屬性（`entity_label`、`unit_label`、`categories`…） |
| `core/source.py`（20） | `scenario/base.py` | 併 | 同上，變成 `Scenario.owner_profile`、`Scenario.log_profile` |
| `core/cli.py`（35） | `interface/cli/common.py` | 搬 | |
| `core/version.py`（5） | `aiops/__init__.py` | 併 | `ENGINE_VERSION` |
| `core/__init__.py`（0） | — | 刪 | |

#### sources/（6 檔，1,269 行）與 models/（6 檔，202 行）

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `sources/es_source.py`（312） | `core/repository/series_repository.py`（`series`、`alive_times`、`points`、aggregation 建構 `_value_aggs`／`_bucket_value`／`_bucket_events`）、`trace_repository.py`（`spans`、`spans_by_id`） | 拆 | 查詢本身一行不改，換個家 |
| `sources/stores.py`（201） | 各 repository | 拆 | `ESStore` 的 `load_baseline`／`save_baseline` → `baseline_repository`；`write_anomalies`／`anomaly_status` → `anomaly_repository`；`adjustments` → `tuning_repository`；`incidents`／`similar_incidents` → `incident_repository`；`MemoryStore`、`FileStore` 刪 |
| `sources/base.py`（162） | — | 刪 | `Backend`、`Store` 兩個大介面被「一個實體一個 repository」取代；`as_backend`、`write_anomalies` 轉手函式一併消失 |
| `sources/rows.py`（165） | — | 刪 | `MemoryBackend`、`FileBackend`、`HttpBackend`：沒有任何設定用 |
| `sources/shape.py`（384） | — | 刪 | 非 ES 來源專用的 pandas 版 value 整形 |
| `sources/__init__.py`（45） | — | 刪 | |
| `models/*`（6 檔） | — | 刪 | 只是演算法的 re-export 門面（演算法在 `detector/model.py` 等）；對應的 `tests/test_models.py` 改測真正的模組 |

#### detector/（24 檔，3,766 行）→ `service/detect/`、`scenario/factory/`

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `detector/model.py`（399） | `service/detect/baseline.py` | 搬 | 純 pandas，不碰 ES，只改 import |
| `detector/cmd_baseline.py`（466） | `service/detect/baseline_job.py` ＋ `interface/cli/detect.py` | 拆 | `train`／`detect`／`tune` 的流程 → service，改用 `baseline_repository`、`series_repository`、`anomaly_repository`、`tuning_repository`；`write_detect_status` → `alert_repository`；參數與分派 → cli |
| `detector/rules.py`（218）＋`cmd_rules.py`（75） | `service/detect/rules.py` | 併 | 計算與 `RulesDetector` 同一檔；`approved_rules` 讀 `tuning_repository` |
| `detector/peer.py`（81）＋`cmd_peer.py`（69） | `service/detect/peer.py` | 併 | |
| `detector/quality.py`（276）＋`cmd_quality.py`（65） | `service/detect/quality.py` | 併 | |
| `detector/trace.py`（328）＋`cmd_trace.py`（142） | `service/detect/trace_stats.py` | 併 | `collect`／`train`／`detect` 三個流程；`trace_merge` 見下 |
| `detector/trace_merge.py`（122） | `service/detect/trace_merge.py` ＋ `core/entity/trace.py` | 拆 | 流程留 service；`build(spans)` → `Trace.to_merged_doc()`；`updated_trace_ids`、`load_state`／`save_state` → `trace_repository` |
| `detector/cmd_shift.py`（114） | `service/detect/shift.py` | 搬 | 查 `anomaly_repository`、寫 `tuning_repository` |
| `detector/chronic.py`（101）＋`cmd_chronic.py`（69） | `service/detect/chronic.py` | 併 | 查 `anomaly_repository.daily_counts` |
| `detector/cmd_prune.py`（50） | `service/detect/prune.py` | 搬 | 刪除動作 → 各 repository 的 `delete_older_than` |
| `detector/adjust.py`（398） | `service/detect/adjustments.py`（pandas 套用運算：`train_filter`、`detect_filter`、`in_schedule`…）＋ `core/entity/adjustment.py`（`describe`、`problem`） | 拆 | `load`／`load_adjustments`／`load_overrides`／`data_adjustments` → `tuning_repository` 加 `config` 的 yaml 讀入 |
| `detector/base.py`（104） | `service/detect/base.py` | 搬 | `Detector` 範本；`Anomaly` 已在 entity |
| `detector/sources.py`（200） | `service/detect/providers.py` | 搬 | 負責人對照、行事曆的外部 provider（HTTP＋快取）；不是 `sources/` 那個資料來源，改名避免撞名 |
| `detector/es.py`（71） | — | 刪 | `fetch`、`load_baseline` 等轉手；`BASELINE_MAPPING` → `baseline_repository` |
| `detector/common.py`（22） | `service/detect/_util.py` | 搬 | |
| `detector/ingest.py`（149）＋`cmd_ingest.py`（72） | `scenario/factory/ingest.py` | 併 | 只有工廠用；由 `Scenario` 的 `extensions` 掛 CLI 命令與 HTTP 路由 |
| `detector/__main__.py`（175） | `interface/cli/detect.py`、`interface/cli/main.py` | 拆 | 原檔留 2 行相容入口 |
| `detector/__init__.py`（0） | — | 刪 | |

#### investigator/（9 檔，1,960 行）→ `service/converge/`、`service/rca/`

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `investigator/alerts.py`（354） | `core/entity/alert.py`（行為）＋ `service/converge/merge.py`（演算法） | 拆 | `_attach`→`Alert.attach`、`_fold`→`Alert.fold_into`、`_join`→`Alert.join_upstream`、`headline`、`summary`、`owner`、`rca_route` 變成 `Alert` 的方法；`merge` 與它的 helper（`_open_new`、`_downstream_alert`…）留 service |
| `investigator/cmd_alert.py`（377） | `service/converge/alert_service.py` | 搬＋重寫 | 一輪流程（`enrich`、`investigate_alert`、`alert_doc`、`announce`）；`_open_alerts`、`_done_ids`、`write_incidents`、`tag_anomalies` → repository；`titled`、`with_feedback_footer` → `Alert` 的 render 方法 |
| `investigator/severity.py`（108） | `service/converge/severity.py` | 搬 | |
| `investigator/analyze.py`（258） | `service/rca/analyze.py` ＋ `core/entity/trace.py` | 拆 | `topology`、`Edge`、`mermaid` → `Trace.edges()`（IT 併件依據要用）；其餘比較、瀑布圖、共同點留 service |
| `investigator/report.py`（400） | `service/rca/report.py` | 搬 | 各段落（`sec_*`）改成 Section 類別；`Ctx` 拿 `Scenario`、repository，不再問全域 plugin |
| `investigator/similar.py`（151） | `service/rca/similar.py` | 搬 | `explain`／`rank`／`evaluate`／`learn_weights`；`search` 改用 `incident_repository.search_similar` |
| `investigator/es.py`（152） | `core/repository/trace_repository.py`、`log_repository.py`、`anomaly_repository.py` | 拆 | `entry_stats`、`sample_trace_ids`… → trace；`logs_of_traces`、`problem_logs`、`change_logs` → log；`anomalies` → anomaly |
| `investigator/__main__.py`（160） | `interface/cli/rca.py`、`interface/cli/converge.py` | 拆 | 原檔留 2 行相容入口 |

#### feedback/（8 檔，1,746 行）→ `service/feedback/`

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `feedback/cmd_feedback.py`（420） | `service/feedback/feedback_service.py`、`incident_service.py` | 拆 | `FeedbackService.add/submit/submit_missed/export/pending/summary`，**命令列版與網頁版共用同一份**（現在 `api/app.py` 的 `submit` 與它各寫一次）；`incident_*` → `IncidentService`；`_anomaly_rows`、`_feedback_rows`… → repository |
| `feedback/cmd_adjust.py`（116） | `service/feedback/adjust_service.py` | 搬 | `list`／`approve`／`remove`／`add` |
| `feedback/suggest.py`（394） | `service/feedback/suggest.py` | 搬 | `FEEDBACK_INDEX`、`FEEDBACK_MAPPING` → `feedback_repository`；`propose_from_text` 的寫入 → `tuning_repository` |
| `feedback/cmd_backtest.py`（330） | `service/feedback/backtest.py` | 搬 | 用 repository 取代 `_CachedES`（快取放 repository 層） |
| `feedback/cmd_metrics.py`（137） | `service/feedback/metrics.py` | 搬 | |
| `feedback/importer.py`（178） | `service/feedback/importer.py` | 搬 | 讀 CSV／JSON／DB 的部分留；寫 → `incident_repository.bulk_import` |
| `feedback/cli.py`（168） | `interface/cli/feedback.py` | 搬 | |
| `feedback/__init__.py`（3） | — | 刪 | |

#### api/（4 檔，695 行）→ `interface/web/`

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `api/app.py`（382） | 拆成 5 處 | 拆 | ① `submit`、`submit_missed` 與 helper → `feedback_service`（約 100 行）；② `render_form`、`render_missed`、`render_report`、`_opt` → `interface/web/views/*.html`（模板，約 100 行）；③ `form_data`、`form_json` → `interface/web/dto.py`；④ `ingest`、`ingest_config_path` → `scenario/factory/ingest.py`；⑤ `request_rca`、`list_alerts`、`load_alert` → `alert_service`／`alert_repository` |
| `api/server.py`（268） | `interface/web/routes.py` | 重寫 | 14 條路由收斂：一個動作一條路由，用 `Accept` 或 `?format=` 回 HTML 或 JSON（現在 `/feedback`＋`/api/feedback`、`/missed`＋`/api/missed`、`/rca`＋`/api/rca` 各寫兩次）；舊路徑保留相容 |
| `api/__main__.py`（29）、`api/__init__.py`（16） | `interface/web/__main__.py`；原檔留相容入口 | 搬 | |

#### scheduler/、tasks/（7 檔）→ `interface/jobs/`

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `scheduler/cron.py`（63）、`runner.py`（144）、`sleep.py`（7） | `interface/jobs/cron.py`、`runner.py`、`sleep.py` | 搬 | |
| `scheduler/__main__.py`（64） | `interface/jobs/local.py` | 搬 | 原檔留相容入口（helm 的 `args: ["scheduler", …]` 照舊） |
| `tasks/__init__.py`（105）、`__main__.py`（18） | `interface/jobs/worker.py` | 併 | 這是「分散式模式」（api／beat／worker），跟 `scheduler` 共用 `runner`：不是兩套重複的排程，而是兩種部署模式。**要不要留分散式模式是使用者的決定**（見下「待你決定」） |

#### plugins/（10 檔，580 行）→ `scenario/`

| 現檔 | 去哪 | 動作 | 說明 |
|---|---|---|---|
| `plugins/observability/__init__.py`（100） | `scenario/data/it_otel.yaml`、`it_apm.yaml` | 重寫 | `PROFILES`、`DOMAIN` 的資料變成 yaml；`route_of`（method＋route）變成宣告：`route: {fields: [method, route]}` |
| `plugins/observability/chain.py`（26）、`changes.py`（16） | `scenario/it/chain.py`、`change.py` | 搬 | 兩個策略，改用 `trace_repository`、`Trace.edges()` |
| `plugins/factory/__init__.py`（78） | `scenario/data/factory.yaml` | 重寫 | `PROFILE`、`DOMAIN`、`SEVERITY_CLASSES` 變成 yaml |
| `plugins/factory/chain.py`（62）、`changes.py`（19） | `scenario/factory/chain.py`、`change.py` | 搬 | |
| `plugins/factory/paths.py`（174）、`cmd_paths.py`（82） | `scenario/factory/paths.py`、`commands.py` | 搬 | `paths-merge` 命令由 `Scenario.extensions` 掛上 |
| `plugins/metrics_only/__init__.py`（21） | `scenario/data/metrics_only.yaml` | 重寫 | `extends: factory`，`chain: none`、`sections` 縮減 |
| `plugins/__init__.py`（2） | — | 刪 | |

#### 行數預估（估計，沒量過實作）

| | 行數 |
|---|---|
| 刪：`sources/rows.py`、`shape.py`、`base.py`、`stores.py` 的 Memory／File 部分、`models/`、`detector/es.py` 轉手、`core/domain.py`、`core/source.py`、`core/plugin.py` 的單例 | 約 −1,300 |
| 增：repository 的樣板（每個實體的 `ensure`、`save`、`find_*`）、`Scenario` 載入、HTML 模板 | 約 +900 |
| 淨變化 | 約 −400；但**每個檔案的職責單一**，這才是目的 |

## Do：執行步驟

每步一個 commit，每步結束 `pytest` 全過；S1、S2、S5 之後另外跑 `loop_e2e`、`chain_e2e`（對真 ES，環境已有）。

| 步驟 | 內容 | 離開條件 |
|---|---|---|
| **S0 準備** | 建 `aiops/` 空樹；`test_layers` 改成新規則（先只對已搬的檔生效）；`tests/fakes.py` 放 `InMemory*Repository` 的骨架；相容入口先寫好 | 舊測試全綠；新規則測試存在 |
| **S1 repository 層** | 一個實體一個 PR 大小的步驟：`alert` → `incident` → `tuning` → `feedback` → `anomaly` → `baseline` → `trace` → `log` → `series` → `owner`。每個：建 repository、把所有直接 `es.req` 的呼叫點換成它、對應測試的假 ES 換成 InMemory | 該實體的索引名與 DSL 只剩 repository 一處（`test_layers` 對那個索引生效） |
| **S2 scenario** | `Scenario` 與四個 yaml、兩個策略；在組合根（cli／web／jobs 入口）建一次、明確傳進 service；刪 `plugin.current()`、`domain`、`source` | grep 不到 `plugin.current`；三個情境的 e2e 都過 |
| **S3 entity 長行為＋service 搬家** | `Alert` 長出 `attach`／`fold_into`／`headline`／`summary`；`Trace` 長出 `edges`／`to_merged_doc`；`git mv` 到 `service/…`，改 import | `alerts.py`、`analyze.py` 不再有「動 dict／物件屬性的函式」 |
| **S4 interface** | `feedback_service` 收回 `api.submit`；表單改模板；路由收斂；cli 拆檔；`jobs/` 搬家 | `api/app.py` 消失；網頁版與命令列版回饋走同一個 `FeedbackService` |
| **S5 刪除與收尾** | 刪 `sources/`、`models/`、`detector/es.py`、各相容殼；`python -m detector` 等入口留 2 行相容；更新 CLAUDE.md、architecture 4.2、aiops-data-reference、各 README 的路徑；PDCA Do／Check／Act | `check-docs` 0 問題；全測試與兩支 e2e 過 |

## Check：預計要驗證的

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 每步行為沒變 | 每步結束 `pytest -q`；基準是現在的 785 過、16 skipped | 未做 |
| 對真的 ES 與 Grafana 仍然通 | S1、S2、S5 後跑 `loop_e2e`（45/45）、`chain_e2e`（26/26）；Grafana 13.0.7 的規則、webhook、10 個面板查詢 | 未做 |
| 索引名與 ES DSL 只在 repository | `test_layers` 擴大：`aiops-*` 的字面值與 `"bool"`／`"terms"`／`"query"` 只能出現在 `core/repository/` | 未做 |
| 依賴方向 | `test_layers`：interface → service → scenario → core | 未做 |
| 排程與 helm 不用改 | `tests/test_cli.py::ScheduleCommands`：排程表裡每條 `python -m detector …`、`investigator …` 還解析得過 | 未做 |
| 網頁版與命令列版回饋結果一致 | 同一筆輸入走 `FeedbackService.submit` 與 `feedback add`，比對寫進 ES 的文件 | 未做 |
| 舊路由相容 | `/feedback`、`/api/feedback`、`/missed`、`/api/missed`、`/rca`、`/api/rca` 的狀態碼與回應 | 未做 |

## Act：預期的風險與沒有答案的

| 風險／問題 | 影響 | 打算怎麼處理 |
|---|---|---|
| 20 個測試檔用假 ES、22 個檔共 120 處 `mock.patch` 對模組屬性 | S1 會大量改測試；改壞斷言會讓測試失去意義 | 假 ES 換成 `InMemory*Repository`；斷言的值不改，只改「怎麼餵資料」；每個實體一步，改完立刻跑 |
| 33 個被測試 import 的模組路徑全部會變 | 一次性大量 import 改動 | S3、S4 的 `git mv` 用腳本改；改完 `pyflakes` 與 `pytest` 抓漏 |
| 11 份非 PDCA 的 md 提到舊檔路徑 | 連結斷 | S5 用 `check-docs` 抓；PDCA 紀錄裡的舊路徑是當時措辭，不回頭改 |
| repository 的粒度：`series_repository`（查 metrics 聚合）與 `trace_repository` 的查詢最複雜 | 方法清單定不好會變成又一個大雜燴 | S1 最後做這兩個，先把前面七個做完再看 |
| 單一套件 `aiops/` 改變所有 import | 是一次性成本，但後悔很貴 | 先問使用者（下面第 1 項） |

### 待你決定

1. **單一套件 `aiops/` 還是維持頂層 `core`、`service`…**：我建議 `aiops/`（頂層的 `core`、`api`、`tasks`、`models` 這種名字太泛，容易撞）；缺點是所有 import 都改。
2. **相容入口留多久**：`python -m detector`／`investigator`／`scheduler`／`tasks`／`api` 留 2 行轉接，排程表與 helm 不用改；要不要之後逐步換成 `python -m aiops …`。
3. **要不要保留分散式模式**（api／beat／worker，Celery＋Redis）：`helmchart/templates/distributed.yaml` 在用。留的話 `interface/jobs/worker.py` 保留；不留可以再刪 `tasks/` 與 celery、redis 依賴。
4. **會不會有第四種情境**：有的話 `Scenario` 的 yaml 加策略名就夠；只有 IT 與工廠兩種的話，`metrics_only` 也可以降成工廠 yaml 的一個開關。
