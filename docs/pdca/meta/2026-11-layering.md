# 高內聚、低耦合：套件分層、一個功能一個資料夾、plugin 邊界切乾淨（2026-11）

**狀態**：階段 A～C 完成，D 進行中　　**日期**：2026-11-05　　**相關**：分支 `claude/layering`（從 master `31c72f8` 開）；接 [frameworks](2026-11-frameworks.md)、[oop-core](2026-11-oop-core.md)（兩輪都在加介面與框架，沒動套件怎麼切，使用者看完判斷「檔案太散、plugin 沒切乾淨」）
**模組**：④～⑨ 全部

## Plan：這一次要達到什麼

- **需求**：使用者要求程式符合高內聚、低耦合。量出來的現況兩個都不合格：

| 指標 | 現況 | 怎麼量 |
|---|---|---|
| 套件雙向相依 | 6 組：core↔investigator、core↔sources、detector↔investigator、detector↔sources、investigator↔sources、scheduler↔tasks | AST 解析 import（含函式內） |
| 模組層循環 | 8 條（都繞過 `sources.base`／`es_source`／`stores`） | 同上 |
| 函式內 import 內部套件 | 118 處（躲循環用的） | 同上 |
| 底層帶情境詞 | `core/traces.py` 73 處 IT 詞；工廠詞散在 plugin 外 12 個檔 | grep |
| 一個功能幾個檔 | rules／peer／quality／trace 各分 `X.py`＋`cmd_X.py`；`detector/` 31 檔平放混四種東西 | 目錄 |
| 同名檔 | `es.py`×3、`rules`／`peer`／`trace`／`similar.py` 各兩份、`base.py` 兩份 | ls |
| plugin 邊界 | `plugins/` 只裝差異（774 行）；IT 專用 1,751 行在 `detector/`、`investigator/`、`core/`；工廠的 `ingest` 在 `detector/` | grep 情境詞 |

- **目的**：套件只能由上往下依賴；一個功能的引擎、命令、設定模型在同一個資料夾；IT 專用全在 `plugins/observability/`、工廠專用全在 `plugins/factory/`，`core/` 沒有任何情境詞。對外契約不變：`python -m detector|investigator|api|scheduler|tasks` 與排程表的字面指令、HTTP 端點、`configs/` 路徑都不動。

### 目標分層（只能往下 import；plugin 由 `core.plugin` 用字串 importlib 載入，不是靜態 import）

| 層 | 套件 | 放什麼 | 不能有 |
|---|---|---|---|
| L0 | `core/` | `es_client.py`（ES 連線、`doc_id`、`get_field`、`time_range`）、`config.py`（通用 `load(path, Model)` 與錯誤格式）、`anomaly.py`（`Anomaly`）、`detector.py`（`Detector` 基底）、`plugin.py`（介面、`current()`、吸收 `domain.py`／`source.py` 的門面）、`timeutil.py`（`_freq`、`_now`、`_v`）、`version.py` | 任何上層套件；任何情境詞 |
| L1 | `sources/` | `schema.py`（**所有** `aiops-*` 索引名與 mapping：baseline、anomalies、alerts、tuning、feedback、incidents、chronic、segments）、`base.py`、`es_source.py`＋`es_aggs.py`（`detector/es.py` 的聚合 helper 搬來）、`rows.py`、`shape.py`、`stores.py`、`traces.py`（`core/traces.py` 的 span 格式解析）、`providers.py`（`detector/sources.py` 的 file／http／taiwan 行事曆與負責人） | detector、investigator、plugins |
| L2 | `detector/` | ④⑤ 中立引擎＋CLI：`__main__.py` 只有命令註冊；`baseline/`（`model.py`、`train.py`、`detect.py`、`tune.py`、`config.py` SeriesConfig）、`rules/`（`engine.py`、`command.py`、`config.py`）、`adjust/`（`adjust.py`、`tuning.py`、`calendar.py`、`command.py`、`shift.py`、`config.py`）、`feedback/`（`feedback.py`、`command.py`、`metrics.py`、`backtest.py`、`incident.py`）、`prune/`、`owner.py` | investigator、plugins |
| L3 | `investigator/` | ⑥⑦⑧：`alerts/`（`alerts.py`、`command.py`、`severity.py`、`schema` 用 sources）、`rca/`（`analyze.py`、`report.py`、`queries.py`（原 `es.py`）、`llm.py`（原 `core/llm.py`，提示詞全是 RCA 的）、`config.py` RcaConfig）、`similar/`（`similar.py`、`importer.py`）；`__main__.py` 只註冊 | plugins |
| L4 | `plugins/` | `observability/`：`profiles.py`、`chain.py`、`changes.py`、`peer/`、`trace/`（`trace.py`、`trace_merge.py`、`command.py`、`config.py`）、`quality/`、`chronic/`，`commands()` 提供 peer／quality／chronic／trace-*；`factory/`：`ingest/`（原 `detector/ingest.py`、`cmd_ingest.py`）、`paths/`、`chain.py`、`changes.py`；`metrics_only/` | — |
| L5 | `api/`、`scheduler/`、`tasks/` | 入口。`scheduler/runner.py` 不 import `tasks`（`--enqueue` 的佇列函式由 `scheduler/__main__.py` 注入） | — |

