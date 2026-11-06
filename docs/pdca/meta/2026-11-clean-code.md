# 程式整理：先順主軸，再拆、再理依賴、再改型別（2026-11）

**狀態**：已完成（單元測試；沒連 ES）　　**日期**：2026-11-06　　**相關**：分支 `claude/clean-code`（從 `claude/vo-models` `d169924` 開，含 `Incident` 模型），四個 commit：A `25f2a78`、B `d446bba`、C `627b76c`、D `ec669b4`；取代 [vo-models](2026-11-vo-models.md) 的步驟 2～6
**模組**：②～⑨（程式結構，不改任何階段的行為）

## Plan：這一次要達到什麼

- **需求**：使用者看程式覺得「越來越發散、越來越多、很髒」。量過（`tests` 以外 85 檔、12,463 行、734 個函式）：
  1. **找不到**：`detector/` 31 檔同時放 ②③④⑤ 和整個 ⑨（feedback、tune、adjust、backtest、metrics、incident）；⑨ 又散在 `detector/`、`investigator/importer.py`、`api/` 三處；`aiops-incidents` 一個索引被三個資料夾各寫一份。文件用 ①～⑨，程式用 detector／investigator，兩套名字。
  2. **讀不懂**：`cmd_feedback` 168 行用 `if args.action ==` 塞 6 個命令；`cmd_alert.run` 117 行、巢狀 5 層；函式 >50 行 13 個、巢狀 ≥4 層 28 個；`detector/__main__.py` 的 `adjust` 24 個參數、`feedback` 18 個，用 `locals()` 整包丟成 `Namespace`。
  3. **改不動**：套件互相 import（`core`→`investigator`、`core`→`sources`、`sources`→`detector`／`investigator`、`detector`↔`investigator`），靠 71 處函式內 import 和「把函式當參數傳進來避免循環」硬解；`except Exception` 26 處。
  4. **沒型別**：`cfg` 是 dict 傳遍全專案（`cfg.get` 166 處）、`.get("欄位")` 1,431 處；只有 `Anomaly`、`Incident` 和設定是模型。
- **目的**（順序就是重要度，前一步不做後一步會白做）：
  - **A** 一個階段一個資料夾：⑨ 全部搬進新的 `feedback/`；`detector/` 只剩 ②③④⑤、`investigator/` 只剩 ⑥⑦⑧；每個套件的 docstring 第一行寫它是哪幾個模組；CLAUDE.md 結構表與 architecture 4.2 對應。**命令列名稱不變**（`python3 -m detector feedback …` 照舊：排程表、helm、64 份文件都在用），`detector/__main__.py`、`investigator/__main__.py` 只是入口（組合根），從 `feedback/` 掛子命令。
  - **B** 讀得懂：沒有函式 >100 行；`action` 當參數的命令（feedback、adjust、incident）改成 typer 子命令，每個子命令只有自己的參數；不再 `locals()`。
  - **C** 依賴只往一個方向：`core` → `sources` → `detector`、`investigator` → `feedback` → `api`、`scheduler`、`tasks`、`plugins`、`models`（門面）、`__main__`。用 `tests/test_layers.py` 鎖住，之後誰加反向 import 測試就紅。函式內 import 只留真的要 lazy 的（SQLAlchemy、LLM SDK）。
  - **D** 跨階段的文件有型別：`Anomaly`（已有，搬 `core/`）、`Alert`（⑥ 寫、⑧⑨ 讀）、`aiops-tuning` 的調整文件（⑨ 寫、④ 讀）；規則同 [vo-models](2026-11-vo-models.md)：`to_doc()` 跟改前逐欄位相同、`from_doc()` 讀舊文件不炸、mapping 跟模型對照測試。
- **範圍**：
  - 做：上面 A～D；每一步一個 commit、全部單元測試過才下一步。
  - 不做：改任何階段的行為、ES 查詢語句、文件格式、命令列參數名；`cfg` 改成到處傳模型（226 處，另開一輪）；`Span`／`Findings`（⑦ 內部的 dict，不跨階段）；PDCA 紀錄裡的舊路徑措辭（只修會斷的連結）。

