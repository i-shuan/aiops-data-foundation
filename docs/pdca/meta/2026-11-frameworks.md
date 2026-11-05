# engine 換三個框架：pydantic（設定）、typer（命令列）、FastAPI（api）（2026-11）

**狀態**：已完成（單元測試；k3d 沒部署）　　**日期**：2026-11-05　　**相關**：分支 `claude/frameworks`（從 master `23bbcc7` 開）；來源是 code-review 的結構發現（設定是裸 dict、`cfg.get` 散 153 處；argparse 217 行加手動維護的 `cmds` dict；api 用標準庫 `http.server` 自己處理路由、JSON、CORS、錯誤）
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

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `requirements.txt` | 加 `pydantic>=2.9`、`fastapi>=0.115`、`uvicorn>=0.30`、`typer>=0.20`、`httpx>=0.27` | 三個框架；httpx 給 `fastapi.testclient`；typer 0.20 起自帶 click，`__main__` 才能接 `typer.TyperException` |
| `core/config.py`（改寫）、`core/config_models.py`（新，716 行） | `load_config(path, kind)`：讀 yaml → 剝掉最上層 `_` 開頭的 anchor 鍵 → `MODELS[kind].model_validate` → `model_dump(exclude_unset=True)` → 依原順序組回、`_*` 鍵放回。13 種設定各一個 `BaseModel`，`extra="forbid"`，欄位全選填、不給預設。錯誤一行一個：`<檔>: <欄位路徑>：<問題>`，多出來的鍵會列出該層能寫的欄位 | yaml 打錯欄位要在啟動就被擋。回傳的 dict 刻意跟 yaml 內容一樣（不塞預設值），因為 153 處 `cfg.get(..., 預設)` 這輪不動；預設值集中是下一輪。數字用 `int \| float` 讓 `2` 不會變 `2.0` |
| `detector/__main__.py`（217 → 271 行）、`investigator/__main__.py`（136 → 159） | argparse → typer：一個子命令一個函式、參數就是命令列參數，`@command(name)` 登記進 `COMMANDS`；`run()` 把參數裝成 `argparse.Namespace`（屬性名同以前）、`load_config(kind)`、呼叫原本的 `cmd_*(cfg, args)`。手寫的 `cmds` dict 刪掉；`main(argv) -> int` 照舊，參數錯印用法回 2。investigator 的 similar-eval、import-incidents 從 `main()` 抽成函式 | 子命令的宣告與分派只剩一處；`cmd_*` 的簽名不動，所以 `detector/cmd_*.py` 一行沒改。行數變多是因為每個子命令的參數現在有型別與 help |
| `core/plugin.py`、`plugins/factory/__init__.py`、`plugins/factory/cmd_paths.py` | `commands()` 契約改成 `{子命令名: register(app)}`，`register` 用 `app.command(...)` 掛 typer 函式；`add_parser` 改寫成 `register`，`cmd_paths_merge` 不動 | 內建與 plugin 的子命令用同一種登記，`build_app()` 一個迴圈掛完 |
| `api/server.py`（新，268 行）、`api/__main__.py`（204 → 29 行） | FastAPI `app`、所有路由、自寫的 20 行 CORS middleware、404／405 統一回 `404 not found`、ES 注入點 `state.es`／`set_es()`；`__main__` 只剩 uvicorn launcher（`API_PORT`、`API_TLS_*` 照舊）。JSON 回應仍用 `A.to_json` 的 bytes 原樣送，表單仍 `parse_qs` raw body（`checked` 保留 list）；`docs_url`／`openapi_url` 關掉 | 路徑、狀態碼、body、content-type 要跟原本一字不差（Grafana 儀表板直接打）。不用 Starlette 的 `CORSMiddleware`：原本只對 `/api/*`、每次請求讀 `API_CORS_ORIGIN`、`*` 時回請求的 origin，三點它都對不上 |
| `api/app.py`、`detector/adjust.py`、`scheduler/runner.py` | 自己 `yaml.safe_load` 的四處（series.yaml、ingest.yaml、adjustments_file、calendar_file、schedule）改走 `load_config`；`runner.py` 拿掉沒用的 `import yaml` | 設定只從一個入口進來，才會被驗證 |
| `tests/test_config.py`、`tests/test_cli.py`（新）；`tests/test_api_http.py`、`test_api_endpoints.py`、`test_grafana_form.py`（改 `TestClient`）；`tests/test_plugin.py`（`--help` 不再 SystemExit，4 行） | 見 Check | — |
| 文件：`api/__init__.py`、`detector/__main__.py` docstring、`plugins/README.md`（commands 契約）、`docs/feedback/README.md`（`backtest --id a --id b`）、`configs/baseline/README.md`（pip 那行） | 對齊新寫法 | — |