- 刪掉：`models/`（202 行門面，只有 `tests/test_models.py` 用；`check_*` 併回各功能的 `config.py`）、`core/domain.py`、`core/source.py`（併進 `core/plugin.py`）、`detector/common.py`（→ `core/timeutil.py`）、`core/config_models.py`（拆到各功能的 `config.py`）。
- `configs/` **不搬**：它是設定資料，路徑寫在排程表、chart 的 `configOverrides`、CLAUDE.md、大量文件與 e2e 腳本裡；這輪只搬程式。
- **跨層契約的收法**：`Store.write_anomalies` 只負責寫，dept／group 的補值回到 `detector/owner.py`（寫入前補）；`Store.similar_incidents(fp…)` 改成 `search_incidents(body)`，查詞由 `investigator/similar` 組；`core/llm.py` 內叫 `investigator.similar.classify` 的那段，改成由呼叫端把分類結果傳進來；`write_detect_status` 要的 `ALERT_INDEX`／`ALERT_MAPPING`／`stamp_of` 從 `sources/schema.py` 拿。

### 分階段（每階段測試全綠才進下一階段）

| 階段 | 做什麼 | 驗收 |
|---|---|---|
| A | 解循環：建 `sources/schema.py`、`es_aggs.py`、`traces.py`、`providers.py`；`core` 與 `sources` 不再 import 上層；`detector` 不 import `investigator`；`scheduler.runner` 不 import `tasks` | 套件雙向 0；模組循環 0；函式內內部 import ≤ 5（只剩 `core.plugin.load` 的 importlib 與入口層） |
| B | `detector/`、`investigator/` 拆成功能資料夾；`config_models.py` 拆到各功能；`models/` 刪；同名檔消失 | 每個功能一個資料夾；非測試程式無同名 `.py`（plugin 的 `chain`／`changes` 例外） |
| C | IT 專用進 `plugins/observability/`，工廠的 `ingest` 進 `plugins/factory/`；`detector/__main__` 只註冊中立命令，其餘由 `commands()` 給 | `core/`、`detector/`、`investigator/` grep IT 與工廠詞 0（`sources/traces.py` 的格式解析例外，另註明）；刪掉 `plugins/factory/` 整個資料夾，其餘程式與測試照跑 |
| D | 文件連結、Dockerfile `COPY`、docs/README 索引、量指標、PDCA | `check-docs` 四項 0；指標表填上「之後」 |

### 階段 B 的搬移對照表（不留轉發用的舊檔；import 全部改指新家）

