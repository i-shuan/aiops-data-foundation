# 設定集中管理：環境變數 Settings、設定檔回模型、時間統一（2026-11）

**狀態**：進行中（步驟 0 完成、步驟 1 做到一半）　　**日期**：2026-11-07　　**相關**：分支 `claude/config-settings`（從 master `079f97e` 開）；承接 [feature-folders](2026-11-feature-folders.md) 之後盤點出的資料形狀問題
**模組**：②～⑨（程式內部，不改任何階段的行為）

## Plan

- **需求**：盤點發現 `dict` 型別佔參數 22%、設定取值約 150 處用字串鍵、環境變數散在 9 個檔、參數命名含糊（`cfg` 同時指 7 種設定）。使用者要設定有一個專門的地方，時間處理統一。
- **目的**：設定打錯字、少欄位在啟動時就報錯；參數名看得出是哪一種設定；預設值只有一份。
- **盤點**（`aiops-engine/` 底下）：參數 yaml 23 個、13 種（已有 Pydantic 模型，但驗完倒回 dict）；情境 yaml 4 個；環境變數約 20 個（無驗證）；程式裡寫死的可調常數約 80 個；helm values 與 Grafana provisioning 屬部署層，不在範圍。
- **範圍與順序**（每一步一個 PR 等級的 commit 群，全部測試加 e2e 都綠才進下一步）

| 步 | 做什麼 | 為什麼排在這 |
|---|---|---|
| 0 | `shared/config/settings.py`：環境變數集中成一個有型別的 `Settings`，只在入口（`main.py` 各角色）讀一次、往下傳；`shared/infra/env.py` 併進來 | 獨立、小、不碰演算法，先消滅「打錯字不報錯」 |
| 1 | `shared/config/` 搬家（`shared/scenario/config.py` 拆成一種一個檔加 `loader.py`），再三階段回模型：①模型加 `get`／`[]` 過渡層（語意同現在的 dict：只有 yaml 有寫的鍵才算有）、`load_config` 回模型 ②依 kind 逐種換成屬性存取、參數名改 `<kind>_config`、預設值搬進模型 ③拆掉過渡層與 AST 檢查 | 設定的主體；過渡層讓現有測試和傳 dict 的測試不必一次全改 |
| 2 | 時間統一 `shared/clock/`：`TimeWindow`、`parse_duration`／`parse_instant` 只給 `interface/` 呼叫，設定檔的 `timezone` 驗成 `ZoneInfo` | 設定回模型後，時區欄位才有地方驗 |
| 3 | 重複副本：**用「模型預設值當 baseline」**（使用者決定）——共通的鍵預設值進模型，各情境的 `rca`、`schedule` 等 yaml 只留跟預設值不同的鍵；不自己再做一套 `extends`；**不**把情境改成包 13 種設定的根模型；像 `rules` 這種長清單若各情境只微調幾條，另議（可能保留獨立檔） | 預設值搬進模型之後才有東西可以省；之前做會維護兩套繼承 |
| 4 | 約 80 個常數：動到那個模組時才搬進對應模型 | 不一次大改 |

- **決定**（討論後）：
  - `Settings` 用 `pydantic` 的 `BaseModel` 自己讀環境變數，**不加 `pydantic-settings`**（`requirements.txt` 現在沒有，多一個相依要進 image）。
  - 必填檢查只在入口（`main.py` 的角色）做，**不在 import 時做**，不然單元測試、`check-docs` 光 import 就要有 `ES_URL`。
  - 預設值照程式現況，不照想像：`LLM_PROVIDER` 預設 `template`、`AIOPS_PLUGIN` 預設 `observability`、`API_PORT` 預設 8080、`SCHEDULE_STAGE` 預設 1。
  - 步驟 1 不照「`load_config` 一次改回模型、測試一起改」做：現有測試與 e2e 有 14 個檔直接傳 dict，一次改會全壞，所以要過渡層。
  - 「模型預設值就是 baseline、各情境 yaml 只寫差異」（做法一，使用者選定）只對真的共通的鍵成立：APM 與 OTel 的索引、欄位本來就不同，那些仍然要寫在各情境的 yaml 裡；步驟 3 開始前會先逐鍵列出「共通 / 各情境不同」給使用者看。