沒改：`detector/cmd_*.py`、`investigator/cmd_alert.py`、`api/app.py` 的邏輯、`scheduler/__main__.py` 與 `tasks/` 自己的 argparse（64 行、不是公開的命令列）、Dockerfile、chart（入口仍是 `python3 -m detector／investigator／api`）。

## Check：測試的目的與結果

`PYTHONUTF8=1 py -m pytest -q` → 747 個通過（改之前 master 是 720）；`py -W error -c "import core.config, core.config_models, api.__main__, detector.__main__, investigator.__main__"` 沒有警告；`tools/check-docs.py` 四項 0 個問題；`helm template aiops aiops-engine/helmchart` 成功。全部是單元測試與本機實跑，沒在 k3d 上部署。

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 設定驗證不改內容 | `test_config`：`configs/**` 23 份＋ `tests/` 7 份 fixture 共 30 份，`load_config` 的結果 `== yaml.safe_load` 且鍵順序相同；13 種 kind 每種至少一份真檔 | 全部相同 |
| 打錯欄位會被擋、訊息指得出位置 | `peer.yaml` 副本加 `min_peer: 3`；`history_weeks: four`；`adaptive.min_events.cnt`、`alerts.severity.windw`；排程表 `retry:` | 都是 `ConfigError`，訊息含檔名、欄位路徑、該層能寫的欄位（含正確的 `min_peers`／`retries`） |
| anchor 鍵不被當成多餘欄位 | `rules.yaml` 的 `_svc` 原樣留著，展開後的值一致 | 符合 |
| 範圍、列舉、條件式必填 | 10 組：`z` 負數、比例 > 1、rule `type` 不在表內、`window` 型沒 `value`、`compare` 沒 `other`、quality `lag` 沒 `from`… | 各自被擋 |
| 排程器與 Celery 的字面指令照樣能跑 | `test_cli`：讀四張 `schedule.yaml` 共 31 條 `detector`／`investigator` 指令，處理函式與 `load_config` 用假的，逐條斷言到得了處理函式、`args.config` 對、`--window` 值對、`load_config` 收到的 kind 對 | 31 條全過 |
| 參數的屬性名、預設、型別、choices 跟 argparse 一樣 | `tune --show --match a --match b` → `args.match == [a, b]`；`adjust` 位置參數與 `--kind` choices；`feedback add missed` 五欄；`incident --service a --service b`；`metrics --from/--to` → `args.start/end`；`backtest --id` 必填；數值型別；壞 choice 回 2 | 符合 |
| `main()` 當函式用的行為 | `main(["bogus"])`、`main([])` 回 2 不丟例外；`--help` 回 0、列全部子命令；`AIOPS_PLUGIN=factory` 多 `paths-merge`；`import-incidents --dry-run` 沒檔案回 2 | 符合；本機 `py -m detector --help`、`py -m investigator --help` 也看過 |
| api 的 JSON body 一個 byte 都不差 | `/api/alert` 的 `response.content == A.to_json(...)`（中文不轉 `\uXXXX`）；404 的 body 也是 `to_json({"error": ...})` | 相同 |
| 表單的 `checked` 仍是 list | form-urlencoded 送兩個 `checked`，mock `A.submit` 收到 list，其餘欄位是字串 | 符合 |
| 路徑集合與錯誤碼不變 | `GET /nope`、`POST /nope`、`GET /api/feedback`、`POST /health`、`GET /health/`、`/docs` 全部 `404 not found text/plain`；`/feedback`、`/report`、`/missed` 是 `text/html; charset=utf-8`；`/health` 恰好 `text/plain` | 符合 |
| CORS 行為不變 | 允許的 origin 在 `/api/*` 有 `Access-Control-Allow-Origin`＋`Vary: Origin`，不在清單的沒有，`/feedback` 永遠沒有；`OPTIONS /api/feedback` 204、`OPTIONS /feedback` 404；`/api/rca` 的 404 錯誤也帶 CORS header | 符合 |
| 寫入端點的 token | 沒設 `AIOPS_API_TOKEN` 回 401「伺服器沒設…」；token 錯回「token 不對」 | 符合 |
| 真的起一次 api | `API_PORT=18089 py -m api`，curl `/health`、`OPTIONS /api/feedback`、`/nope` | `ok` 純 text、204、404；啟動行印出 |