| 現在 | 新位置 | 說明 |
|---|---|---|
| `detector/base.py` | `core/anomaly.py`（`Anomaly`）、`core/detector.py`（`Detector`） | 中立的基底放 L0 |
| `detector/common.py` | `core/timeutil.py` | `_freq`、`_now`、`_v`、`keys_of` |
| `core/es.py` | `core/es_client.py` | 三個 `es.py` 之一；名字說清楚是連線 |
| `detector/es.py` | 刪。`fetch`／`alive_times`／`load_baseline`／`load_group_baselines` 四個一行函式搬到 `sources/__init__.py`（跟 `spans_of` 同一處）；mapping 的 re-export 不再需要（直接 `from sources.schema import …`） | 階段 A 後它只剩轉發 |
| `detector/model.py` | `detector/baseline/model.py` | |
| `detector/cmd_baseline.py`（466 行） | 拆三個：`detector/baseline/train.py`（`save_baseline`、`train_summary`、`min_events`、`apply_resolution_overrides`、`series_resolutions`、`cmd_train`）、`detector/baseline/detect.py`（`_keep_upgraded`、`save_anomalies`、`_add_silent`、`status_params`、`score_params`、`with_status`、`score_tuned`、`borrow_resolution`、`detect_borrowed`、`detect_series`、`write_detect_status`、`cmd_detect`）、`detector/baseline/tune.py`（`effective_lines`、`cmd_tune`） | 一個檔三件事 |
| `detector/rules.py`、`cmd_rules.py` | `detector/rules/engine.py`、`detector/rules/command.py` | 引擎與命令同一個資料夾 |
| `detector/adjust.py`、`tuning.py`、`cmd_adjust.py`、`cmd_shift.py`、`cmd_backtest.py` | `detector/adjust/adjust.py`、`tuning.py`、`command.py`、`shift.py`、`backtest.py` | 回測是核准調整的一環 |
| `detector/sources.py` | `sources/providers.py` | 行事曆與負責人的 file／http／taiwan 來源是資料層；也消掉跟套件 `sources/` 撞名 |
| `detector/feedback.py`、`cmd_feedback.py`、`cmd_metrics.py` | `detector/feedback/feedback.py`、`command.py`（含 `cmd_incident`、匯出）、`metrics.py` | |
| `detector/cmd_prune.py` | `detector/prune.py` | 單一檔就夠 |
| `detector/owner.py` | 不動 | |
| `detector/peer.py`、`quality.py`、`chronic.py`、`trace.py`、`trace_merge.py`、`ingest.py` 與它們的 `cmd_*` | **階段 B 不動**（階段 C 搬進 plugin） | 同名問題不在它們身上 |
| `investigator/alerts.py`、`cmd_alert.py`、`severity.py` | `investigator/alerts/merge.py`、`command.py`、`severity.py` | ⑥ 收斂 |
| `investigator/analyze.py`、`report.py`、`es.py` | `investigator/rca/analyze.py`、`report.py`、`queries.py` | ⑦ 根因分析；`es.py` 改名說清楚是查詢 |
| `investigator/similar.py`、`importer.py` | `investigator/similar/similar.py`、`importer.py` | 相似事故與匯入 |
| `core/llm.py` | 不動 | detector 的回饋、api、investigator 都用；階段 C 再看情境詞 |
| `core/domain.py`、`core/source.py` | 併進 `core/plugin.py` | 兩個都是 plugin 的門面 |
| `core/config_models.py`（716 行，55 個類別） | 拆回各功能：`sources/config.py`（`Source`、`Store`、`Provider`）、`detector/baseline/config.py`（`Train`、`Detect`、`Status`、`Adaptive*`、`Group`、`Borrowing`、`Shift`、`Series`、`SeriesConfig`）、`detector/rules/config.py`（`Join`、`Rule`、`RulesConfig`）、`detector/adjust/config.py`（`RecurringSchedule`、`RecurringEffect`、`Adjustment`、`AdjustmentsConfig`、`CalendarEntry`、`CalendarConfig`）、`detector/prune.py` 旁的 `PruneIndex`／`PruneConfig`、`investigator/rca/config.py`（`Indices`、`Trend`、`Commonality`、`Changes`、`SeverityClass`、`SeverityCfg`、`Alerts`、`IncidentsDb`、`RcaConfig`）、`scheduler/config.py`（`Job`、`ScheduleConfig`）；IT 與工廠的（`Peer*`、`Quality*`、`Trace*`／`Merge`／`MetricRule`、`Chronic*`、`Pick`／`Input`／`IngestConfig`、`Mes*`／`Events*`／`PathsConfig`）先放到階段 C 的目標資料夾 `plugins/observability/{peer,quality,trace,chronic}/config.py`、`plugins/factory/{ingest,paths}/config.py`，只放模型 | 設定模型跟它的功能住一起；`Strict` 基底留在 `core/config.py` |
| `core/config.py` | 只留通用 `load(path, Model)` 與錯誤格式；`load_config(path, kind)` 與 `KINDS` 刪（註冊表會讓 core 認得上層） | 呼叫端（`detector/__main__`、`investigator/__main__`、`api/app.py`、`scheduler/runner.py`、`adjust.load`、`cmd_paths`）直接給模型類別 |
| `models/`（6 檔） | 刪。`check_*`／`fit_and_score`／`points_of`／`samples_of` 併進對應功能：`detector/baseline/model.py`、`detector/rules/engine.py`、`investigator/similar/similar.py`、`detector/peer.py`、`detector/trace.py`；`tests/test_models.py` 改 import | 門面只有測試在用；同名檔的來源 |