- **不做**：不動 helm values、Grafana 設定；不動領域 dict（`alert`、`spec`、`inp`）——那是下一份計畫；不把 `Scenario` 改成包全部設定的根模型。
- **風險**：預設值搬進模型時，同一個鍵在不同檔案原本的預設值不一致（搬之前逐鍵 grep，不一致的列出來問）；`value` 這類自由格式欄位第一輪保留成 `dict`；測試用的 `mock.patch.dict(os.environ, …)` 要照樣有效（所以 `settings()` 每次重讀環境，不做快取）。

## Do

### 步驟 0：環境變數集中成 `Settings`（已做）

- 新增 `shared/config/settings.py`：`Settings`（21 個欄位，名字是環境變數的小寫，預設值照原本）、`settings()`（每次呼叫重讀環境，所以測試的 `mock.patch.dict(os.environ, …)` 照樣有效，24 個動環境變數的測試檔不用改）、`require_es_url()`／`require_llm_api_key()`（用的那一刻才檢查，繼承 `KeyError`，原本抓 `KeyError` 的地方照舊）、`chat_model(default)`（各 provider 的預設模型不同）、`cors_origins`、`tls`；型別錯拋 `SettingsError`，訊息講變數名與值。
- 改 9 個讀環境變數的檔（`shared/infra/es.py`、`shared/scenario/base.py`、`llm/summary.py`、`interface/web/{__main__,routes,dto}.py`、`interface/jobs/{local,worker}.py`、`shared/infra/env.py`）改用 `settings()`；`ENGINE_VERSION` 用單獨的 `engine_version()` 讀（模組載入時就要，不該因為別的變數錯而連 import 都失敗）。
- `main.py`：各角色的程式改成「要跑的那一刻才 import」；`SettingsError`／`MissingSetting` 印一行「設定錯誤：…」、結束碼 2，不丟 traceback。
- `tests/test_settings.py`：除了 `settings.py`，`src/` 和 `main.py` 沒有任何 `os.environ`／`os.getenv`（AST 檢查）。
- 順手刪掉 `shared/infra/clock.py` 裡重複定義的 `stamp_of`（先前搬檔時重複貼的）。
- **跟計畫不同的地方**：計畫寫「只在入口讀一次、往下傳」，實際是「每次用到就 `settings()` 重讀」。原因：`ES()` 在二十幾個函式裡直接建立、`load_scenario()` 在測試裡依環境變數重讀；真的往下傳要改所有簽名、測試要全改。每次重讀只是讀 21 個字串，成本可以忽略。步驟 1 設定回模型時簽名本來就要動，到時候再看哪些適合改成注入。

### 步驟 1：設定檔回模型（①②③做完，13 種都已換成屬性存取；過渡層還在）

