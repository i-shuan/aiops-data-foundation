# 資料物件化（VO）：把在函式之間流動的 dict 改成有型別的類別（2026-11）

**狀態**：進行中（步驟 1 `Fingerprint`、`Incident` 做完；步驟 2～6 沒做）　　**日期**：2026-11-06　　**相關**：分支 `claude/vo-models`（從 master `31c72f8` 開）；接 [oop-core](2026-11-oop-core.md)（只做了 `Anomaly`）
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

實作的順序與切法（每一步獨立一個 commit，單元測試通過再下一步）：

| 步驟 | 內容 | 為何這個順序 |
|---|---|---|
| 1 ✅ | 新增 `core/incident.py`（不叫 `core/models.py`：`models/` 已是五個演算法模型的門面，會混）：`Fingerprint`、`Incident`，四條寫入路徑（`importer`、`cmd_alert`、`cmd_feedback`、`api/app` 的 upsert 種子）和 `similar` 改用 | 指紋重複最明顯，風險最低，先驗證 `to_doc()`／`from_doc()` 的做法 |
| 2 | `Alert`（含私有欄位處理）、`Owner` | `alerts.py` 純計算、單元測試最完整，改完立刻有保護網 |
| 3 | `Span`／`Trace` | 影響 `analyze`、`report`、`detector/trace`，量最大；放在 `Alert` 之後，因為 dataclass 的效能要先量過 |
| 4 | `Findings` 與巢狀子模型；消掉 analyze 的物件 → dict 來回 | 依賴 3 |
| 5 | `Feedback`、`Adjustment`、`SimilarHit`、`Trend`、`LlmPack` | 依賴 1、4 |
| 6 | 待確認那幾個檔案盤點後補進這份 | |

做法的約定（先定下來，避免每一步各寫各的）：
- 讀 ES 回來的文件用 `Model.from_doc(src)`，寫進 ES 用 `model.to_doc()`；`to_doc()` 要跟現在寫進去的 dict **逐欄位相同**（含「值是 None 就不寫」這種現有行為），不改 mapping。
- 時間欄位型別現在是「有時 `datetime`、有時 ISO 字串」混用（`alerts._ts` 到處轉）。模型內統一成 `datetime`，只在 `to_doc()` 轉字串。**步驟 1 例外**：`Incident` 的時間欄位三條寫入路徑拿到的本來就都是 ISO 字串（`local_iso`、`cmd_alert` 寫 alert 前已 `isoformat()`、表單帶的是 ES 讀回的字串），這一步留 `str`；pandas 的 `isoformat()` 帶奈秒時 pydantic 的 `datetime` 不一定收，轉的時候要另外測。
- 不加 getter／setter 方法。需要計算的用 `@property`。
- `to_doc()` 用 pydantic 的 `exclude_unset`＋`exclude_none`：**只寫建構時有給的欄位**。第一版用「預設值也寫」，匯入的事故會多出 `anomaly_ids: []`、`upstream_services: []`、`checked: []` 三個它從來沒寫過的欄位（test_incident 的 golden 比對抓到的）；改成 `exclude_unset` 後 `from_doc` → `to_doc` 也變成忠實的來回。