### 階段 C 的搬移對照表（IT 專用進 `plugins/observability/`、工廠專用進 `plugins/factory/`）

「情境詞」的定義在階段 C 開始前改成**只數識別字**（函式、類別、變數、參數、屬性的名字，拆 snake_case 比對）；字串與註解不算，因為 `al["service"]` 這種 ES 欄位名是對外契約（Grafana 規則用它）、CLI 範例與說明是文件，不是程式認得情境。字串／註解裡的次數另外印出當參考。改定義後：識別字違規 19 檔（`rca/report.py` 71、`rca/queries.py` 38、`detector/trace.py` 19、`severity.py` 12、`analyze.py` 12…），字串／註解 37 檔。

| 現在 | 新位置 | 說明 |
|---|---|---|
| `detector/peer.py`、`cmd_peer.py` | `plugins/observability/peer/engine.py`、`detector.py`（`PeerDetector`、`cmd_peer`） | `command.py`、`config.py` 階段 B 已在 |
| `detector/quality.py`、`cmd_quality.py` | `plugins/observability/quality/engine.py`、`detector.py` | |
| `detector/chronic.py`、`cmd_chronic.py` | `plugins/observability/chronic/engine.py`、`job.py` | |
| `detector/trace.py`、`trace_merge.py`、`cmd_trace.py` | `plugins/observability/trace/engine.py`、`merge.py`、`job.py` | `sources/traces.py`（格式解析）留資料層 |
| `detector/ingest.py`、`cmd_ingest.py` | `plugins/factory/ingest/engine.py`、`job.py` | `api/app.py` 的 `/api/anomalies` 改 import 新家（api 是入口層，可 import plugins） |
| `investigator/rca/analyze.py` | 拆：trace 的（`SegmentDiff`、`compare_segments`、`waterfall`、`Edge`、`topology`、`mermaid`）→ `plugins/observability/trace/analyze.py`；log 樣式與共同點（`log_pattern`、`PatternDiff`、`Common`…）留 `rca/analyze.py` | |
| `investigator/rca/queries.py` | 拆：入口請求、取樣 trace 的查詢 → `plugins/observability/rca/queries.py`；`anomalies()` 這種中立的留 | |
| `investigator/rca/report.py` | 拆：段落函式裡 IT 的（`entries`、`segments`、`slowest`、`topology`）→ `plugins/observability/rca/sections.py`；工廠的（履歷）→ `plugins/factory/rca/sections.py`；`report.py` 留框架（`Ctx`、`pick_reference`、組報告）與中立段落（`anomalies`、`logs`、`changes`、`conclusion`）。`Plugin` 加 `report_sections() -> dict[名, callable(ctx)]`，`sections` 順序照舊由 plugin 給 | 報告的段落本來就是 plugin 的 `sections` hook，只是函式還住在共用區 |
| `investigator/alerts/severity.py` | 黃金訊號的預設分類表 `CLASSES` → `plugins/observability/profiles.py`；基底 `Plugin.severity_classes` 不再預設 IT 的 | |
| `investigator/alerts/merge.py`、`command.py` | 識別字裡的 `service`／`lot` 改中立（`entity`、`unit`），折疊邏輯本來就經 `plugin.chain()` | |
| `core/llm.py`、`core/plugin.py`、`detector/baseline/*`、`adjust/backtest.py`、`feedback/command.py`、兩個 `__main__.py` | 識別字改中立：`route_of` → `where_of`（哪支 API／哪個 recipe）、`_service_of` → `_entity_of`、CLI 參數的 Python 名改中立但旗標字串（`--service`、`--route`）不變 | 旗標是對外契約 |

