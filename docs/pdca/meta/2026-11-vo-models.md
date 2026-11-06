# 資料物件化（VO）：把在函式之間流動的 dict 改成有型別的類別（2026-11）

**狀態**：進行中（只有計畫與盤點，還沒動 code）　　**日期**：2026-11-06　　**相關**：分支 `claude/vo-models`（從 master `31c72f8` 開）；接 [oop-core](2026-11-oop-core.md)（只做了 `Anomaly`）
**模組**：④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：使用者問「有沒有像 Java 的 VO、getter／setter」。目前 `aiops-engine/code/` 非測試的 .py 約 12,300 行，除了 `Anomaly`、設定的 pydantic 模型和約 11 個 dataclass，**資料都以 dict 在函式之間傳**（用 grep 粗數：`["欄位"]` 與 `.get("欄位")` 約 1,764 處）。欄位名稱與型別沒有任何東西約束，打錯字要到執行時才發現；同一個形狀（例如指紋）在 `similar.py` 與 `importer.py` 各寫一份，靠註解「跟 xxx 同一個形狀」維持一致。
- **目的**：在流動的 dict 裡，挑出**有固定欄位、跨檔案傳遞**的，改成 pydantic 模型（或 dataclass）。做完要能：
  1. 欄位只定義一處，讀寫 ES 的 dict 由模型的 `to_doc()`／`from_doc()` 產生與解析（跟 `Anomaly.to_doc()` 同一個做法）。
  2. 打錯欄位名稱在建立物件時（或 IDE／型別檢查）就報錯，不是執行到一半才 `KeyError`。
  3. ES 文件的格式、API 回應、報告文字都**不變**（對外行為零差異）。
- **範圍**：
  - 做：下面「盤點」表 P0／P1 的模型。
  - 不做：pandas DataFrame（baseline 的訓練與評分，是欄式計算，不是 VO）；ES query 的 body（每次不同的 DSL，不是領域物件）；`cfg` 設定（已是 pydantic，`core/config_models.py`）；getter／setter 方法（Python 用屬性，需要驗證或計算才用 `@property`）；檢查種類拆成類別（oop-core 已列為不做）。

## 盤點：流動中的 dict 與建議

方法：讀 `investigator/alerts.py`、`similar.py`、`importer.py`、`report.py`、`analyze.py`、`core/traces.py`、`core/llm.py`，以及 `detector/feedback.py`、`tuning.py`、`cmd_feedback.py`、`cmd_alert.py` 的函式簽名與文件建構處，**欄位以程式裡建 dict 的地方為準**。`detector/` 其他檔案（`cmd_baseline`、`adjust`、`trace`、`quality` 內部）和 `plugins/` 只看了函式簽名，尚未逐行確認，列在「待確認」。

優先序：P0 ＝ 跨三個以上檔案、欄位多、錯了會寫壞 ES；P1 ＝ 跨檔案但欄位少或只在一條路徑上；P2 ＝ 範圍小，有餘力再做。

### P0

