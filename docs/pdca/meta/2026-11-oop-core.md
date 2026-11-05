# 核心邏輯物件導向化（一）：ES 實作 Backend、Detector 基底與 Anomaly（2026-11）

**狀態**：已完成（單元測試；沒連 ES）　　**日期**：2026-11-05　　**相關**：分支 `claude/oop-core`（從 master `4fd6582` 開）；接 [frameworks](2026-11-frameworks.md)（只換了骨架，核心邏輯沒動）；來源是 code-review 的結構發現
**模組**：④ 判斷、⑤ 異常總表、⑦ 根因分析

## Plan：這一次要達到什麼

- **需求**：使用者要的是繼承、多型、封裝，但 `detector/`、`investigator/` 仍是「函式加字典」：
  1. `sources.Backend` 抽象類別已經有 Memory／File／Http 實作，**ES 自己卻不是 Backend**，所以 18 個讀寫函式開頭都要 `if is_backend(es): …轉交… else: …打 ES…`，多型退化成型別檢查；工廠的 `cmd_paths.py` 還要 `hasattr(es, "es")` 猜包了幾層。
  2. `cmd_rules`、`cmd_peer`、`cmd_quality`（以及 trace 的三種、baseline 的 `save_anomalies`）各自手寫約 15 個欄位的異常文件、各自複製「ensure_index → `--only` 過濾 → 跑每個檢查 → 印 → `write_anomalies` → 印總結」的骨架；異常文件的格式改一次（例如之前加 `engine_version`）要改五處。
- **目的**：
  - ES 成為 `Backend` 的一個實作（`ESBackend` = 既有的 `ESSourceBackend` 讀 ＋ `ESStore` 寫），`open_backend` 永遠回 Backend；原本散在各處的 ES 查詢搬進 `ESBackend`／`ESStore` 的方法。呼叫端只呼叫 `backend.series()`、`backend.load_baseline()`…，**刪掉全部 `is_backend` 分支與 `hasattr(es, "es")`**。測試裡的假 ES（只有 `req`／`search`）照樣能傳：一個 `as_backend(x)` 把「長得像 ES 的東西」包成 `ESBackend`，是唯一的正規化點。
  - `detector/base.py`：`Anomaly` dataclass（異常文件的欄位只定義一處，`to_doc()` 產生寫進 `aiops-anomalies` 的 dict）；`Detector` 抽象基底（template method：`prepare()`、`checks()`、`evaluate(check) -> list[Anomaly]`、`describe()`、`run()`），`RulesDetector`、`PeerDetector`、`QualityDetector` 繼承，`cmd_rules`／`cmd_peer`／`cmd_quality` 變成一行包裝（`(cfg, args, backend)` 簽名不變，排程表、typer、測試都不用改）。trace 的三種偵測與 baseline 的 `save_anomalies` 這輪只改用 `Anomaly.to_doc()`，不繼承 `Detector`（它們的流程不同：trace 一次三種、baseline 依粒度與 status）。
- **範圍**：
  - 做：上面兩件；單元測試對齊（假 ES 不用改寫法）；文件（`sources/README.md`、`plugins/README.md`、`baseline/reference.md` 提到 `is_backend` 或異常欄位的地方）。
  - 不做：檢查種類改成每種一個類別（quality 的 6 種、rules 的 10 種 `if type ==`）；`plugin.current()` 改注入；`cfg.get` 153 處與模型預設值；行為、ES 查詢語句、異常文件的欄位內容都不變。

## Do：改動哪些 code、改了什麼、為何要改