- **範圍**：上面全部；行為、ES 查詞、異常文件欄位、CLI 參數、HTTP 端點不變；不做預設值集中（`cfg.get` 153 處）、不做檢查種類各一個類別（那是「每個資料夾裡面」的事，分層切好之後才有地方放）。

## Do：改動哪些 code、改了什麼、為何要改

### 階段 A：解循環（已完成）

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `tools/check-layering.py`（新） | 量五項：往上 import、模組層循環、函式內 import 內部套件、同名 `.py`、`core`／`detector`／`investigator` 的情境詞。規則寫在檔頭（`ALLOWED`＝誰能 import 誰、`ALLOWED_INNER`、`MODULE_EXCEPTIONS`） | 驗收標準要能量，不靠感覺；規則在階段 A 沒有為了通過而改 |
| `sources/schema.py`（新） | 所有 `aiops-*` 索引名與 mapping 一份：baseline、anomalies、tuning、incidents、feedback、alerts、chronic、segments，加 `RULE_FIELDS`、`INGEST_FIELDS`、`STAMP_LAG`／`stamp_of`、`BASELINE_COLUMNS`。舊模組用 `from sources.schema import … # noqa` re-export，既有 import 與測試不用改 | `sources` 與 `detector`／`investigator` 之間的循環，根源是「索引的定義放在上層、下層要用」；定義放最底下，大家往下取 |
| `sources/es_aggs.py`（新） | `_value_aggs`、`_bucket_value`、`_bucket_events`、`value_kind` 從 `detector/es.py` 搬來 | `sources/es_source.py` 要用這些，原本向上 import `detector.es` |
| `core/traces.py` → `sources/traces.py` | span 格式解析與純函式整個搬到 sources；`spans_of`／`spans_by_id` 放在 `sources/__init__.py`（要 `as_backend`，放 `traces.py` 會繞圈）。所有使用者改指向新家 | `core` 不該依賴 `sources`，而 trace 格式解析本來就是資料層的事 |
| `as_backend` 從 `sources/base.py` 搬到 `sources/__init__.py` | `from sources.base import as_backend` 全改成 `from sources import as_backend` | `base` ↔ `es_source` 的循環唯一解法 |
| `detector/owner.py`、`sources/base.py`、`sources/stores.py` | `Store.write_anomalies` 只負責寫（基底給預設實作）；dept／group／`engine_version` 的補值回到 `detector/owner.py::write_anomalies`，用 `as_backend(x).raw_req` 查負責人，非 ES 來源丟 `NotImplementedError` 就當空表、全部 `unassigned` | `sources` 不該 import `detector.owner`。副作用見 Act 第 1 列 |
| `Store.similar_incidents(..., query_builder=None)`、`investigator/similar.py` | 查詞函式由 `investigator` 傳進去，`ESStore` 只負責執行 | `sources` 不該 import `investigator.similar` |
| `core/llm.py`、`investigator/cmd_alert.py` | `build_pack(..., classification=None)`：分類結果由呼叫端傳進來，`core` 不再 import `investigator.similar`。`llm.py` 沒搬（`detector/cmd_feedback`、`api` 也用它） | `core` 不該依賴 `investigator` |
| 41 處函式內 import | 全部提到模組頂；兩處 `from . import x` 改絕對寫法（`sources/rows.py`、`plugins/factory/cmd_paths.py`） | 函式內 import 是躲循環的徵兆，循環解開後就不需要 |
| 文件與註解 | `core/traces.py` → `sources/traces.py` 的路徑文字（`architecture.md` 3 處、`apm/README.md`、`apm/trace.yaml`、`apm/rca.yaml`、幾個 docstring） | 不留指向已刪檔的說明 |
| 測試（只改 mock 目標或傳參） | `test_cmd_rules`、`test_plugin`、`test_cmd_alert`、`test_llm`、`test_es_backend`、四個 trace 測試 | 名字綁定的位置變了 |