| # | 現在是 | 欄位（來源） | 在哪些檔案流動 | 建議 |
|---|---|---|---|---|
| 1 | `Alert`：一件告警，`dict`，寫進 `aiops-alerts` | `alert_id`、`category`、`service`、`status`、`started_at`、`updated_at`、`resolved_at`、`anomaly_ids`、`series`、`routes`、`lots`、`upstream_services`、`severity`、`merged_into`、`dept`、`group`、`minutes`；另夾 **執行期私有欄位** `_anoms`、`_new`、`_upgraded`、`_rerun`（`alerts._new/_attach/_fold`） | `alerts.py`、`cmd_alert.py`、`similar.py`、`llm.py`、`severity.py`、`detector/feedback.py`、`cmd_feedback.py` | pydantic 模型；`_anoms` 等私有欄位改成非序列化欄位（`exclude=True`），取代 `cmd_alert.py:341` 的 `startswith("_")` 過濾。方法 `attach(anomaly)`、`fold_into(other)`、`headline()`、`owner()` 收進類別（原本是 `alerts.py` 的 `_attach`、`_fold`、`headline`、`owner` 函式） |
| 2 | `Incident`：事故紀錄，`dict`，寫進 `aiops-incidents` | `title`、`start`、`end`、`services`、`status`（auto／open／confirmed／corrected／merged／resolved）、`source`、`detected_at`、`resolved_at`、`root_cause_category`、`root_cause`、`action`、`how_detected`、`checked`、`fingerprint`、`alert_id`、`dept`、`group`、`anomaly_ids`、`merged_into`、`confirmed_by`、`by`（`importer.normalize`、`cmd_alert.py:149`、`cmd_feedback.cmd_incident`） | 三個寫入者（匯入、alert 自動開、`incident` 指令）形狀各自手寫，加上 `similar.py`、`feedback.suggest_rules` 讀 | pydantic 模型 + `status` 用 `Literal`；三個寫入者都經它的 `to_doc()`，欄位只定義一處 |
| 3 | `Fingerprint`：指紋，`dict` | `services`、`routes`、`series`、`slow_segments`、`error_origins`、`patterns`（全是 `list[str]`） | `similar.fingerprint` 與 `importer.fingerprint` **各寫一份**，註解寫「跟 similar.fingerprint 同一個形狀」；`similar.query/explain/rank` 讀；`Incident.fingerprint` 內嵌 | pydantic 模型；`from_alert(alert, findings)` 與 `from_row(row, services)` 兩個建構方法，去掉重複。權重表 `WEIGHTS` 的欄位名改由模型欄位推導 |
| 4 | `Span`：一個 span，`dict`，`to_span`／`_apm_span`／`from_merged` 三處各手寫一遍 | `trace_id`、`span_id`、`parent`、`service`、`name`、`kind`、`target`、`start_ms`、`dur_ms`、`error`、`exception`、`links`、`attrs`、`resource`（`core/traces.py`） | `core/traces.py`、`detector/trace.py`、`trace_merge.py`、`investigator/analyze.py`、`report.py`（`list[dict]` 的 trace 在 analyze 到處傳） | dataclass（量大、要快：`frozen=True, slots=True`）；三個轉換函式改成 `Span.from_apm/from_otel/from_merged`。`Trace` = `list[Span]` 加 `root`、`self_times()`、`error_origins()`（現在是 `traces.py` 的 `root_of`、`self_times`、`error_origins` 函式） |
| 5 | `Findings`：根因分析的結果，`dict`，`Ctx.findings` 逐段填 | `service`、`route`、`start`、`end`、`ref_label`、`anomalies`、`segments`、`error_origins`、`new_patterns`、`topology`、`requests`、`trend`、`commonality`、`changes`（`report.Ctx.__init__`） | `report.py` 產生；`similar.fingerprint`、`llm.build_pack`、`cmd_alert`（存 ES）、`alerts.dependencies` 讀；巢狀項目（segments 的 `segment`／`now_ms`／`ref_ms`／`delta_ms`，error_origins 的 `now_errors`／`ref_errors`，new_patterns 的 `pattern`／`is_new`／`now`／`ref`）也都是 dict | pydantic 模型，巢狀項目各一個子模型。`analyze.py` 裡已有 dataclass `SegmentDiff`、`PatternDiff`、`Common`、`Edge`，但在 `report.py` 又被轉成 dict 放進 findings，這層「物件 → dict」的來回要消掉 |

### P1

