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
aiops-engine/
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

### S1 做了什麼（2026-11-06）

- `core/repository/` 一個實體一個：`_base.py`（`Repository`：ensure、翻頁 `_scan`／`_composite`、`_mget`、`_put`／`_patch`／`_delete`、`bulk`、`_bulk_update`）、
  `alert`、`incident`、`tuning`、`feedback`、`anomaly`（write 補 engine_version 與歸屬、tag_alert）、`baseline`、`segment`、`state`、`chronic`、
  `series`（原本 `sources/es_source.py` 的序列、points、rows 查詢；`fill_zero` 留在這裡）、`trace`（span、合成、入口統計、抽樣；原 `rca_queries.py` 的 trace 部分）、
  `log`（原 `rca_queries.py` 的 log 部分＋慢性病的每日筆數）、`change`（IT 版號第一次出現、工廠變更紀錄最近一筆）、`quality`（品質檢查的 composite 統計）、
  `housekeeping`（prune 的 count／delete_by_query、index template）。`Repos(es)` 一條連線上全部的 repository，指令開頭 `Repos(ES())`。
- service／scenario／interface 不再有 `es.req`／`es.search`／索引名：函式簽名的 `es` 全改成 `repo`；`Detector` 的 `self.es` → `self.repo`，`cmd_*` 的 `backend=` → `repo=`。
- mapping 跟著實體：新增 `core/entity/baseline.py`、`core/entity/feedback.py`（原本在 `service/detect/es_facade.py`、`service/feedback/suggest.py`）；`RULE_FIELDS` 搬到 `core/entity/anomaly.py`。
- 刪掉：`core/repository/{base,stores,rows,shape,rca_queries}.py`（Backend／Store／file／http 來源）、`service/detect/es_facade.py`、config 的 `source:`／`store:` 模型；
  pandas 版整形搬成 `tests/shaping.py`，`tests/fakes.MemoryRepos` 是不連 ES 的 Repos（`test_memory_repos.py` 用它跑 train→detect、rules、peer、相似事故）。
- `test_layers`：`INDEX_LITERALS` 填齊九個 `aiops-*` 索引（字面值只能在 `core/`）；`KNOWN` 的 `rca_queries.py` 換成 `log.py`（S2 清 `source.profile`）。
- Check：`pytest` 762 過、16 skipped（刪了 test_es_backend 與 file／http 的測試，加了 repository 的）；`check-docs` 0 問題。對真 ES 的 `loop_e2e`／`chain_e2e` 留到 S2 之後一起跑（容器裡的 ES 要重新起）。

### S2 做了什麼（2026-11-06）

- `scenario/base.py`：`Scenario` 是 frozen dataclass（source、owner／log 對照、用語與分類表、entity_keys、route、chain／change 兩個策略、
  severity_classes、critical_levels、sections、schedule、commands）；`load_scenario(spec)` 讀 `scenario/data/*.yaml`（`extends` 可疊：`it_apm` 疊 `it_otel`、
  `metrics_only` 疊 `factory`），環境變數 `AIOPS_PLUGIN`／`AIOPS_SOURCE` 的名字不變（helm 不用改）；客製給一份 yaml 的路徑或「套件.模組:名字」的 `Scenario` 實例。
- 策略是可呼叫物件：`scenario/it/chain.py` `CallEdges`、`it/change.py` `VersionFirstSeen`、`factory/chain.py` `LotLinks`、`factory/change.py` `LatestChange`；
  `NoChain`／`NoChange` 在 base。`route` 只有 http／recipe／none 三種算法，放 base。
- 刪掉全域單例：`plugin.current()`、`scenario/domain.py`、`scenario/source.py`、三個 `Plugin` 類別、`metrics_only/` 套件。情境在組合根載入
  （`Repos(ES(), load_scenario())`、cli 的 `build_app`、web 的 `set_es`、jobs 的 `trigger`）往下傳：`Repos.scenario` → repository（owner／log 對照、trace schema），
  service 從 `repo.scenario` 拿；`merge`／`owner`／`headline`／`summary`／`rca_route` 多一個 `sc` 參數（沒給才 `load_scenario()`，給測試用）；`llm.build_pack` 的 `unit` 進 pack。