## Do：改動哪些 code、改了什麼、為何要改

四步各一個 commit，每步結束全部單元測試都過才下一步。合計 104 檔、+2,935／−1,703 行（含測試與文件）。

### A 一個階段一個資料夾（`25f2a78`）

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `feedback/`（新套件）：`suggest.py`（原 `detector/feedback.py`）、`cmd_feedback.py`、`cmd_adjust.py`、`cmd_backtest.py`、`cmd_metrics.py`（原 `detector/cmd_*`）、`importer.py`（原 `investigator/importer.py`） | `git mv`；相對 import 改絕對；`detector/feedback.py` 改名 `suggest.py`（它是「回饋 → 建議」的計算，不是整個 ⑨） | ⑨ 本來散在三個資料夾；`detector/` 31 檔減到 24 檔，只剩 ②③④⑤ |
| `core/tuning.py`（新）、`detector/adjust.py`、`feedback/suggest.py` | 原 `detector/tuning.py` 拆三份：`TUNING_INDEX`／`TUNING_MAPPING` 是契約（④ 讀 ⑨ 寫）→ `core/`；`load_adjustments`／`load_overrides`／`data_adjustments` 是 ④ 套用時讀的 → `adjust.py` 末段；`propose_from_text` 是 ⑨ 寫的 → `suggest.py` | 一個檔同時有讀、寫、契約，搬到哪都會製造反向依賴；拆開後 detector 不用知道 feedback |
| `core/cli.py`（新）、`feedback/cli.py`（新）、`detector/__main__.py` | 命令列共用的 `registry`／`run`／`Opt`／`SERIES` 搬 `core/cli.py`；⑨ 的五個子命令（feedback、incident、adjust、backtest、metrics）搬 `feedback/cli.py`，`detector/__main__.py` 把三邊的 `COMMANDS` 合起來建 app | **命令列名稱不變**（排程表、helm、64 份文件都寫 `python3 -m detector feedback …`）；`__main__` 是入口（組合根），它 import feedback，feedback 不 import 它 |
| `detector/cmd_baseline.py`（`cmd_tune`） | 沒搬 | `tune` 用了 `cmd_baseline` 一半的 helper（`apply_min_count`、`with_status`、`score_tuned`…），拆出去會把 baseline 數學一起拖走；它是 ④ 的旋鈕，寫 aiops-tuning 的那一行透過 `core/tuning.py` |
| `tests/`（15 檔）、`docs/aiops-data-reference.md`、`docs/detection-and-learning.md`、`docs/feedback/README.md`、`models/README.md`、`CLAUDE.md`、`docs/architecture.md` 4.2 | import 路徑、檔案連結、結構表 | 文件指的位置要對；`docs/pdca/meta/2026-10-open-issues.md` 只改一個會斷的連結，措辭不動 |