| # | 現在是 | 欄位 | 在哪些檔案流動 | 建議 |
|---|---|---|---|---|
| 6 | `Feedback`：值班回饋，寫進 `aiops-feedback` | `@timestamp`、`note`、`by`、`incident_id`、`correct_root_cause`，另有 `feedback_type`、`identified_cause`、`target_service`、`suggested_action`（`cmd_feedback.py:249` 起；`core/llm.validate_extraction`） | `cmd_feedback.py`、`tuning.propose_from_text`、`llm.py`、`feedback.suggest_from_text`、Grafana 表單 | pydantic 模型；四欄抽取結果用子模型，`validate_extraction` 的手寫檢查改成模型驗證（`Literal` 取代 `FEEDBACK_TYPES`／`SUGGESTED_ACTIONS` 集合） |
| 7 | `Adjustment`／`Suggestion`：調整建議與已核准調整，寫進 `aiops-tuning` | `kind`、`status`（proposed／approved…）、`source`、`expires_at`、`match`、`series`、`z`、`at`、`by`（`tuning.py` mapping；`feedback.suggest_*` 回傳；`adjust.problem/describe`） | `feedback.py`（8 個 `suggest_*`）、`tuning.py`、`adjust.py`（**注意**：`core/config_models.py` 已有 pydantic `Adjustment`，是設定檔那一邊） | 先確認設定檔的 `Adjustment` 與 ES 裡的調整文件是不是同一個形狀；是就合併成一個模型，不是就另開 `TuningDoc`。`suggest_*` 的回傳 dict 統一成它 |
| 8 | `SimilarHit`：相似事故檢索的一筆結果 | `incident_id`、`score`、`why`、`start`、`root_cause_category`、…（`similar.rank`／`explain`） | `similar.py`、`llm.build_pack`、`alerts.summary` | dataclass；`Classification`（`category`、`confidence`、`votes`、`n`、`based_on`，`similar.classify`）一併做 |
| 9 | `Trend`：`classify_trend` 回傳 | `p95_ms`、`now_p95_ms`、`kind`（gradual／sudden／mixed／unknown） | `analyze.py`、`report.py`、`findings["trend"]` | dataclass，`kind` 用 `Literal`／`Enum` |
| 10 | `LlmPack`／`Summary`：`llm.build_pack` 的輸入包、`summarize` 的回傳 | `alert`、`anomalies`、`segments`、`error_origins`、`new_patterns`、`similar`、`classification`、`trace_ready`、`_ids` | `core/llm.py` 一個檔案 | pydantic；順手讓 `build_pack` 吃 `Alert`、`Findings`，不再靠 `.get` |
| 11 | `Owner`：`(dept, group)` 的 tuple，`owner_of`／`alerts.owner` 回傳 | `dept`、`group` | `detector/owner.py`、`alerts.py` | 小 dataclass（`NamedTuple` 即可），取代 `tuple[str | None, str]` |

### P2（範圍小）

| # | 現在是 | 建議 |
|---|---|---|
| 12 | `Entity`：`alerts.entity_of` 回傳 `(category, name)` 的 tuple | `NamedTuple` |
| 13 | `Job`、`Cron`（`scheduler/`）、`TrainOptions`、`DetectOptions`、`GradeOptions`、`DbRows`、`Common`（已是 dataclass） | 不動，只在風格上補 `frozen`／`slots` 時順手改 |
| 14 | 序列的 `keys: dict`（異常、baseline、feedback 都有） | **不改成類別**：欄位隨序列設定而變（service、pod、route…），固定型別會把彈性砍掉。保留 `dict`，但把 `keys_of`、`key_value`、`target_of`（`common.py`、`adjust.py`、`feedback.py`）收成一組函式 |

### 待確認（還沒逐行讀，開工前先盤）

`detector/cmd_baseline.py`（466 行）、`detector/trace.py`、`detector/quality.py`、`detector/cmd_backtest.py`（backtest 結果）、`detector/cmd_metrics.py`（KPI 結果）、`detector/chronic.py`（慢性病清單）、`plugins/factory/`（履歷 `paths`、`changes`）、`api/` 的請求與回應（FastAPI 該用 pydantic response model，目前是否已用未確認）。

## Do：改動哪些 code、改了什麼、為何要改

這一輪只寫計畫，**無 code 改動**。實作預計的順序與切法（每一步獨立一個 commit，單元測試通過再下一步）：