- **①搬家與拆檔**：`shared/scenario/config.py`（790 行）拆成 `shared/config/` 套件：`common.py`（共用型別與小段）、一種設定一個檔（`series.py`、`rules.py`、`peer.py`、`quality.py`、`trace.py`、`rca.py`、`schedule.py`、`chronic.py`、`prune.py`、`calendar.py`、`adjustments.py`、`ingest.py`、`paths.py`）、`loader.py`（讀檔、驗證、錯誤訊息、`KINDS`、`MODELS`）；用腳本依 AST 切、補 import，只搬不改。
- **②過渡層與回模型**：`Strict` 加 `get`／`[]`／`in`／`keys`／`items`／`values`／`len`／`to_dict`／`==`，語意跟原本的 dict 一樣（**只有 yaml 有寫的鍵才算有**）；`load_config`、`validate` 回那一種的模型（13 個 `@overload`，編輯器知道 `load_config(p, "series")` 是 `SeriesConfig`），不再 `model_dump` 倒回 dict。現有的程式與測試幾乎不用改。
- **③逐種換成屬性存取**：先做 `prune` 當樣板——`plan(prune_config: PruneConfig, …)`、`it.keep_days`、預設值（`time_field="@timestamp"`、`timezone="Asia/Taipei"`、`indices=[]`）進模型、程式裡的 `.get(…, 預設)` 刪掉、測試改用 `validate({…}, "prune")` 造設定。
- **③ 目前的進度**（13 種設定換成屬性存取：參數改名 `<種類>_config`、預設值進模型、程式裡的 `.get(鍵, 預設)` 刪掉、測試改用 `validate({…}, 種類)` 造設定）：

| 種類 | 狀態 | 說明 |
|---|---|---|
| prune、calendar、chronic、ingest、paths、peer、quality、schedule | ✅ 做完 | `calendar` 的來源（`Provider`）仍用 `src.get(…)`：`cache_dir` 寫 null 是「不快取」、沒寫是「用預設」，屬性分不出來；`quality` 每種 `type` 的 `min_docs` 等預設不同，預設留在程式（`_or(c.min_docs, 100)`），不進模型 |
| adjustments | ✅ 載入端 | `manual_adjust.load` 吃 series／trace／rules 其中一種（型別 `AdjustableConfig`）；`adjustments_file`、`calendar_file` 寫 null 是「不載入」，保留 `config.get` |
| rules | 🟡 設定層 | `RulesDetector`、`approved_rules`、`cmd_rules` 用屬性；單條規則的函式（`evaluate`、`describe`…）仍吃 dict 或 `Rule`——aiops-tuning 核准的規則是 dict，tests 有 59 處直接傳 dict |
| trace、rca、series | ✅ 載入端與服務端 | 參數改叫 `trace_config`、`rca_config`、`series_config`（策略外掛的簽名也是 `rca_config`）；預設值進模型（`SeriesConfig` 的 interval 5m、history_weeks 4、borrowing…）；`status`、`shift`、`adaptive` 沒寫是 `None`，用 `config.status or Status()`。測試用 `validate({...}, "series")` 或 `tests/config_factory.py` 造 |

- **過渡層留著的地方**：`repo.series.fetch／rows／points` 與 `keys_of` 吃「像 dict 的規格」（series 的一條、peer／quality 的一個檢查、ingest 的 input、paths 的 mes）。這些要等 `series` 做完一起處理。
- **踩到的三個問題（過渡層的代價）**：
  1. `owners_source: list[Provider] | Provider` 只寫一個來源時變成 `Provider` 模型，`load_owners` 的 `isinstance(srcs, dict)` 失效、會去迭代模型——單元測試傳的都是 dict 所以沒抓到。改成 `srcs if isinstance(srcs, list) else [srcs]`，補測試（用 `validate` 造設定）。
  2. 5 個模型有欄位叫 `keys`，蓋掉過渡層的 `keys()` 方法（pydantic 只警告），`{**spec}`（`peer_compare.py:78`）會壞。欄位改叫 `key_names`（`alias="keys"`，yaml 與 `spec["keys"]` 不變），加測試鎖「沒有欄位跟 `keys`／`items`／`values`／`get`／`to_dict` 同名」。
  3. 最上層的鍵順序變成欄位宣告的順序，不是 yaml 的；清單與 dict 值的順序不變。測試改比 `set`。

## Check