### 步驟 1 改了什麼

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `core/incident.py`（新，221 行） | `Doc` 基底（`extra="forbid"`、`from_doc` 過濾不認得的鍵再驗證、`to_doc` 只寫有給的欄位）；`Fingerprint`（六個 `list[str]`，`normalized()` 是去數字、截 3／5／120 字的唯一一處，`from_alert()`、`of()`、`empty`）；`Incident`（27 個欄位，`status` 是 `Literal` 七個值，`from_alert()` 取代 `cmd_alert._incident_doc`）；`INCIDENT_MAPPING`、`INCIDENT_INDEX`、`UNMAPPED_FIELDS` 從 `detector/feedback.py` 搬來 | 欄位只定義一處；mapping 跟模型放一起才對得起來（`Anomaly` 跟 `ANOMALY_MAPPING` 同一個做法）。放 `core/` 是因為 detector（feedback、cmd_feedback）、investigator、api 都用 |
| `investigator/similar.py`（−8 行） | `fingerprint()` 回 `Fingerprint`；`query`／`explain`／`rank`／`search` 開頭 `Fingerprint.of(fp)`，收模型或同形狀 dict；`rank` 用 `Incident.from_doc` 讀候選；刪 `_seg_name`（留別名指到 `core.incident.seg_name`） | 讀 ES 候選的路徑走 `from_doc`，是「舊文件不能讀不進來」唯一真的在跑的地方；收 dict 是給 `models/similar.py` 門面與 Store 的介面留相容 |
| `investigator/importer.py`（−9 行） | `normalize()` 建 `Incident` 再 `to_doc()`；`fingerprint(row, services)` 改呼叫 `Fingerprint.normalized`，刪掉自己那份「跟 similar 同一個形狀」的 dict | 兩份指紋寫法合成一份；`_id`（`doc_id("import", title, start, services)`）不變，重匯還是覆蓋同一筆 |
| `investigator/cmd_alert.py` | 刪 `_incident_doc`，兩處改 `Incident.from_alert(al, doc, findings[, status="merged", merged_into=…]).to_doc()`；`INCIDENT_INDEX`／`INCIDENT_MAPPING` 改從 `core.incident` 來 | `_incident_doc` 是第三份手寫的 incident dict |
| `detector/cmd_feedback.py` | `cmd_incident` 人工補登改建 `Incident(...).to_doc()`，刪 `{k: v … if v not in (None, "", [])}` 過濾 | 空字串由模型的 validator 當 None；第四份手寫 dict |
| `api/app.py` | upsert 的種子（`seed`）改 `Incident(...).to_doc()`；`INCIDENT_INDEX` 改 import | 第五份；順手修掉 `services: [None]`（告警沒 service 時）|
| `detector/feedback.py`（−15 行） | 刪 `INCIDENT_MAPPING` 本體與 status 註解，改 `from core.incident import …` re-export；`suggest_rules` 讀指紋改 `Fingerprint.of(...)` | 舊的 `F.INCIDENT_MAPPING`、`F.INCIDENT_INDEX` 用法（cmd_backtest、cmd_metrics、cmd_feedback）不用改 |
| `sources/base.py` | `similar_incidents(fp)` 拿掉 `fp: dict` 型別註記，docstring 說收 `Fingerprint` 或 dict | 介面本來就只是轉手 |
| `models/README.md`、`docs/aiops-data-reference.md` | `fingerprint` 回什麼；mapping 與欄位定義的位置改指 `core/incident.py` | 文件指的位置要對 |
| `tests/test_incident.py`（新，20 個測試） | golden、mapping、負向、from_doc（見 Check） | |
| `tests/test_similar.py`、`test_models.py`、`test_cmd_alert.py` | `fp["x"]` → `fp.x`、`{**fp, …}` → `{**fp.to_doc(), …}`、`C._incident_doc` → `C.Incident.from_alert` | 測試對齊新介面；斷言的值沒改 |

## Check：測試的目的與結果