### B 拆大函式、子命令（`d446bba`）

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `feedback/cmd_feedback.py` | `cmd_feedback` 168 行拆成 `feedback_export`／`pending`／`list`／`summary`／`suggest`／`add` 六個函式；suggest 的四種建議各一個（`_suggest_false_positives`／`_missed`／`_precision`／`_rules`），「查過沒 → 寫 → 印」收成 `_proposed`／`_propose` 一處；add 拆 `_target`、`_extract`；`cmd_incident` 拆 `incident_list`／`resolve`／`add` | 一個函式用 `if args.action ==` 塞 6 個命令，讀的人要跳著看；ES 呼叫順序照舊（GET 查 → PUT 寫 → print），假 ES 的測試沒改斷言 |
| `feedback/cmd_adjust.py` | 拆 `adjust_list`／`remove`／`approve`／`add`；核准擋不擋的四條規則收成 `_approve_blocked` 回原因字串；三種 kind 的欄位收成 `_kind_fields` | 同上；`ensure_index` 從函式開頭移到真的要寫的那一刻（參數錯時不碰 ES） |
| `feedback/cli.py` | feedback／incident／adjust 改成 typer 子 app（`core.cli.group`），每個子命令只有自己的參數 | `adjust` 24 個參數、`feedback` 18 個，大半只有某個 action 用得到；現在 `feedback export` 給 `--min-false-positive` 會回 2 |
| `core/cli.py`、`detector/__main__.py`、`investigator/__main__.py` | `run(handler, kind, /, **params)`：參數明確列出，不再 `locals()`（`detector` 14 個、`investigator` 5 個命令）；前兩個 positional-only 是因為 `adjust add` 自己有個參數叫 `kind` | 哪個命令傳哪些參數看得見、grep 得到 |
| `investigator/cmd_alert.py` | `run` 117 行拆 `_chain`（跨對象關聯）、`enrich`（歸屬＋嚴重度）、`investigate_alert`（根因分析＋相似事故＋LLM，再拆 `_rca`／`_similar`／`_with_head`）、`alert_doc`（寫 ES 的那份）、`heartbeat_doc`、`announce`（印什麼、回 new／resolved）；`run` 只剩流程 | 巢狀 5 層、一個函式同時讀 ES、併件、算嚴重度、跑根因、叫 LLM、寫 ES、印訊息 |
| `investigator/alerts.py` | `merge` 98 行：閉包 `downstream_alert` 升成 `_downstream_alert`；工廠前站的那段 → `_upstream_station`；開不開新件 → `_open_new`；上游整件併入 → `_fold_upstreams`；`_open_copy`、`_call_graph` | 核心演算法的每一條規則一個函式一個名字；44 個 merge 測試一條沒改 |
| `feedback/cmd_backtest.py` | `run_backtest` 93 行：兩個閉包 `run`／`judge` 升成 `_detect_all`／`_judge`，逐筆剔除 → `_ablation` | 閉包抓了 12 個外層變數，看不出各自要什麼 |
| `api/app.py` | `submit` 71 行拆 `_ensure`／`_feedback_base`／`_extract_text`／`_feedback_docs`／`_incident_patch`／`_upsert_incident` | 同上 |
| `tests/test_cli.py` | `HANDLERS` 改成「命令 子命令」兩層 key；去掉 `a.action` 的斷言；加 `feedback`／`adjust approve`／`incident resolve` 少參數回 2 的案例 | 對齊子命令；斷言的值沒改 |