### 階段 B：一個功能一個資料夾（已完成）

照上面的搬移對照表做，153 個檔 +877／−898；全部用 `git mv`（27 個 R、34 個新檔、11 個刪）。跟對照表不同的地方：

| 不同 | 為什麼 |
|---|---|
| `Detector` 放 `detector/detector.py`，不是 `core/` | 它要 `sources.open_backend`、`sources.schema`、`detector.owner`，放 L0 一定往上 import；`Anomaly` 照表放 `core/anomaly.py` |
| `borrow_resolution`、`min_events` 放 `baseline/train.py` | train 與 detect 都用，放 detect 會互相 import |
| `to_freq` 搬到 `core/timeutil.py` | `_freq` 在 core，不能回頭 import detector |
| `plugins/factory/paths.py`、`cmd_paths.py` 提前搬進 `plugins/factory/paths/{build,command}.py` | 套件 `paths/` 與模組 `paths.py` 同名，Python 會指到套件 |
| peer／quality／chronic／trace-*／ingest 的 typer 包裝放各功能資料夾的 `command.py`，plugin 的 `commands()` 回 `{名: register}` | `detector/__main__` 不能 import `plugins`（分層）；引擎本體階段 C 才搬 |
| 新增 `detector/cli.py`（`Opt`、`run(handler, Model, params)`） | 內建與 plugin 的子命令共用，plugin 不必 import `detector.__main__` |
| `core/domain.py`、`core/source.py` 併進 `core/plugin.py` 時函式改名（`categories()`、`entity_label()`、`source_profile(section)`…） | 避免撞名 |
| `load_config(path, kind)` → `load(path, Model)`，`KINDS` 刪 | 註冊表會讓 core 認得上層；呼叫端直接給模型類別 |

文件：`Dockerfile` 刪 `COPY models/`；`CLAUDE.md` 結構表刪 `models/` 一項；`aiops-engine/README.md`、`docs/README.md`、`sources/README.md`、`plugins/README.md`、`docs/semiconductor-design.md` 的路徑與連結對齊；PDCA 紀錄只改壞掉的連結目標。

### 階段 C：plugin 邊界（已完成）

照對照表搬（全部 `git mv`）：peer／quality／chronic／trace／trace_merge 進 `plugins/observability/<功能>/`，ingest 進 `plugins/factory/ingest/`；`cmd_*` 併進各功能的 `command.py`（引擎＋`cmd_*`＋typer 包裝同一檔）。跟對照表不同、要知道的：