| 要驗什麼 | 怎麼驗 | 結果 |
|---|---|---|
| 預設值、空字串、型別錯、必填、唯讀 | `tests/test_settings.py` | ✅ 9 項 |
| 沒有別處再讀環境變數 | 同上的 AST 檢查 | ✅ |
| 行為沒變 | 全部測試（helm 也跑） | ✅ 796 通過、0 跳過 |
| 對真 ES、真的 api 行程 | `chain_e2e`、`factory_e2e`、`loop_e2e`（loop 用 `API_PORT` 起 api） | ✅ 26/26、15/15；loop 仍是那 2 個舊斷言 |
| 錯誤訊息 | `API_PORT=abc`、`SCHEDULE_STAGE=x` 跑 api／scheduler／tasks worker；沒設 `ES_URL` 跑 detector | ✅ 一行「設定錯誤：…」、結束碼 2（原本是 traceback 或 KeyError） |
| 第一版的問題 | `API_PORT=abc` 時連不相干的指令都在 import 就失敗：`env.py` 與 `worker.py`（載入時就建 Celery app、讀 broker 與時區）在載入時讀環境 | 已修：`engine_version()` 單獨讀、角色延遲 import |
| 步驟 1①②：拆檔與回模型後行為不變 | 全部測試（helm 也跑）；`chain`、`factory`、`loop`、`adaptive`、`synth` e2e | ✅ 單元 802 項（0 跳過）；e2e 同前（loop 仍是那 2 個舊斷言） |
| 步驟 1：每份 yaml 都驗得過、`to_dict()` 跟 `yaml.safe_load` 一樣（去掉 `_` 開頭的 anchor） | `tests/test_config.py`（configs/ 23 份加 tests/ 的 7 份） | ✅ |
| 步驟 1：過渡層的語意跟 dict 一樣 | `test_config.py::ModelsReadLikeDicts`：沒寫的鍵 `get` 回 default、`in` 為假、`**` 展開 | ✅ |
| 步驟 1③ 樣板 `prune` 走真的 CLI | `python3 -m main detector prune --config configs/baseline/prune.yaml --dry-run`（對真 ES） | ✅ 三個索引各印一行 |
| 其他 e2e（`adjust_edge`、`rules_edge`、`baseline_edge`、`trace_quality_edge`、`fine`、`peer_edge`） | 在 master 與這個分支各跑一次 | ⚠️ 前四個在 master 上也一樣失敗（e2e 腳本本身過期：`adjustments_file` 欄位、`index` 缺、索引沒建、日期格式），後兩個是測試 ES 當時分片分配不了（磁碟水位）；沒有一個是這個分支造成的 |

## Act

| 問題 | 在什麼情況下發現的 | 打算 |
|---|---|---|
| `interface/jobs/worker.py` 載入時就建立 Celery app | 驗證錯誤訊息時 | 之後改成函式裡才建；現在靠延遲 import 擋住 |
| 空字串視同沒設：原本 `SCHEDULE_STAGE=""`、`AIOPS_SOURCE=""` 會報錯，現在用預設 | 寫 `Settings` 時 | 接受（chart 本來就會給空字串）；要嚴格再改 |
| `Settings` 的欄位與 helm 的 `env.yaml` 沒有自動對照 | 盤點時 | 可以加測試：每個欄位在 chart 或文件有出現；之後 |
| 過渡層讓「型別錯的設定值傳進來」要到用的那一刻才發現（例如單元測試傳 dict 給已經換成屬性存取的函式） | 轉換 `prune` 時 | 每一種換完就把那一種的測試改成 `validate({…}, kind)`；過渡層拆掉前不加新的 dict 用法 |
| 6 個舊的 e2e 腳本在 master 上就壞（見 Check） | 驗證步驟 1 時 | 另開一件修；這份計畫不處理 |
| 過渡層還在：單條 `Series`／規則／檢查的 `spec` 仍是「像 dict」（`spec.get`）、`repo.series.fetch／rows／points`、`keys_of` | — | 下一步：階段 3 拆過渡層，再加 AST 測試擋 `cfg` 參數名；之後進時間統一與 yaml 瘦身 |