| 步驟 | 內容 | 為何這個順序 |
|---|---|---|
| 1 | 新增 `core/models.py`（或 `models/` 底下，二選一，開工時看哪個不撞名）：`Fingerprint`、`Incident`，兩個寫入者（`importer`、`cmd_alert`、`cmd_feedback`）和 `similar` 改用 | 指紋重複最明顯，風險最低，先驗證 `to_doc()`／`from_doc()` 的做法 |
| 2 | `Alert`（含私有欄位處理）、`Owner` | `alerts.py` 純計算、單元測試最完整，改完立刻有保護網 |
| 3 | `Span`／`Trace` | 影響 `analyze`、`report`、`detector/trace`，量最大；放在 `Alert` 之後，因為 dataclass 的效能要先量過 |
| 4 | `Findings` 與巢狀子模型；消掉 analyze 的物件 → dict 來回 | 依賴 3 |
| 5 | `Feedback`、`Adjustment`、`SimilarHit`、`Trend`、`LlmPack` | 依賴 1、4 |
| 6 | 待確認那幾個檔案盤點後補進這份 | |

做法的約定（先定下來，避免每一步各寫各的）：
- 讀 ES 回來的文件用 `Model.from_doc(src)`，寫進 ES 用 `model.to_doc()`；`to_doc()` 要跟現在寫進去的 dict **逐欄位相同**（含「值是 None 就不寫」這種現有行為），不改 mapping。
- 時間欄位型別現在是「有時 `datetime`、有時 ISO 字串」混用（`alerts._ts` 到處轉）。模型內統一成 `datetime`，只在 `to_doc()` 轉字串。
- 不加 getter／setter 方法。需要計算的用 `@property`。

## Check：測試的目的與結果

尚未實作，以下是**預計要驗證的**：

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 改成模型後，寫進 ES 的文件跟改之前**逐欄位相同** | 每個模型：用既有單元測試的輸入，改前先把 dict 輸出存成 golden，改後比對（`Alert`、`Incident`、`Fingerprint`、`Feedback`） | 未做 |
| 模型的欄位跟 ES mapping 一致（異常已有這個測試：`tests/test_detector_base.py`） | 每個寫 ES 的模型各一個測試：模型欄位集合 == mapping 欄位集合 | 未做 |
| 錯誤的欄位名稱或型別在建立物件時被擋 | 每個模型一個負向測試（缺必填、`status` 給不存在的值） | 未做 |
| 用既有文件讀回來不炸（歷史資料不能因為模型太嚴格讀不進來） | `from_doc` 餵 `tests/` 與 `e2e/` 現有的樣本文件，另外餵缺欄位的舊格式 | 未做；**這是最大風險**：舊文件缺欄位時模型要給預設值，不能直接丟例外 |
| `Span` 改 dataclass 後不比 dict 慢太多 | 拿 `core/traces.py` 現有測試的 trace，量 `group_traces`／`self_times` 前後耗時 | 未做 |
| 整體行為沒變 | `cd aiops-engine/code && python3 -m pytest -q`；`python3 tools/check-docs.py` | 未做 |

沒測到的：連真的 ES 跑（oop-core 也沒連）；Grafana 面板讀改過的文件。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 盤點只讀了 investigator 與 core 的主要檔案，detector 其他檔案和 plugins 沒逐行讀 | 寫這份計畫時為了控制範圍 | 「所有 dict」的清單可能不完整，P0／P1 以外還有漏的 | 開工第 6 步前先補完「待確認」那幾個檔案 |
| 時間欄位 `datetime`／ISO 字串混用 | 讀 `alerts.py` 發現 `_ts(...)` 到處轉換 | 模型化時要統一，容易漏掉某條路徑還在傳字串 | 步驟 2 處理 `Alert` 時一併處理，並加測試 |
| 設定檔的 `Adjustment`（`core/config_models.py`）與 `aiops-tuning` 的調整文件可能是兩個形狀 | 讀 `tuning.py` mapping 時看到同名 | 合併或分開的決定會影響 `adjust.py` 與 `feedback.py` | 步驟 5 開工前先比對欄位 |

- **決定**：用 pydantic 做寫進 ES 的文件（要驗證、要序列化），用 dataclass 做量大的內部物件（`Span`）；不加 getter／setter，Python 慣例是直接用屬性。
- **下一輪**：照上面步驟 1 開始；完成後更新 [docs/pdca/README.md](../README.md)。