| 不同 | 為什麼 |
|---|---|
| `rca/analyze.py`、`rca/queries.py` **沒搬**進 observability，識別字改中立；只有 `service_trace_ids` 搬去 observability 的 `chain.py` | 工廠現在就用同一套：履歷由同一種「合成一筆的 path」格式表示，`compare_segments` 比壞 lot／好 lot。搬進 observability 會讓 factory 依賴 observability（對稱刪除就壞），或複製約 250 行 |
| `rca/report.py` 只剩框架與中立四段（anomalies、logs、changes、conclusion）；有 path 才有的四段（entries、segments、slowest、topology）放 **`rca/path_sections.py`**，識別字中立；`Plugin.report_sections()` 回 `{段名: 函式}`，observability 與 factory 都回它、metrics_only 回 `{}` | 段落本來就是 plugin 的 `sections` hook，只是函式住在共用區 |
| `trace_merge.build`／`is_entry` → `sources/traces.merged_doc`／`is_entry` | IT 的 trace-merge 與工廠的 paths-merge 共用同一個合成格式，放資料層才不會讓 factory 依賴 observability |
| 黃金訊號分類表 `CLASSES` → `plugins/observability/profiles.py`；`PROFILES`、`DOMAIN`、`ENTITY_KEYS`、`SCHEDULES` 也進去，`__init__.py` 只剩 Plugin 類別 | 基底 `Plugin.severity_classes=None` 現在真的是「不分類」 |
| 中立碼的識別字改名：`route_of`→`where_of`、`trace_label`→`path_label`、`SERVICE_RE`→`ENTITY_RE`、`_service_of`→`_entity_of`、`Ctx.service/route`→`entity/where`、`has_traces`→`has_paths`、`sample_trace_ids`→`sample_path_ids`、`confirm_span=`→`confirm_within`、局部 `lot`→`unit`、`span`→`within`／`spread`… | 四個中立字：`entity`（服務／機台）、`unit`（請求／lot）、`path`（trace／履歷）、`where`（API／recipe），寫在 `core/plugin.py` 檔頭。**沒改**：ES 欄位名、文件 key、CLI 旗標（`--service`、`--route`）、YAML key、HTTP 端點 |

### 階段 D

（進行中）

## Check：測試的目的與結果

### 階段 A

`PYTHONUTF8=1 py -m pytest -q` → 769 個通過（跟開始前一樣，沒有退步）；`py -W error -c "import api.server, detector.__main__, investigator.__main__, scheduler.__main__, tasks"` 沒有警告；`tools/check-docs.py` 四項 0 個問題。

| 指標 | 開始 | 階段 A 後 | 階段 B 後 | 目標 |
|---|---|---|---|---|
| 往上 import（違反分層） | 6 | 0 | 0 | 0 ✓ |
| 模組層循環 | 8 | 0 | 0 | 0 ✓ |
| 函式內 import 內部套件 | 41 | 0 | 0 | 0 ✓ |
| 同名 `.py` | 6 | 6 | 0 | 0 ✓ |
| 情境詞 | 39 | 38 | 37 | 階段 C |
| 非測試總行數 | 12,286 | 12,313 | 12,412 | — |

階段 B：`pytest` 768 個通過（少 1 是 `test_config` 刪了兩個測 `KINDS` 的、加一個 `test_not_a_mapping`，其餘只改 import 與 mock 目標）；`--help` 內建 11 個子命令＋observability 7 個、factory 2 個、metrics_only 1 個，跟以前一樣；`check-docs` 四項 0。

沒測到的：沒連真的 ES、沒在 k3d 跑。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [⑤] 行為有兩處差異：`ESSourceBackend` 搭配 file／memory store 現在也會從 ES 查 dept／group（以前全部 unassigned）；`MemoryStore` 以前 `setdefault`、現在一律覆寫 dept／group | 階段 A 把補值搬回 `owner.py` 時 | 只影響「來源是 ES、結果存檔案」這種離線實驗組合 | 跟 ES 路徑一致，不處理 |
| `detector/sources.py` 沒搬成 `sources/providers.py` | 它不 import 任何內部套件，不是循環的一部分 | 檔名 `sources.py` 與套件 `sources/` 仍容易混 | 階段 B 一併改名 |
| `check-layering` 把 `from . import x` 視為 import 套件 `__init__` | 階段 A 修循環時 | 同套件內用這種寫法、`__init__` 又 import 該模組，會算循環 | Python 層面確實是循環（靠部分初始化才跑得過），改絕對 import 是誠實的修法 |
| 同名 `.py` 6 組、情境詞 38 處 | 計畫就分階段 | 內聚的部分還沒做 | 階段 B、C |
| 沒連真的 ES | 只有單元測試 | 搬過去的查詢沒對 ES 跑過 | 全部階段做完後在 k3d 跑一輪 |