- core 不再 import scenario：`entity/anomaly.py` 多 `NS_KEYS`／`NODE_KEYS`／`entity_of(a, entity_keys)`／`entity_value`；`entity/trace.py` 多 `Edge`／`topology`／`mermaid`
  （原 `service/rca/analyze.py`）與 `is_entry`／`merged_doc`（原 `service/detect/trace_merge.build`），`schema_of(index, source)`；`core/clock.py`（duration、now_ts、parse_ts）。
  scenario 也不再 import service（`_dur`、`_now`、`topology`、`build`、`merge.entity_of` 都改用 core 的）。
- `test_layers` 的 `KNOWN` 清空：四層方向現在沒有例外。
- Check：`pytest` 763 過、16 skipped（`test_plugin.py` 換成 `test_scenario.py`；`test_apm`、`test_owner`、`test_factory_alerts`、`test_api_endpoints` 改成明確給 Scenario）；
  `check-docs` 0 問題；scenario/README、architecture 4.2、configs 的註解改成情境的說法。

### S3 做了什麼（2026-11-06）

- `core/entity/alert.py`：併件時動一件屬性的行為搬成 `Alert` 的方法——`open_for`（新開一件）、`attach`（異常併進來；route 由情境算好傳進來）、
  `join_upstream`、`fold_into`（上游整件併進下游）、`close`、`own_anomalies`、`rca_route`、`owner`、`headline`、`conclusion`、`make_summary`；`SEVERITY` 一起搬。
  `service/converge/merge.py` 只剩「哪筆異常歸哪件」的演算法（345 → 239 行）；`owner`／`headline`／`summary`／`rca_route` 留轉手函式給 alert_service 與測試。
- `core/entity/trace.py`：`Trace` 類別（`group`、`root`、`self_times`、`error_origins`、`to_merged_doc`、`edges`）；trace-merge、工廠的履歷合成、IT 的併件依據改用它。
- service 內部併檔：`rules_job`→`rules.py`、`peer_job`→`peer.py`、`quality_job`→`quality.py`、`chronic_job`→`chronic.py`、`trace_stats_job`→`trace_stats.py`
  （計算與 Detector 子類別、指令同一檔）；`baseline_job` 留著（baseline.py 是模型，併了 860 行太大）。
- Check：`pytest` 763 過、16 skipped；`check-docs` 0 問題。

### S4 做了什麼（2026-11-06）

- `interface/web/app.py`（369 行）消失，拆成四處：① `submit`／`submit_missed`／`request_rca`／`list_alerts` 與 helper → `service/feedback/feedback_service.py`
  （網頁版、Grafana 表單、命令列版現在同一個 service）；② HTML → `interface/web/views.py` ＋ `views/feedback.html`、`missed.html`、`report.html`（jinja2 模板、自動跳脫；
  `requirements.txt` 加 `jinja2>=3.1`）；③ 資料形狀 → `interface/web/dto.py`（`form_data`、`form_json`、`missed_form_json`、`to_json`、表單元素 id、`ingest_config_path`）；
  ④ 推異常進 ⑤ 的 body 檢查與寫入 → `scenario/factory/ingest_job.ingest_api`。
- `routes.py`：同一個動作一條處理函式——`/feedback`＋`/api/feedback`、`/missed`＋`/api/missed`、`/rca`＋`/api/rca` 各一個，依路徑是否 `/api/` 開頭決定回 JSON 還是 HTML；
  路徑、狀態碼、回應格式都沒變（Grafana 直接打這些 URL）。
- cli 沒再拆：S0 已是 `detect.py`（組合根，含情境加的子命令）、`feedback.py`、`rca.py`、`common.py`；`jobs/` 已在 S0 搬好。
- Check：`pytest` 763 過、16 skipped（web 的測試改成 import `feedback_service`／`dto`／`views`；斷言不變）；`check-docs` 0 問題。

### S5 做了什麼（2026-11-06）

- 刪掉殘餘：空的 `feedback/`、`service/detect/_util.RULE_FIELDS`、`trace_merge.build`、`merge.NS_KEYS`、`suggest.FEEDBACK_*`、`analyze` 的 trace 轉手等「名字留給既有 import」的 re-export（用的人直接 import entity）。
  相容入口留著（`detector/`、`investigator/`、`scheduler/`、`tasks/`、`api/` 各 2 行；排程表、helm 不用改）。