### C 依賴只往一個方向（`627b76c`）

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `core/anomaly.py`（新） | `ANOMALY_INDEX`／`ANOMALY_MAPPING`／`KEYS_MAPPING`（原 `detector/es.py`）、`Anomaly`（原 `detector/base.py`） | ④⑤ 寫、⑥⑨ 讀：`investigator/cmd_alert` 以前函式內 `from detector.es import ANOMALY_MAPPING` |
| `core/alert.py`（新） | `ALERT_INDEX`／`ALERT_MAPPING`／`STAMP_LAG`／`stamp_of`（原 `investigator/cmd_alert.py`） | ⑥ 寫、④（detect-status）⑧⑨ 讀：`detector/cmd_baseline` 以前函式內 `from investigator.cmd_alert import …  # 避免循環` |
| `core/owner.py`（新）、`sources/base.py`、`sources/stores.py` | 歸屬查詢（`load_owners`／`owner_of`，原 `detector/owner.py`）→ `core/`；`write_anomalies` 改成 `sources.base` 的模組函式＋`Backend.write_anomalies` 補 `engine_version`；`ESStore.write_anomalies` 在檔頭 import `core.owner` | `sources/stores.py` 以前函式內 `from detector.owner import …`：sources（下層）反過來依賴 detector |
| `sources/es_source.py`、`detector/es.py` | `_value_aggs`／`_bucket_value`／`_bucket_events`（序列的 value 寫法 → ES aggregation）搬到 `es_source.py` | `es_source` 以前函式內 `from detector.es import …`，而 `detector/es.py` 檔頭又 import sources——同一組函式兩邊互相要 |
| `core/incident.py`、`investigator/similar.py`、`sources/stores.py` | `Fingerprint.es_query()`（原 `similar.query`）、`DEFAULT_WEIGHTS`、`classify`／`describe_class` 搬進 `core/incident.py`；`similar.py` 從那裡 import | `sources/stores.py` 以前要 `investigator.similar.query`；`core/llm.py` 以前要 `investigator.similar.classify`——core 依賴 investigator |
| `core/traces.py`、`detector/trace.py`、`detector/trace_merge.py`、`investigator/report.py`、`investigator/__main__.py`、`plugins/observability/chain.py` | 刪 `spans_of`／`spans_by_id`（只是 `as_backend(es).spans(...)` 的轉手）；呼叫端直接呼叫 Backend | `core/traces.py` 以前函式內 `from sources.base import as_backend`——core 依賴 sources。拿掉後 `sources/es_source.py` 對 `core.traces` 的 import 也能提到檔頭 |
| 23 個檔 | 47 處函式內 import 提到檔頭（`detector/model.py` 的 `match_keys` 包裝函式刪掉，直接用 `adjust.match_keys`） | 真的要 lazy 的只剩三處：`feedback/importer.py` 的 `sqlalchemy`（選配）、`scheduler/__main__.py` 的 `tasks`（只有 `--enqueue` 要 broker）、`sources/base.py` 的 `.es_source`（套件內工廠，es_source 反過來 import base） |
| `tests/test_layers.py`（新） | 三個測試：每個套件只能 import 名單上的套件（`__main__.py` 除外）；函式內 import 只有那三處；四個索引名的字面值只能出現在 `core/` | 之後誰加反向 import、誰在別處再寫一次 `"aiops-alerts"`，測試就紅 |
| `tests/`（test_apm、test_values、test_owner、test_es_backend、test_plugin、test_trace_merge、test_cmd_alert、test_import_incidents、test_detector_base、e2e 腳本） | import 路徑；mock 的對象從 `core.traces.spans_of` 改成 `as_backend(...).spans` | |