### 一、ES 實作 `Backend`

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `sources/base.py`（120 → 148 行） | `as_backend(x)`：是 Backend 原樣回，長得像 ES（有 `req`／`search`／`bulk_index`）就包成 `ESBackend`，其他丟 `TypeError`；刪 `is_backend`。`Backend` 加抽象 `source_rows(inp, start, end)`、`raw_search`／`raw_req`（非 ES 丟 `NotImplementedError`）；`Store` 加 `similar_incidents(fp, statuses, exclude_id, size)`（預設＝`incidents`）、`incidents(..., having=())` | 唯一的正規化點，取代 18 處 `if is_backend(es)`。測試裡只有 `req`／`search` 的假 ES 不用改寫法 |
| `sources/es_source.py`（新，220 行） | `ESSourceBackend` 從 `rows.py` 搬來，原本散在各處的 ES 查詢搬成它的方法：`series`（原 `detector.es._fetch_one`）、`alive_times`、`points`（原 `rules.fetch`／`_fetch` 含 join）、`spans`／`spans_by_id`（原 `core.traces`）、`source_rows`（原 `cmd_ingest._es_rows`）、`raw_search`／`raw_req`。`ESBackend(ESSourceBackend)`：`__init__(es)` 配 `ESStore(es)`，`.es` 是原始連線 | ES 的讀法集中在一個類別；detector／core 的 helper 在方法內 lazy import，`sources` 頂層不 import `detector`（避免繞圈） |
| `sources/stores.py`（163 → 199） | `ESStore` 收進原始 ES 寫法：`load_baseline`／`load_group_baselines`（原 `detector/es.py`）、`save_baseline`（原 `cmd_baseline`，含 `_delete_by_query`）、`anomaly_status`（原 `_keep_upgraded` 的 `_mget`）、`adjustments`（含讀不到時的 ⚠ 警告）、`write_anomalies`（`load_owners`＋`owner_of` 補 dept／group）、`incidents(having)`、`similar_incidents`（用 `similar.query`）。`BASELINE_COLUMNS`／`_RENAMED` 成為唯一定義 | ES 的寫法集中在一個類別；原本「各寫一份」的欄位表只留一份 |
| `sources/rows.py`、`sources/__init__.py` | `RowsBackend.source_rows`（原 `cmd_ingest._backend_rows`）；`open_backend` 來源與存放都是 es 時回 `ESBackend(es_factory())`，不再回裸 ES | 指令拿到的永遠是 Backend |
| `detector/es.py`（259 → 167）、`detector/rules.py`（259 → 218）、`core/traces.py`（256 → 206）、`detector/cmd_ingest.py`（105 → 72） | `fetch`／`alive_times`／`load_baseline`／`load_group_baselines`、`rules.fetch`、`spans_of`／`spans_by_id`、`read_rows` 各變一行 `as_backend(es).<方法>(...)`；刪掉搬走的實作 | 函式名與簽名保留，呼叫端（含另一組同時在改的 cmd_*）不用動 |
| `detector/owner.py`、`detector/adjust.py`、`detector/cmd_baseline.py`、`investigator/similar.py`、`investigator/cmd_alert.py`、`plugins/factory/cmd_paths.py` | 各自的 `if is_backend` 分支刪掉：`write_anomalies` 先蓋 `engine_version` 再交給 Store（**修了 Memory／File 路徑漏 `engine_version` 的 bug**）；`adjust.load` → `.adjustments()`；`save_baseline`／`_keep_upgraded` → `.save_baseline`／`.anomaly_status`；`tune` 的 store 檢查改 `es.store.kind != "es"`；`similar.search` → `.similar_incidents(...)`、`_confirmed_cases` → `incidents(having=…)`；`cmd_alert` 的換版查詢改 `be.kind == "es"`；`cmd_paths` 用 `raw_search`／`raw_req`，刪 `hasattr(es, "es")` | 多型：呼叫端只叫方法。`cmd_paths`、`cmd_alert` 仍有 `kind == "es"`，那是依後端能力分流（只有 ES 能放 template、查換版），不是型別檢查 |

