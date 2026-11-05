# 核心邏輯物件導向化（一）：ES 實作 Backend、Detector 基底與 Anomaly（2026-11）

**狀態**：進行中　　**日期**：2026-11-05　　**相關**：分支 `claude/oop-core`（從 master `4fd6582` 開）；接 [frameworks](2026-11-frameworks.md)（只換了骨架，核心邏輯沒動）；來源是 code-review 的結構發現
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

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