- 文件：CLAUDE.md 的結構表改成四層；architecture 4.2 的資料夾段與 §8 的程式路徑；aiops-data-reference、data-storage-reference、detection-and-learning、
  aiops-engine/README、interfaces/README 的舊路徑；docs/README 拿掉 `models/README` 那列。PDCA 紀錄裡的舊路徑是當時措辭，不回頭改。
- 對真 ES（容器裡的 8.19.0）跑 e2e 抓到一個單元測試沒蓋到的錯：`alert_service.alert_doc` 用了不在範圍內的 `repo`（S2 把情境改成傳參數時漏傳）；
  修掉並加 `tests/test_cmd_alert.py::AlertDocUsesScenario`。
- 現況：`aiops/` 95 個 .py、11,715 行（core 2,416、scenario 777、service 6,368、interface 1,340、config 805）；106 個類別、714 個函式；tests 14,317 行。
  相對 master：254 個檔案、+5,910／−6,235 行。

### 事後調整：拿掉 `aiops-engine/code/` 這一層（2026-11-06）

使用者回報 `aiops-engine/code/aiops/` 讀起來混亂（資料夾裡又一個 aiops）。把 `code/` 這層拿掉：`Dockerfile`、`requirements.txt`、`configs/`、`tests/`、`helmchart/` 與 Python 套件 `aiops/` 現在都直接在 `aiops-engine/` 底下（`git mv`，歷史保留）。
改了 55 份 md 的連結與路徑文字（用腳本依檔案搬家前後的位置重算相對連結，PDCA 紀錄裡的舊路徑文字也一併更新，因為 `check-docs` 要求連結不能斷）、測試裡的相對深度（`test_deploy`、`test_grafana_form`）、sonar、`.gitignore`、`tools/check-docs.py`、helm values 的註解。
`grafana/`、`elasticsearch/` 的 `code/` 沒動（那是它們自己的結構）。Python 套件名 `aiops` 沒改（改了所有 import 都要動）。
Check：`pytest` 764 過、`check-docs` 0 問題、`chain_e2e` 26/26、`factory_e2e` 15/15（都在新位置 `aiops-engine/` 底下跑）。沒實際 `docker build`（Dockerfile 的 COPY 路徑沒變，因為 build context 本來就是 code/ 那一層）。

### 事後調整二：刪掉五個相容入口資料夾（2026-11-06）

使用者問 `aiops-engine/` 第一層的檔案是不是跟 `aiops/` 重複。實際檢查：`detector/ investigator/ scheduler/ tasks/ api/` 每個只有 `__init__.py` 和 7～17 行的 `__main__.py`（轉呼叫 `aiops.interface.*`），沒有重複的邏輯，但是多餘的一層。
刪掉，改成單一入口 `aiops/__main__.py`：`python3 -m main <detector|investigator|scheduler|tasks|api> …`。同步改：四張排程表的 `args`（第一個字加 `aiops`）、helm 的 `args`（deployment、distributed、`_helpers.tpl` 的 api）、Dockerfile（拿掉五個 COPY，`CMD` 改 `aiops detector --help`）、e2e 腳本的子行程指令、`test_cli`／`test_scheduler`／`test_deploy`／`test_tasks` 的預期值，以及文件和 docstring 裡約 100 處 `python3 -m detector …` → `python3 -m main detector …`（PDCA 紀錄的舊指令是當時措辭，不改）。
子命令名稱（`train`、`detect`、`alert`…）沒變，`interfaces/`、`fake-data/` 是資料與說明文件，不重複，留著。
部署影響：已部署的環境升版時，chart 與 image 要一起升（舊 chart 的 `args: ["scheduler", …]` 在新 image 找不到 `scheduler` 模組）。
Check：`pytest` 764 過、`check-docs` 0 問題；`python3 -m main`、`aiops detector --help`、`aiops scheduler --list` 手動確認；e2e 見下。

