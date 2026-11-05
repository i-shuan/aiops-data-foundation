# engine 換三個框架：pydantic（設定）、typer（命令列）、FastAPI（api）（2026-11）

**狀態**：進行中　　**日期**：2026-11-05　　**相關**：分支 `claude/frameworks`（從 master `23bbcc7` 開）；來源是 code-review 的結構發現（設定是裸 dict、`cfg.get` 散 153 處；argparse 217 行加手動維護的 `cmds` dict；api 用標準庫 `http.server` 自己處理路由、JSON、CORS、錯誤）
**模組**：④～⑨

## Plan：這一次要達到什麼

- **需求**：程式可讀性不夠，骨架的部分（設定、命令列、HTTP）全是自己寫的，每加一個設定欄位、子命令、端點都要改好幾處，而且 yaml 打錯欄位不會報錯。
- **目的**：三層各換成一個穩定、常見、內網 Nexus 一定有的框架，一次換完：

| 層 | 現在 | 換成 | 做完之後 |
|---|---|---|---|
| 設定 | `yaml.safe_load` 成裸 dict，各處 `cfg.get(..., 預設)` | pydantic：每種設定一個模型，`core/config.py::load_config(path, kind)` 載入並驗證 | yaml 多打、打錯欄位啟動就報錯並指出欄位；13 份設定都要載得過 |
| 命令列 | `detector/__main__.py`、`investigator/__main__.py` 的 argparse，加手動維護的 `cmds` dict | typer：一個子命令一個函式，decorator 註冊；plugin 的子命令也用同一種註冊 | 參數與行為完全不變（排程器與 Celery 都是用 `python3 -m detector <args>` 子行程呼叫） |
| api | `api/__main__.py` 的 `http.server`，自己做路由、CORS、JSON、錯誤碼 | FastAPI＋uvicorn：路由用 decorator，CORS 用 middleware，錯誤用 HTTPException | 端點、路徑、回應格式、狀態碼不變（Grafana 儀表板直接打它）；`python3 -m api` 照舊能啟動 |

- **範圍**：
  - 做：上面三件；`requirements.txt` 加五個套件；單元測試改成對框架測（FastAPI 的 `TestClient`、typer 的 `CliRunner`）；Dockerfile、chart 的 `args` 不用改（入口還是 `python3 -m <套件>`）。
  - 不做：`cfg.get` 那 153 處的呼叫端不在這輪改（`load_config` 回傳的 dict 跟 yaml 相同，先只加驗證；預設值集中到模型是下一輪）；偵測邏輯（`Detector` 基底、`ESBackend`）不動；`scheduler/` 自己的 argparse 不動（只有 64 行）；`api/app.py` 的邏輯不動；`e2e` 腳本不跑。
  - 排程用 Celery 加 Redis 是 master 已經定的（PDCA `2026-11-api-beat-worker.md`），不在這裡重選。

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