### D 跨階段的文件改成模型（`ec669b4`）

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `core/doc.py`（新） | `Doc` 基底（原在 `core/incident.py`）：`extra="forbid"`、賦值也驗、`from_doc` 寬鬆、`to_doc` 只寫有給的、不寫 None、**datetime 轉 ISO 字串** | Incident、Fingerprint、Alert、Adjustment 四個模型同一套規矣 |
| `core/alert.py` | `Alert`：38 個 mapping 欄位＋`rca_requested`／`rca_requested_at`（api 標的，`UNMAPPED_FIELDS`）；執行期欄位 `anoms`／`new`／`upgraded`／`rerun`／`report_text`／`llm` 標 `exclude=True`（原 `_anoms`／`_new`／`_upgraded`／`_rerun`／`_report`／`_llm`）；時間欄位 `datetime`；`status` 是 `Literal["open", "resolved"]` | 以前 `alerts.py` 用 `al["_anoms"]` 這類底線鍵記執行期狀態、`cmd_alert` 寫 ES 前 `{k: v … if not k.startswith("_")}` 過濾；`_ts()` 到處把字串轉時間 |
| `investigator/alerts.py` | `_new` 回 `Alert`；`_attach`／`_join`／`_fold`／`merge` 及 B 拆出來的六個 helper、`_own`／`rca_route`／`owner`／`link_shared`／`headline`／`summary` 全改屬性；`merge` 收 dict（ES 讀的）或 Alert，回 `list[Alert]` | 物件在 ⑥ 內部全程是 Alert，不再 dict ↔ 時間字串來回 |
| `investigator/cmd_alert.py` | `_open_alerts` 回 `Alert.from_doc`；`grade_alert`／`enrich`／`investigate_alert`／`wants_rerun`／`needs_rerun`／`attempts`／`announce` 改屬性；`alert_doc` 把報告、連結、筆數、`@timestamp` 設回模型再 `to_doc()`；`wants_rerun` 把 `rca_requested` 設 None（to_doc 不寫，等於以前的 pop） | |
| `core/incident.py` | `Fingerprint.from_alert` 收 Alert 或 dict（匯入、測試還是 dict）；`Incident.from_alert(al: Alert, …)` 用屬性 | |
| `plugins/observability/__init__.py` | `chain()` 拿到的 `open_alerts` 是 Alert | plugin 看到的是同一個物件 |
| `core/tuning.py`、`feedback/cmd_adjust.py`、`feedback/cmd_feedback.py`、`feedback/suggest.py`、`detector/cmd_shift.py`、`detector/cmd_baseline.py` | `Adjustment` 模型（五種 kind、兩種 status 是 `Literal`，有型別的欄位 25 個，`extra="allow"`）＋`validated(doc)`；六個寫 aiops-tuning 的地方寫之前都過一次 | 讀的那端（`adjust.load` → pandas 向量運算）這一輪不改，寫入端先擋 kind／status／型別錯 |
| `tests/test_alert.py`、`tests/test_tuning.py`（新）；`test_alerts`／`test_factory_alerts`／`test_severity`／`test_cmd_alert`／`test_incident`／`test_api_endpoints` | 契約測試；既有測試的 `out[0]["service"]` → `out[0].service`、`stored()` → `to_doc()`、手寫的 alert dict → `Alert(...)`；斷言的值沒改 | |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 四步每一步行為沒變 | `cd aiops-engine/code && python3 -m pytest -q`，每步結束跑一次 | 改之前 773 過；A 773、B 773、C 776（多 test_layers 的 3 個）、D 785（多 test_alert 5、test_tuning 4）；16 skipped 四步都一樣 |
| **A** 命令列沒變 | `tests/test_cli.py` 的 `ScheduleCommands`：四張排程表裡每一條字面指令都還解析得過、到得了處理函式 | 全過；`python3 -m detector feedback suggest`、`incident add`、`adjust approve --id …` 的參數值跟以前一樣 |
| **B** 拆完的函式跟拆之前做一樣的事 | 沒改任何既有測試的斷言值（test_feedback、test_feedback_export、test_alerts 44 個 merge 情境、test_factory_alerts、test_backtest、test_backtest_group、test_api）；只改了 test_cli 的查表方式與 `a.action` 斷言 | 全過。拆 `alerts.merge` 時 docstring 接錯過一次（三引號不在行首），測試整檔 SyntaxError 立刻抓到，從 git 的原文重接 |
| **B** 子命令只收自己的參數 | test_cli 新加：`feedback export --min-false-positive 2`、`adjust approve`（沒 `--id`）、`incident resolve`（沒 `--id`）、`["feedback"]`、`["adjust"]` 都要回 2 | 過；`adjust add` 的 Namespace 沒有 `force` 屬性（以前有） |
| **C** 依賴方向 | `tests/test_layers.py::test_direction`：`core` 不 import 別人；`sources` 只 import `core`；`detector`、`investigator` 只 import `core`、`sources`；`feedback` 可以 import 前四個；`__main__.py` 不算 | 過。改之前的套件圖：`core → investigator、sources`；`sources → detector、investigator`；`detector ↔ investigator`。改完：`core → ∅`、`sources → core`、`detector → core、sources`、`investigator → core、sources`、`feedback → core、detector`、上層隨意 |
| **C** 函式內 import 只剩真的要 lazy 的 | `test_layers::test_no_inline_imports`：名單外的函式內 import 就紅 | 71 → 3（名單上的三個） |
| **C** 索引名不會再被各處重寫一次 | `test_layers::test_contracts_live_in_core`：`"aiops-anomalies"`／`"aiops-alerts"`／`"aiops-incidents"`／`"aiops-tuning"` 的字面值只能在 `core/` | 抓到 `sources/rows.py` 的 `bulk_index("aiops-incidents", …)`，改成 `INCIDENT_INDEX` |
| **C** 搬了的函式行為不變 | `test_values`、`test_apm`（`_value_aggs`／`_bucket_*` 從 `sources.es_source` import）、`test_owner`（歸屬從 `core.owner`、寫入從 `sources.write_anomalies`，含「每筆都有 engine_version」）、`test_trace_merge`／`test_apm`（`as_backend(es).spans`） | 過；`test_cmd_rules` 一度紅：`cmd_rules` 改成檔頭 `from .adjust import load_adjustments` 後測試的 `mock.patch("detector.adjust.load_adjustments")` 接不到，改成經模組取 `A.load_adjustments` |
| **D** `Alert.to_doc()` 跟改之前 `alert_doc` 寫的 dict 逐欄位相同 | 把 C 結尾的樹（`627b76c`）`git archive` 到暫存目錄，同一支腳本在兩棵樹各跑一次：新開的一件（5 筆異常、有報告）、上一輪寫進 ES 的件過了 close_after 結束、跨服務上游併進下游；攤平比對每個鍵 | 三組的差只有兩種：**值是 None 的鍵不再寫**（`resolved_at`、`findings`、`llm_summary`、`llm_provider`、`report`、`report_url`、`feedback_url`：以前寫 null）、**沒跑過根因分析的件不寫 `similar: []`**（以前寫空陣列）。有值的欄位（含 `report_titled`、`@timestamp`、時間字串格式）全部相同。讀的程式都是 `.get(...) or []`，ES 對 null 與沒有欄位的查詢結果一樣 |
| **D** 模型跟 mapping 對得上、執行期欄位不寫 ES | `test_alert`：mapping 38 欄都在模型裡、模型多的只有 `UNMAPPED_FIELDS`；`anoms`／`new`／`upgraded`／`rerun`／`report_text`／`llm` 不在 `to_doc()` | 過 |
| **D** 時間欄位 | `test_alert`：`"…Z"` 與 `"…+00:00"` 都讀成同一個 datetime；寫出去是 `+00:00` 的 ISO（跟以前 `isoformat()` 一樣）；沒給的不寫 | 過 |
| **D** 讀舊文件不炸、打錯會擋 | `test_alert`：`series: null`、`similar: null`、多一個舊欄位 `kinds`、`rca_requested: true` 都讀得進來，寫回去不帶 `kinds`；`Alert(servcie=…)`、`status="closed"`、賦值 `al.status = "done"`、缺 `category` 都丟 `ValidationError` | 過 |
| **D** `api POST /api/rca` 的標記下一輪拿掉 | `test_api_endpoints::AlertHonoursRcaRequest` 改用 `Alert.from_doc`：`wants_rerun` 後 `rca_requested is None`、`to_doc()` 沒有這兩個鍵 | 過 |
| **D** aiops-tuning 寫入前驗 | `test_tuning`：mapping 的欄位都在 `Adjustment` 裡；`kind="overide"`、`status="approved"`、`z="high"`、`apply_to="train"`（該是陣列）都擋；八種 `suggest_*` 的輸出＋`suggest_from_text` 三種動作＋`suggest_exclude` 全部過得了 `validated` | 過 |
| 文件 | `python3 tools/check-docs.py` | 連結、索引、舊說法、類型 0 個問題（A 抓到 `2026-10-open-issues.md` 一個指到 `detector/tuning.py` 的連結，只改連結目標） |
| 靜態檢查 | `python3 -m pyflakes` 改過的檔 | 剩的都是標了 `noqa: F401` 的 re-export（`detector/es.py` 給 cmd_*.py 用的 `doc_id`、`fill_zero`、`BASELINE_*`） |