## Check：預計要驗證的

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 每步行為沒變 | 每步結束 `pytest -q`；基準是現在的 785 過、16 skipped | ✅ 每步都全綠；最後 764 過、16 skipped（少的是刪掉 file／http 來源、Backend／Store 的測試與舊 plugin 測試，加了 repository、Scenario、alert_doc 的） |
| 對真的 ES 與 Grafana 仍然通 | S1、S2、S5 後跑 `loop_e2e`（45/45）、`chain_e2e`（26/26）；Grafana 13.0.7 的規則、webhook、10 個面板查詢 | ✅ S5 後對容器裡的 ES 8.19.0：`loop_e2e` 43/45（2 個 FAIL 是改之前就有的過期斷言「過去相似」，`claude/e2e-verify` 分支已修）、`chain_e2e` 26/26、`factory_e2e` 15/15、`--plugin metrics_only` 13/13；S1 之前的 e2e 沒跑（容器裡的 ES 要重起，改在 S5 一次跑）。Grafana 13.0.7：4 個 ES 資料來源 healthy、3 條規則 provisioned、webhook 收到「AIOps 停了」（e2e 之間 alert 沒持續跑，預期）；儀表板這次沒重載 provisioning，面板查詢沒重驗 |
| 索引名與 ES DSL 只在 repository | `test_layers` 擴大：`aiops-*` 的字面值與 `"bool"`／`"terms"`／`"query"` 只能出現在 `core/repository/` | 🟡 九個 `aiops-*` 索引名的字面值鎖在 `core/`（entity 定義、repository 讀寫）；DSL 關鍵字沒鎖（`"query"` 也出現在 series.yaml 的 filter 設定裡，鎖了會誤殺），改用 grep 確認 service／scenario／interface 沒有 `es.req`／`es.search`／`_search`／`_mget`／`_update` |
| 依賴方向 | `test_layers`：interface → service → scenario → core | ✅ `KNOWN` 清空，沒有例外；函式內 import 只剩 2 個（sqlalchemy 選配、local 模式才載 worker） |
| 排程與 helm 不用改 | `tests/test_cli.py::ScheduleCommands`：排程表裡每條 `python -m detector …`、`investigator …` 還解析得過 | ✅ 四張排程表全部解析得過；helm 的 `AIOPS_PLUGIN`／`AIOPS_SOURCE` 名字沒變 |
| 網頁版與命令列版回饋結果一致 | 同一筆輸入走 `FeedbackService.submit` 與 `feedback add`，比對寫進 ES 的文件 | 🟡 兩邊現在在同一個模組（`feedback_service.py`），走同一個 `Repos.feedback`；文件格式本來就一樣（`_id` 都是 `doc_id("feedback", …)`），但「一件的每筆異常各一筆」（網頁）與「一筆異常一筆」（命令列）仍是兩條路，沒有合成一個函式 |
| 舊路由相容 | `/feedback`、`/api/feedback`、`/missed`、`/api/missed`、`/rca`、`/api/rca` 的狀態碼與回應 | ✅ `test_api_endpoints`、`test_api_http`、`test_grafana_form` 的斷言沒改一個值，全過；三組路由各只剩一個處理函式 |

## Act：做完之後還沒解決的

| 問題 | 在什麼情況下發現 | 打算 |
|---|---|---|
| `alert_doc` 漏傳情境，單元測試沒抓到 | S5 跑 `loop_e2e` 第一輪就炸；`test_cmd_alert` 沒有測 `alert_doc` | 已修、已補測試。教訓：把全域單例改成參數時，grep 每一個被改簽名的函式有沒有單元測試 |
| Grafana 儀表板沒重載 provisioning | 這次只驗了資料來源、規則、webhook | 下次改 Grafana 相關的東西時一起驗；儀表板的 10 個面板查詢沒動過，風險低 |
| `baseline_job.py` 461 行沒併進 `baseline.py` | S3 併 `*_job` 時：併了會 860 行 | 留著；train／detect／tune 三條流程之後可以再拆成三個檔 |
| 網頁版與命令列版的回饋仍是兩條寫入路徑 | S4 | 等老師傅的回饋格式定下來（一件一筆還是一筆異常一筆）再合 |
| `loop_e2e` 的 2 個過期斷言 | 改之前就有（`claude/e2e-verify` 分支已修） | 那個分支合回來就沒了 |
| 分散式模式（`interface/jobs/worker.py`）保留，但這一輪沒對真 broker 跑 `distributed_e2e` | S5 只跑了一隻 pod 模式的三支 e2e | 下次動 jobs/ 時跑 |

## 事後調整三：取消 `aiops/` 這一層

**為什麼**：使用者覺得 `aiops-engine/aiops/` 重複，而且 `configs/`、`interfaces/` 不知道是什麼。這推翻了上面「待你決定」第 1 項（當時建議單一套件 `aiops/`）。

