# 設定集中管理：環境變數 Settings、設定檔回模型、時間統一（2026-11）

**狀態**：進行中　　**日期**：2026-11-07　　**相關**：分支 `claude/config-settings`（從 master `079f97e` 開）；承接 [feature-folders](2026-11-feature-folders.md) 之後盤點出的資料形狀問題
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

## Check

| 要驗什麼 | 怎麼驗 | 結果 |
|---|---|---|
| 預設值、空字串、型別錯、必填、唯讀 | `tests/test_settings.py` | ✅ 9 項 |
| 沒有別處再讀環境變數 | 同上的 AST 檢查 | ✅ |
| 行為沒變 | 全部測試（helm 也跑） | ✅ 796 通過、0 跳過 |
| 對真 ES、真的 api 行程 | `chain_e2e`、`factory_e2e`、`loop_e2e`（loop 用 `API_PORT` 起 api） | ✅ 26/26、15/15；loop 仍是那 2 個舊斷言 |
| 錯誤訊息 | `API_PORT=abc`、`SCHEDULE_STAGE=x` 跑 api／scheduler／tasks worker；沒設 `ES_URL` 跑 detector | ✅ 一行「設定錯誤：…」、結束碼 2（原本是 traceback 或 KeyError） |
| 第一版的問題 | `API_PORT=abc` 時連不相干的指令都在 import 就失敗：`env.py` 與 `worker.py`（載入時就建 Celery app、讀 broker 與時區）在載入時讀環境 | 已修：`engine_version()` 單獨讀、角色延遲 import |

## Act

| 問題 | 在什麼情況下發現的 | 打算 |
|---|---|---|
| `interface/jobs/worker.py` 載入時就建立 Celery app | 驗證錯誤訊息時 | 之後改成函式裡才建；現在靠延遲 import 擋住 |
| 空字串視同沒設：原本 `SCHEDULE_STAGE=""`、`AIOPS_SOURCE=""` 會報錯，現在用預設 | 寫 `Settings` 時 | 接受（chart 本來就會給空字串）；要嚴格再改 |
| `Settings` 的欄位與 helm 的 `env.yaml` 沒有自動對照 | 盤點時 | 可以加測試：每個欄位在 chart 或文件有出現；之後 |
| 步驟 1～4 還沒做 | — | 下一步：`shared/config/` 搬家與設定檔回模型（過渡層） |