**量化（`tests/` 以外）**：

| | 改之前 | 改完 |
|---|---|---|
| 檔／行 | 85／12,463 | 91／12,977（多 `core/` 6 個契約檔、`feedback/cli.py`、`core/cli.py`；舊的 `detector/tuning.py`、`detector/owner.py` 刪掉） |
| 函式 >100 行 | 2（168、117） | 0 |
| 函式 >50 行 | 13 | 7（最長 77：`detector/cmd_trace.cmd_trace_detect`） |
| 巢狀 ≥4 層 | 28 | 24 |
| 函式內 import | 71 | 3 |
| `locals()` | 19 個命令 | 0 |
| 套件間反向依賴 | 5 條 | 0（`test_layers` 鎖住） |
| 一個階段散在幾個資料夾 | ⑨ 三個 | 一個 |
| `except Exception` | 26 | 26（沒動：都是「這一件失敗不擋整輪」的刻意寫法，留 Act） |

沒測到的：連真的 ES 跑（oop-core、vo-models 也沒連）；Grafana 面板讀改過的文件（`similar: []` 不再寫，面板若有查 `similar` 存在與否要看）；`python3 -m detector feedback suggest` 真的對著 ES 跑一輪（`_propose` 的順序只有假 ES 驗）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `except Exception` 26 處沒動 | B 量函式時 | 真的 bug 會被吞成一行訊息（例如根因分析失敗只印「根因分析失敗：…」） | 下一輪逐處看：哪些該縮成具體的例外、哪些該至少 `print(traceback)` |
| `cfg` 還是 dict 傳遍全專案（`cfg.get` 166 處） | Plan 就排除 | 設定鍵打錯要到用到那一行才知道（`load_config` 驗過一次，之後 `model_dump` 回 dict） | 另開一輪：`load_config` 回模型、`cfg.get("alerts", {})` 這類改成 `cfg.alerts` |
| `Adjustment` 是 `extra="allow"` | D 盤 suggest_* 的輸出時：附屬欄位（`missing`、`incident_ids`、`feedback_text`、`identified_cause`、`alert_id`…）散在八個函式裡，一輪盤不完整 | 欄位名打錯的附屬欄位不會被擋（kind／status／有型別的 25 欄會） | 把這些附屬欄位盤完、加進模型、改 `forbid` |
| aiops-tuning 的讀取端、aiops-alerts 在 api／feedback 的讀取端還是 dict | D 的範圍決定：`adjust.load` 之後是 pandas 向量運算；api 的 `load_alert` 只讀幾個欄位 | 讀的那端沒有型別保護 | 等有人改那幾段再順手用 `Alert.from_doc`／`Adjustment` |
| `Span`／`Findings` 還是 dict | Plan 就排除（⑦ 內部，不跨階段） | `report.py` 的 `Ctx.findings` 逐段填 dict，`analyze.py` 的 dataclass 進 findings 時又轉回 dict | 看 ⑦ 要不要改時一起 |
| `to_doc()` 不再寫值為 None 的鍵、沒跑過根因的件不寫 `similar: []` | D 的 golden 比對 | ES 查詢結果一樣；Grafana 面板若有「`similar` 欄位存在」這種條件要看一眼 | 下次連 Grafana 時確認 |
| `scheduler/__main__.py` 的 `import tasks` 還在函式內 | C 盤函式內 import | 無（只有 `--enqueue` 要 broker，不裝 celery 的環境也能跑 beat） | 留著，名單上有寫原因 |
| 命令列參數 ≥8 個的函式還有 23 個 | B 量完 | 都是 typer 的命令函式（`adjust add` 20 個、`feedback add` 12 個…），參數就是命令列選項 | 不改：這是命令列本來的面 |
| `detector/cmd_trace.cmd_trace_detect` 77 行、`plugins/factory/paths.build_lot` 74 行、`detector/cmd_baseline.cmd_train` 69 行 | B 量完 | 讀得懂但長 | 下一輪有動到再拆 |

- **決定**：
  - 資料夾照模組切、命令列名稱不動、`__main__` 是組合根可以 import 下游套件——寧可多一層入口，不改 64 份文件和排程表。
  - 兩個階段以上都碰的 ES 索引，契約（index 名、mapping、文件模型）一律放 `core/<名詞>.py`，`test_layers` 連字面值都鎖。
  - `to_doc()` 用 `exclude_unset`＋`exclude_none`：只寫有給的欄位。代價是 null 不再寫，換來 `from_doc → to_doc` 是忠實的來回。
  - 讀的那端不硬改成模型：寫入端有型別就擋得住壞資料進 ES，讀的 dict 等有人改那段再順手。
- **下一輪**：`except Exception` 逐處看；`Adjustment` 盤完改 `forbid`；`cfg` 改模型另開一輪。[README.md](../README.md) 的待辦照這三項。