**做了什麼**
- `aiops/{core,scenario,service,interface}` 直接搬到 `aiops-engine/` 第一層；`aiops/config` 改名 `schema/`（它是設定檔欄位的定義，值在 `configs/`，避免跟 `configs/` 混淆）。
- 入口 `aiops/__main__.py` → `main.py`，指令變 `python3 -m main <角色> …`；排程表、helm、Dockerfile、e2e、測試的第一個字從 `aiops` 改成 `main`。
- `ENGINE_VERSION`、`CODE_ROOT`（原本在 `aiops/__init__.py`）移到 `core/env.py`。
- 情境 yaml 的策略路徑 `aiops.scenario.it.chain:…` 改成 `scenario.it.chain:…`。
- `interfaces/README.md`（只有說明，沒有程式）改放 `fake-data/interfaces.md`，刪掉 `interfaces/`。
- `tests/test_layers.py` 改成掃 `schema/ core/ scenario/ service/ interface/` 五個資料夾，依賴規則不變。

**取捨**：頂層名字（`core`、`service`、`schema`…）比較泛，Python 路徑上要是有同名的第三方套件會撞；目前環境沒有。部署時 image 與 chart 要一起升（`args` 第一個字變了）。

## 事後調整四：五個程式資料夾收進 `src/`

**為什麼**：取消 `aiops/` 後，`schema/ core/ scenario/ service/ interface/` 跟 `configs/ tests/ helmchart/ fake-data/` 混在 `aiops-engine/` 第一層，使用者覺得散。

**做了什麼**：五個資料夾搬進 `aiops-engine/src/`；`main.py` 留在 `aiops-engine/` 第一層，開頭把 `src/` 加進 `sys.path`，所以從 `aiops-engine/` 跑 `python3 -m main …` 不用設 `PYTHONPATH`，排程器開的子行程（cwd 是 `aiops-engine/`，`configs/…` 相對路徑才找得到）也一樣；`tests/__init__.py` 做同樣的事；`core/env.py` 的 `CODE_ROOT` 仍指 `aiops-engine/`；Dockerfile 一行 `COPY src/ src/`；`test_layers.py` 改掃 `src/`。import 寫法不變（`from core.xxx import …`）。

## Act（計畫時）：預期的風險與沒有答案的

| 風險／問題 | 影響 | 打算怎麼處理 |
|---|---|---|
| 20 個測試檔用假 ES、22 個檔共 120 處 `mock.patch` 對模組屬性 | S1 會大量改測試；改壞斷言會讓測試失去意義 | 假 ES 換成 `InMemory*Repository`；斷言的值不改，只改「怎麼餵資料」；每個實體一步，改完立刻跑 |
| 33 個被測試 import 的模組路徑全部會變 | 一次性大量 import 改動 | S3、S4 的 `git mv` 用腳本改；改完 `pyflakes` 與 `pytest` 抓漏 |
| 11 份非 PDCA 的 md 提到舊檔路徑 | 連結斷 | S5 用 `check-docs` 抓；PDCA 紀錄裡的舊路徑是當時措辭，不回頭改 |
| repository 的粒度：`series_repository`（查 metrics 聚合）與 `trace_repository` 的查詢最複雜 | 方法清單定不好會變成又一個大雜燴 | S1 最後做這兩個，先把前面七個做完再看 |
| 單一套件 `aiops/` 改變所有 import | 是一次性成本，但後悔很貴 | 先問使用者（下面第 1 項） |

### 待你決定

1. **單一套件 `aiops/` 還是維持頂層 `core`、`service`…**：我建議 `aiops/`（頂層的 `core`、`api`、`tasks`、`models` 這種名字太泛，容易撞）；缺點是所有 import 都改。
2. **相容入口留多久**：`python -m detector`／`investigator`／`scheduler`／`tasks`／`api` 留 2 行轉接，排程表與 helm 不用改；要不要之後逐步換成 `python -m main …`。
3. **要不要保留分散式模式**（api／beat／worker，Celery＋Redis）：`helmchart/templates/distributed.yaml` 在用。留的話 `interface/jobs/worker.py` 保留；不留可以再刪 `tasks/` 與 celery、redis 依賴。
4. **會不會有第四種情境**：有的話 `Scenario` 的 yaml 加策略名就夠；只有 IT 與工廠兩種的話，`metrics_only` 也可以降成工廠 yaml 的一個開關。
