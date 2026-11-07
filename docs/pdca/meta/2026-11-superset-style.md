# 積木的寫法與命名仿造 Superset（superset-ui）（2026-11）

**狀態**：進行中（階段 A）　　**日期**：2026-11-07　　**相關**：分支 `claude/superset-style`（從 master 開）
**模組**：④⑥⑦⑨（積木機制；演算法不動）

## Plan：這一次要達到什麼

- **需求**：現在的積木（`common/blocks.py`）是「主程式手寫一張登記表，表裡指向各自不同形狀的函式」；參數寫在 YAML、說明寫在 docstring、契約寫在文字。使用者要**寫法架構跟 Superset 前端（superset-ui）一樣**：一個外掛是有固定形狀、會自我描述、自己登記自己的單位；**名字也照 Superset 的**（Plugin、Preset、Registry、metadata、controlPanel…）。程式語言不同沒關係，內部細節照我們的。
- **目的**：新增一個積木＝新增一個檔，不改既有檔；積木自帶名字、說明、可調參數（controlPanel）；`main blocks` 從登記表自動印出；YAML 的參數用 controlPanel 驗證，打錯字當場擋。
- **範圍**：
  - 做（階段 A）：`common/core/`（Registry、Plugin、Preset、BlockPlugin、BlockMetadata、ControlPanelConfig）；⑥ 嚴重度的兩個積木（`golden_signals`、`p0`）改成 BlockPlugin；舊的 `blocks.py` 對這一段改讀新登記表，其他段照舊。
  - 之後（B～E，各自另開一輪）：B 其餘各段搬完、刪舊登記表；C ④ 判斷的指令抽成 `run`；D 情境改成 `Preset`；E 新增 ⑧ 輸出段、統一設定檔與 `FEATURE_FLAGS`。
  - 不做：React 元件、畫面、RBAC、Flask-AppBuilder 的 api／schemas／commands／dao 那一套。

### 命名對照（Superset → 我們；Python 用 snake_case，名字的詞照搬）

| Superset（superset-ui） | 我們 | 說明 |
|---|---|---|
| `Registry`、`registerValue`／`registerLoader`／`get`／`has`／`keys`／`values`／`entries`／`remove`／`clear`／`size` | `Registry`：`register_value`／`register_loader`／`get`／`has`／`keys`／`values`／`entries`／`remove`／`clear`／`size` | `register_loader` 是延遲載入（第一次 `get` 才跑） |
| `OverwritePolicy.ALLOW／WARN／PROHIBIT` | `OverwritePolicy.ALLOW／WARN／PROHIBIT` | 重名時怎麼辦 |
| `Plugin`：`configure(config, replace)`／`resetConfig`／`register`／`unregister` | `Plugin`：`configure`／`reset_config`／`register`／`unregister` | `Plugin().configure({"key": "x"}).register()` |
| `ChartPlugin` | `BlockPlugin(stage, metadata, control_panel, run／load_run)` | 一個積木 |
| `ChartMetadata`（name、description、tags、category、behaviors、credits、deprecated） | `BlockMetadata`（同欄位，沒有 thumbnail） | 積木自我描述 |
| `ControlPanelConfig`（`controlPanelSections`、`controlOverrides`、`formDataOverrides`） | `ControlPanelConfig`（`control_panel_sections`、`control_overrides`、`form_data_overrides`） | 宣告可調參數 |
| `ControlPanelSectionConfig`（label、description、expanded、`controlSetRows`） | `ControlPanelSectionConfig`（`label`、`description`、`expanded`、`control_set_rows`） | 參數分組 |
| `sharedControls`、control 的 `type`（`TextControl`、`SelectControl`、`CheckboxControl`…）、`default`、`label`、`description`、`choices` | `shared_controls`、`ControlConfig(type, label, description, default, choices, min, max)` | 常用的參數共用 |
| `formData`（使用者填的值） | `form_data`（YAML 裡這個積木的參數）；`ControlPanelConfig.build_form_data(raw)` 補預設、轉型、驗證 | 打錯字、型別錯丟 `ValueError` |
| `getChartMetadataRegistry()`、`getChartControlPanelRegistry()`、`getChartComponentRegistry()` | `get_block_metadata_registry()`、`get_block_control_panel_registry()`、`get_block_run_registry()` | 三張登記表；key＝`<段>.<名字>`（例 `severity.golden_signals`） |
| `Preset({name, description, presets, plugins})`、`.register()` | `Preset(name, description, presets, plugins)`、`.register()` | 一包外掛一起登記；階段 D 情境改成 Preset |

### 階段 A 的搬移對照

| 現在 | 之後 |
|---|---|
| `common/blocks.py` 的 `Stage("severity", {"golden_signals": ("incident.alert_severity:golden_signals", 說明)…})` | `common/block_plugins/severity.py`：`BlockPlugin(stage="severity", metadata=…, control_panel=…, load_run=…).configure({"key": "golden_signals"})`；`main_preset` 把它們 `register()` |
| 說明字串寫在登記表 | `BlockMetadata.description` |
| 參數寫在 YAML、說明寫在 docstring | `control_panel`：`golden_signals` 宣告 `window`、`min_coverage`、`min_duration`、`min_span_critical`、`min_points_per_class`、`deploy_grace`；`p0` 宣告 `p0_services` |
| `BLOCKS.stage("severity").loaded(sc)` 回 `[(名字, 函式)]` | 介面不變（舊呼叫端不用改）；內部改讀 `get_block_run_registry()` |
| `main blocks` 印名字＋說明 | 多印每個積木的可調參數（名字、型別、預設） |

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
