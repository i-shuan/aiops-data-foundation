# 高內聚、低耦合：套件分層、一個功能一個資料夾、plugin 邊界切乾淨（2026-11）

**狀態**：進行中　　**日期**：2026-11-05　　**相關**：分支 `claude/layering`（從 master `31c72f8` 開）；接 [frameworks](2026-11-frameworks.md)、[oop-core](2026-11-oop-core.md)（兩輪都在加介面與框架，沒動套件怎麼切，使用者看完判斷「檔案太散、plugin 沒切乾淨」）
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

- **範圍**：上面全部；行為、ES 查詞、異常文件欄位、CLI 參數、HTTP 端點不變；不做預設值集中（`cfg.get` 153 處）、不做檢查種類各一個類別（那是「每個資料夾裡面」的事，分層切好之後才有地方放）。

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
