# 程式改成「一個功能一個資料夾」：detect／learn／incident／llm／shared（2026-11）

**狀態**：已完成（已合進 master）　　**日期**：2026-11-06　　**相關**：分支 `claude/feature-folders`（從 `claude/flatten-package` 開）；承接 [target-architecture](2026-11-target-architecture.md)（四層）
**模組**：②～⑨（程式結構，不改任何階段的行為）

## Plan

- **需求**：使用者看 `core/` 的檔案說「太散」——要找一個功能（例如告警）得跨 `core/entity/`、`core/repository/`、`service/converge/`、`service/rca/` 四個資料夾。要改成一個功能一個資料夾：異常分析、學習迴圈、告警與根因、LLM 與 RAG。
- **目的**：找一個功能的程式只看一個資料夾。
- **範圍**：只搬檔、改 import、改測試與文件；不改任何判斷邏輯。
- **新結構**（`aiops-engine/src/`）

| 資料夾 | 內容 | 從哪裡來 |
|---|---|---|
| `detect/` | ②③④⑤ 異常分析：規則、同儕比對、基準線、品質、慢性病、trace 合併與統計、工廠的 ingest／paths；`store/` 放它的資料物件與 ES 讀寫 | `service/detect/*`、`scenario/factory/{ingest,ingest_job,paths,commands}`、entity 的 anomaly／baseline／trace、repository 的 anomaly／baseline／chronic／housekeeping／owner／quality／segment／series／state／trace |
| `incident/` | ⑥⑦⑧ 告警與根因：收斂、告警、嚴重度、根因分析、報告；`strategy/` 放 chain、change 兩個情境策略；`store/` | `service/converge/*`、`service/rca/{analyze,report}`、`scenario/{it,factory}/{chain,change}`、entity alert、repository 的 alert／incident／change／log |
| `llm/` | LLM 摘要、過去相似事故的檢索與權重學習（RAG） | `service/rca/{llm,similar}` |
| `learn/` | ⑨ 學習迴圈：回饋、建議、回測、調整、事故匯入、量化指標；`store/` | `service/feedback/*`、entity 的 adjustment／feedback、repository 的 feedback／tuning |
| `shared/` | ES 連線、時間、執行環境、文件基底、事故與指紋的資料物件、repository 基底、`Repos`、情境（`scenario/`）、設定 schema（`schema/`） | `core/{connection,clock,env}`、entity 的 base／incident、repository 的 \_base、`scenario/base.py`＋`data/`、`schema/` |
| `interface/` | 命令列、API、排程；不動 | 不動 |

- **依賴方向**：`interface → learn → incident → llm → detect → shared`，只能往右。`tests/test_layers.py` 照這個順序鎖；索引名只能在 `*/store/`、ES 查詢語法只能在 `*/store/*_repo.py` 與 `shared/repository.py`。
- **要先決定的三件事**
  1. `Incident`／`Fingerprint`／`classify` 放 `shared/incident_doc.py`（名字不叫 `incident.py`，免得跟 `incident/` 資料夾重名）：`llm/`（過去相似）要用它，`incident/`（告警）又要用 `llm/`，放在任一邊都會循環。
  2. `Repos`（一條連線全部 repository）放 `shared/repos.py`，用名稱表**第一次取用才載入**各功能的 repository，所以 `shared/` 不靜態依賴任何功能。
  3. `stamp_of`（`@timestamp` 往前寫 1 分鐘）從 entity alert 搬到 `shared/clock.py`，因為 `detect/` 的狀態心跳也用它。
- **風險**：改全部 import 與約 75 個測試對 `Repos`、`mock.patch` 字串；情境 yaml 裡的策略路徑字串；文件裡的檔案路徑。靠 `pytest`、`check-docs`、`test_layers` 與 e2e 抓漏。

## Do

- `git mv` 加腳本改 import（147 個檔）：`core/{connection,clock,env}`→`shared/`、`core/entity/base`→`shared/document.py`、`core/entity/incident`→`shared/incident_doc.py`、`core/repository/_base`→`shared/repository.py`、`scenario/base`＋`data/`→`common/scenario/`、`schema/`→`shared/schema/`；`service/detect/*`→`detect/`；`service/converge/*` 與 `service/rca/{analyze,report}`→`incident/`；`service/rca/{llm,similar}`→`llm/{summary,similar}.py`；`service/feedback/*`→`learn/`；entity 與 repository 分到各功能的 `store/`（repository 檔名加 `_repo`）；`scenario/{it,factory}/{chain,change}`→`incident/strategy/{it,factory}_{chain,change}.py`；`scenario/factory/{ingest,ingest_job,paths,commands}`→`detect/factory/`。
- `Repos` 改成 `shared/repos.py`：名稱表＋第一次取用才用 `importlib` 載入，所以 `shared/` 沒有靜態 import 任何功能；呼叫端只是 import 路徑變（`from shared.repos import Repos`）。
- `stamp_of`、`STAMP_LAG` 搬到 `shared/clock.py`（`detect/` 的狀態心跳也用，放在 alert 會變成 detect 依賴 incident）；`incident/store/alert.py` 仍 re-export。
- `tests/test_layers.py` 改成新順序（`shared < detect < llm < incident < learn < interface`），索引名字面值只能在 `*/store/` 與 `shared/`；新增 `test_scans_something`——搬完第一次跑時舊名單掃不到任何檔，三個測試是空轉通過的。
- 情境 yaml 的策略字串、文件與註解裡的路徑、CLAUDE.md／README／architecture 的結構段同步。
- 踩到的：第一次改 import 的正則把變數 `scenario`、`schema` 也換掉了（`Repos(es, scenario)` 變成 `Repos(es, common.scenario)`），整批 `git checkout` 還原後，改成只換 `from scenario import` 與完整模組名。

## Check

| 要驗什麼 | 怎麼驗 | 結果 |
|---|---|---|
| 依賴順序沒反 | `test_layers`（加了 `test_scans_something`：掃到的檔要超過 60 個，名單跟資料夾對不上時會紅） | ✅ 只有一處反向（`detect/baseline_job` 用 alert 的 `stamp_of`），搬到 `shared/clock.py` 後清掉 |
| 行為沒變 | 全部單元測試 | ✅ 765 通過、16 跳過（要 helm） |
| 對真 ES 的流程沒壞 | `chain_e2e`、`factory_e2e`、`loop_e2e` | ✅ 26/26、15/15；`loop_e2e` 還是那 2 個舊的過期斷言 |
| 文件連結、索引 | `tools/check-docs.py` | ✅ 0 個問題 |
| 延遲載入的 `Repos` | 所有測試與 e2e 都走它；`REGISTRY` 的 15 個名稱各被用到 | ✅ |

## Act

| 問題 | 在什麼情況下發現的 | 打算 |
|---|---|---|
| `Repos` 的屬性是動態載入，編輯器不會自動補全 `repo.alerts` | 改 `Repos` 時 | 接受；要補全的話在 `repos.py` 加 `TYPE_CHECKING` 的型別宣告（會讓 test_layers 看到反向 import，要先讓測試放行這種寫法） |
| 舊文件裡的 `core/`、`service/` 說法 | 改文件時 | PDCA 紀錄是當時的措辭不改；其他文件的檔案路徑與目錄名已改，少數敘述性文字（例如 `core/traces.py` 這種早就改名的）靠人看 |
| `detect/` 一層 14 個檔、約 2900 行 | 搬完看大小 | 要再拆就拆成 `detect/rules`、`detect/baseline` 等子資料夾，先看使用者要不要 |
| 沒跑 `docker build` 與 helm | 環境沒有 | 部署前補 |