沒測到的：k3d 上沒部署（chart 只有 `helm template`）；Grafana 儀表板沒對新 api 打過；排程器沒真的跑子行程（只驗了字面指令能解析）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [④⑨] 多值參數寫法變了：`--match a b` → `--match a --match b`（`--match`、`--service`、`--anomaly`、backtest `--id`） | typer 沒有 `nargs="*"`，只有重複 flag | 舊寫法會被當成多一個位置參數、回 2。repo 內的排程表、e2e、文件範例本來就是一個 flag 一個值，只改了 `docs/feedback/README.md` 一處 | 使用者手打指令時要知道 |
| [⑨] `load_config` 回傳的仍是裸 dict，`cfg.get(..., 預設)` 153 處沒動 | 計畫就這樣切 | 預設值仍散在程式裡；模型欄位現在全是 `None` 預設 | 下一輪：模型填真預設、`load_config` 拿掉 `exclude_unset`、呼叫端改 `cfg["x"]` |
| [⑨] 刻意放寬成 `Any`／`str` 的欄位：ES query 片段（`filter`、`value` 聚合、`events`）、`match`、`schedule.jobs[].args`、`alerts.severity.classes`、adjustment `status`／`source`、`timezone`、`cron` | 這些本來就是自由格式或由程式自己驗 | 這些欄位打錯要到執行時才會發現 | 不處理 |
| [⑧] api 跟原本不是 byte-identical 的五點：`OPTIONS` 204 不帶 `Content-Length: 0`；`/feedback`、`/api/feedback` 收到壞 JSON 原本斷線、現在 500；PUT 等方法原本 501、現在 405；HEAD 原本 501、現在同 GET；`server: uvicorn` 取代 `Server: BaseHTTP` | 寫 server.py 時逐條對 | 瀏覽器與 Grafana 都不受影響 | 不處理 |
| [⑧] `starlette.testclient` 對 httpx 1.x 印 `StarletteDeprecationWarning`（要 httpx2） | 跑測試 | 只有警告 | 之後 requirements 換 `httpx2` |
| [④] `--help` 的子命令總表會被 click 截成 `...`（feedback 那條 help 很長） | 本機看 `--help` | 各子命令自己的 `--help` 完整 | 不處理 |
| [④] `scheduler/__main__.py`、`tasks/__init__.py` 仍用 argparse | 範圍外 | 兩處各 60 行、不是公開命令列 | 要一致的話下一輪順手改 |
| 沒在 k3d 部署、Grafana 沒對新 api 打過 | 這輪只有單元測試 | 路由、CORS 都有測，但沒有端到端 | 合入前在 k3d 起一次 api、從 Grafana 送一筆回饋 |

- **決定**：三層一次換，但只換骨架；行為、參數、端點全部不變，由 `test_cli`（31 條字面指令）與 `test_api_*`（bytes 比對）守住。
- **下一輪**：設定預設值集中到模型；`ESBackend` 實作 `Backend` 介面、`Detector` 基底（code-review 的另兩項結構發現）。