### 二、`Anomaly` 與 `Detector`

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/base.py`（新，185 行） | `Anomaly` dataclass：異常文件的全部欄位（必填 `ts, series, keys, value, level`，其餘 `None`）；`to_doc()` 產生寫入的 dict，一定寫的鍵、有值才寫的鍵、鍵的順序都跟五種舊寫法一樣；`id()` ＝ `doc_id(series, keys, ts)`。`Detector(ABC)` template：`connect → prepare → stamp → checks → selected(--only) → evaluate → on_error → after_check → doc_id → write（一次）→ summary → exit_code` | 異常文件只定義一處（docstring 有欄位表，`ANOMALY_MAPPING`、`RULE_FIELDS` 要跟它一致，測試會對）；三個偵測指令的骨架只寫一次 |
| `detector/cmd_rules.py`（81 → 75）、`cmd_peer.py`（70 → 68）、`cmd_quality.py`（72 → 64） | `RulesDetector`、`PeerDetector`、`QualityDetector` 繼承 `Detector`，只寫 `checks()`、`evaluate()` 與各自的差異（rules：`on_error` 記失敗、`summary` 列失敗、`exit_code` 1；peer：`stamp` 用 `now.floor("1min")`、`after_check` 印「N 組、M 個 Pod 比過」；quality：`interval_default` 10m）；`cmd_*(cfg, args, backend=None)` 變一行包裝 | 簽名不變，typer、排程表、測試都不用改。`cmd_quality` 多了 `backend` 參數、改走 `open_backend`（原本寫死 `ES()`，是 code-review 列的 bug） |
| `detector/cmd_trace.py`、`detector/cmd_baseline.py::save_anomalies` | 四處手寫 dict → `Anomaly(...).to_doc()`；trace 的 clock_skew `_id` 維持舊算法 | 格式統一；不繼承 `Detector`（流程不同，計畫就這樣切） |
| `tests/test_peer.py`、`tests/test_sources_backend.py` | patch 的目標從 `cmd_peer.ES`／`write_anomalies` 改成 `detector.base`；`open_backend` 沒寫 source 的斷言改成回 `ESBackend` | 跟著結構 |
| 文件：`sources/README.md`、`sources/base.py` docstring、`configs/baseline/reference.md`（指向 `base.Anomaly`） | 對齊新設計 | — |

### 行數

| | 之前 | 之後 |
|---|---|---|
| 非測試 Python 總行數 | 12,097 | 12,286（+189） |
| 被改的 20 個檔 | — | −520 ＋306（淨 −214） |
| 新檔 | — | `base.py` 185、`es_source.py` 220 |

總行數沒有減少：原本散在各處的 ES 查詢是**搬**進 `es_source.py`／`ESStore`，不是刪；`Detector` 基底是新增的。減少的是重複：18 處型別分支變 1 處、異常文件格式 5 處變 1 處、偵測指令骨架 3 處變 1 處。

## Check：測試的目的與結果

`PYTHONUTF8=1 py -m pytest -q` → 769 個通過（master 原本 747）；`tools/check-docs.py` 四項 0 個問題。全部是單元測試，沒連 ES、沒在 k3d 跑。

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 非測試程式裡沒有型別分支了 | `test_es_backend.py::NoTypeBranches` 讀 detector／investigator／core／plugins／sources／api 所有 .py，數 `is_backend(` 與 `hasattr(es, "es")` | 都是 0 |
| 沒寫 source 時指令拿到的是 Backend | `open_backend({...}, factory)` → `ESBackend`，`.es` 是 factory 的物件，`.store.kind == "es"` | 符合 |
| `as_backend` 的三種輸入 | 假 ES → 包起來；`MemoryBackend` → 原樣；`None`／字串 → `TypeError`；`raw_search` 只有 ES 有，Memory 丟 `NotImplementedError`，但 Store 層 `req` 對 Memory 仍回 `{}` | 符合 |
| 搬走的 ES 查詢行為不變 | `fetch` 經 `ESBackend` 仍把 `by` 改成 `keys` 名；`rules.fetch` 的 join 仍補分母；`similar.search` 打的 body 等於 `query()`（status terms＋size）；`_confirmed_cases` 帶 exists filter | 符合；`test_similar` 原本對 `size`／`CANDIDATES` 的斷言照過 |
| `engine_version` 的回歸 | `MemoryBackend` 經 `owner.write_anomalies` 有 `engine_version`、dept None、group unassigned；`ESBackend(FakeES)` 有 dept／group 與 `engine_version` | 符合（之前 Memory／File 路徑漏掉） |
| 異常文件跟改之前一字不差 | `test_detector_base.py::ToDoc`：rules／peer／quality／baseline／trace 五種樣式，期望值是從舊程式抄下來的 dict 字面值，比鍵、值、順序 | 五種都相同 |
| mapping 與 dataclass 不會漂 | mapping 欄位（扣掉 owner／investigator 補的）⊆ dataclass；全填的 `to_doc()` 鍵 ⊆ mapping ∪ RULE_FIELDS ∪ {since} 且 ＝ dataclass 欄位 | 符合 |
| `Detector` template 的順序與行為 | 假 backend：prepare → checks → evaluate×N → write 一次（全部 docs）→ 回 0；`--only` 只跑那一個；沒觸發不呼叫 write；預設 `on_error` 會 raise；rules 式回 1、只跳過壞的那條 | 符合 |
| 既有的指令測試不用改就過 | `test_cmd_rules`、`test_detect_status`（monkeypatch `cmd_baseline.ES`）、`test_quality`、`test_trace`、`test_cli` 的 31 條字面指令、各種只有 `req` 或 `search` 的 FakeES | 全過；只有 `test_peer` 的 patch 目標、`test_sources_backend` 的一個斷言跟著結構改 |

沒測到的：沒連真的 ES（搬過去的查詞是同一段程式碼，但沒對 ES 跑過）；k3d 沒部署。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [④] 行數沒減（+189） | 算行數時 | 使用者的目標之一是程式變少；這輪是搬與去重，不是刪 | 下一輪：檢查種類改成每種一個類別（會刪 `if type ==` 鏈與重複的 `c.get`）、模型填預設值後刪 153 處 `cfg.get(..., 預設)` |
| [④] `cmd_paths`、`cmd_alert` 仍有 `kind == "es"` 兩處 | 放 index template、查換版只有 ES 做得到 | 是依能力分流，不是型別檢查；但仍是分支 | 要收乾淨可以在 `Backend` 加 `put_template()`／`change_at()` 預設不做 |
| [④] trace 的三種偵測、baseline 的 detect 沒繼承 `Detector`，只改用 `Anomaly` | 計畫就這樣切（流程不同） | 骨架仍是自己的 | 下一輪看要不要抽 `TraceDetector` |
| [④] rules 總結的「N 條規則」算全部規則（含 `--only` 濾掉的），peer／quality 算濾後的 | 寫基底時對照舊行為 | 只是印出來的數字不一致 | 沿用舊行為，沒改 |
| [⑨] `docs/pdca/meta/2026-11-model-modules.md` 第 58、83 行仍寫「ES 本身不是 Backend」 | 它是當時的 PDCA | 依 CLAUDE.md 不回頭改 | 現況看 `sources/README.md`、`sources/base.py` |
| 沒連真的 ES | 這輪只有單元測試 | 搬過去的查詞沒對 ES 跑過 | 合入前在 k3d 跑一輪 rules／detect／peer／quality／alert |

- **決定**：ES 成為 `Backend` 的一個實作，`as_backend` 是唯一正規化點；異常文件只定義在 `base.Anomaly`；rules／peer／quality 繼承 `Detector`。行為、查詞、文件欄位全部不變，由五種樣式的 dict 字面值比對守住。
- **下一輪**：檢查種類各一個類別（quality 6 種、rules 10 種）；模型預設值集中、刪 `cfg.get`；`plugin.current()` 改注入。
