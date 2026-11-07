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
| 3 | 重複副本與情境：先看步驟 1 之後 4 份 `rca`、4 份 `schedule` 還剩多少真的差異，只留差異；**不**把情境改成包 13 種設定的根模型 | 預設值搬進模型之前做會維護兩套繼承 |
| 4 | 約 80 個常數：動到那個模組時才搬進對應模型 | 不一次大改 |

- **決定**（討論後）：
  - `Settings` 用 `pydantic` 的 `BaseModel` 自己讀環境變數，**不加 `pydantic-settings`**（`requirements.txt` 現在沒有，多一個相依要進 image）。
  - 必填檢查只在入口（`main.py` 的角色）做，**不在 import 時做**，不然單元測試、`check-docs` 光 import 就要有 `ES_URL`。
  - 預設值照程式現況，不照想像：`LLM_PROVIDER` 預設 `template`、`AIOPS_PLUGIN` 預設 `observability`、`API_PORT` 預設 8080、`SCHEDULE_STAGE` 預設 1。
  - 步驟 1 不照「`load_config` 一次改回模型、測試一起改」做：現有測試與 e2e 有 14 個檔直接傳 dict，一次改會全壞，所以要過渡層。
  - 「模型預設值就是 baseline、各情境 yaml 只寫差異」只對真的共通的鍵成立：APM 與 OTel 的索引、欄位本來就不同，不是預設值差異，所以步驟 3 要看實際差異才決定。
- **不做**：不動 helm values、Grafana 設定；不動領域 dict（`alert`、`spec`、`inp`）——那是下一份計畫；不把 `Scenario` 改成包全部設定的根模型。
- **風險**：預設值搬進模型時，同一個鍵在不同檔案原本的預設值不一致（搬之前逐鍵 grep，不一致的列出來問）；`value` 這類自由格式欄位第一輪保留成 `dict`；測試用環境變數（`mock.patch.dict(os.environ, …)`）要跟著改成注入 `Settings`。

## Do

（做完補）

## Check

（做完補）

## Act

（做完補）