步驟 1（`Fingerprint`、`Incident`）的結果；步驟 2 之後的欄位留著當待辦。

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 改成模型後，寫進 ES 的文件跟改之前**逐欄位相同** | 改之前（master `31c72f8`）用 8 組輸入跑 `similar.fingerprint`、`importer.normalize`、`cmd_alert._incident_doc` 存成 golden；改後用同樣輸入跑新程式，攤平比對每個鍵 | **指紋 3 組、匯入 3 組（含略過的錯誤訊息、`_id`）完全相同**。事件整合自動開的 2 組只差在 `dept: None`、`title: None` 以前會寫 null、現在不寫這個鍵——ES 對 null 與沒有欄位的查詢結果一樣，讀的程式都是 `.get()`。另外兩個已知差異：人工補登以前連 `[]` 也不寫，現在 `services: []`、`anomaly_ids: []` 會寫；表單種子的 `services: [None]` 現在是 `[]`。這些都寫成 `tests/test_incident.py` 的斷言 |
| 模型的欄位跟 ES mapping 一致 | `test_mapping_fields_are_model_fields`：mapping 的 24 個欄位都在模型裡；模型多出來的只能是 `UNMAPPED_FIELDS`（`upstream_services`、`merged_into`——以前就有寫、mapping 沒有，`dynamic: false` 存得下查不到） | 對得上。指紋六欄＝`similar.WEIGHTS` 的鍵＝mapping 的 `fingerprint.properties` |
| 錯誤的欄位名稱或型別在建立物件時被擋 | `Incident(titel=…)`、`status="confimed"`、建好後 `inc.status = "done"`、`Fingerprint(service=…)` | 四個都丟 `ValidationError`（`validate_assignment=True` 連賦值也擋） |
| 用既有文件讀回來不炸 | `from_doc` 餵：只有 `status` 的、`services` 是單值字串、`severity` 是數字、多一個不認得的鍵、`fingerprint` 是字串、`title` 是空字串、沒有 `@timestamp`（既有測試的假候選都沒有） | 都讀得進來：缺的給預設值、單值包成陣列、數字轉字串、不認得的鍵忽略、壞掉的指紋當 None。`@timestamp` 因此**不設必填**（寫入的四條路都會給，但讀的一端擋不得） |
| `from_doc` → `to_doc` 來回 | 匯入的一筆讀回再寫 | 跟原文件相同（`exclude_unset` 之後才成立） |
| 整體行為沒變 | `cd aiops-engine/code && python3 -m pytest -q` | 改之前 753 過；改完 773 過（多的 20 個是 test_incident）、16 skipped 同改前。`python3 tools/check-docs.py` 連結、索引、舊說法、類型 0 個問題 |
| `Span` 改 dataclass 後不比 dict 慢太多 | 步驟 3 | 未做 |

沒測到的：連真的 ES 跑（oop-core 也沒連）；Grafana 面板讀改過的文件；回饋網頁 `submit` 的種子只有單元測試的假 ES，沒在瀏覽器點過。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 盤點只讀了 investigator 與 core 的主要檔案，detector 其他檔案和 plugins 沒逐行讀 | 寫這份計畫時為了控制範圍 | 「所有 dict」的清單可能不完整，P0／P1 以外還有漏的 | 開工第 6 步前先補完「待確認」那幾個檔案 |
| 時間欄位 `datetime`／ISO 字串混用 | 讀 `alerts.py` 發現 `_ts(...)` 到處轉換 | 模型化時要統一，容易漏掉某條路徑還在傳字串 | 步驟 2 處理 `Alert` 時一併處理，並加測試 |
| 設定檔的 `Adjustment`（`core/config_models.py`）與 `aiops-tuning` 的調整文件可能是兩個形狀 | 讀 `tuning.py` mapping 時看到同名 | 合併或分開的決定會影響 `adjust.py` 與 `feedback.py` | 步驟 5 開工前先比對欄位 |
| `rank`／`search` 的回傳（`incident_id`、`score`、`why`、`title`…）、`classify` 的回傳還是 dict | 步驟 1 把 `rank` 讀候選改成 `Incident.from_doc` 時，輸出端沒一起改 | 下游 `llm.build_pack`、`alerts.summary`、`api.form_data` 仍用 `.get` | 步驟 5 的 `SimilarHit`、`Classification` |
| Store 的 `incidents()`／`similar_incidents()` 回 `(id, dict)`，不是 `Incident` | 改 `rank` 時：要回模型得動 `sources/` 三個 Store 與它們的測試 | 讀候選的 `from_doc` 在 `rank` 裡做，Store 層仍是 dict | 等 `Alert` 也進 Store 介面時（步驟 2）一起看要不要讓 Store 回模型 |
| `upstream_services`、`merged_into` 有寫、mapping 沒有 | 寫 mapping 對照測試時 | Grafana／查詢用不到這兩欄；要用得加 mapping 並重建索引 | 列在 `UNMAPPED_FIELDS`，等真的要查再加 |

- **決定**：用 pydantic 做寫進 ES 的文件（要驗證、要序列化），用 dataclass 做量大的內部物件（`Span`）；不加 getter／setter，Python 慣例是直接用屬性。
- **下一輪**：步驟 2 `Alert`（含 `_anoms`、`_new` 等私有欄位怎麼不進 `to_doc`，和時間欄位統一成 `datetime`）；完成後更新 [docs/pdca/README.md](../README.md)。
