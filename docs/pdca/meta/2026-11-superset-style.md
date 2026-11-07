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

### 檔案結構（照 superset-ui，`chart` 換成 `pipeline`，檔名短）

Superset 把 `chart` 當「外掛的種類」；我們的種類是 `pipeline`（管線裡的一個環節）。資料夾結構照抄，檔名去掉重複的前綴
（Superset 是 TypeScript 一個類別一個檔、檔名帶種類；Python 用模組路徑就分得出來，所以 `pipeline/models/plugin.py` 不寫成 `pipeline_plugin.py`）。

| superset-ui | 我們 |
|---|---|
| `superset-ui-core/src/models/` `Plugin.ts` `Preset.ts` `Registry.ts` | `common/core/models/` `plugin.py` `preset.py` `registry.py` |
| `superset-ui-core/src/chart/models/` `ChartPlugin.ts` `ChartMetadata.ts` `ChartProps.ts` | `common/core/pipeline/models/` `plugin.py`（`PipelinePlugin`） `metadata.py`（`PipelineMetadata`） `props.py`（`PipelineProps`） |
| `superset-ui-core/src/chart/registries/` `ChartMetadataRegistrySingleton.ts` 等 | `common/core/pipeline/registries/` `metadata.py` `control_panel.py` `run.py`（各一個 `get_pipeline_xxx_registry()`） |
| `superset-ui-chart-controls/` `ControlPanelConfig`、`sharedControls` | `common/core/controls/` `types.py`（`ControlPanelConfig`、`ControlPanelSectionConfig`、`ControlConfig`） `shared_controls.py` |
| `plugins/plugin-chart-echarts/`（一包，裡面多個圖表；`src/index.ts` 匯出全部） | `plugins/plugin_pipeline_severity/`（一包，裡面多個外掛；`__init__.py` 匯出全部）；以後 `plugin_pipeline_detect`、`plugin_pipeline_rca`、`plugin_pipeline_notify` |
| `plugin-chart-echarts/src/Timeseries/` `index.ts` `controlPanel.tsx` `transformProps.ts` `buildQuery.ts` `types.ts` `EchartsTimeseries.tsx` | `plugin_pipeline_severity/golden_signals/` `__init__.py`（`GoldenSignalsPipelinePlugin`） `control_panel.py` `transform_props.py` `build_query.py`（需要時） `types.py` `golden_signals.py`（真正做事的） |
| `visualizations/presets/MainPreset.js` | `presets/main_preset.py`（`MainPreset`） |
| 外部 npm 套件 `@superset-ui/plugin-chart-xxx` | 外部 pip 套件 `aiops-plugin-pipeline-xxx`（Python 的模組名不能有 `-`、也不需要 `src/`，所以 repo 裡是 `plugins/plugin_pipeline_xxx/`；Superset 的圖表資料夾是 PascalCase（`Timeseries`），Python 套件用小寫（`golden_signals`）） |

### 類別與方法（詞照搬，Python 用 snake_case）

| superset-ui | 我們 |
|---|---|
| `Registry`：`registerValue` `registerLoader` `get` `has` `keys` `values` `entries` `remove` `clear` | `Registry`：`register_value` `register_loader` `get` `has` `keys` `values` `entries` `remove` `clear` |
| `OverwritePolicy.Allow／Warn／Prohibit` | `OverwritePolicy.ALLOW／WARN／PROHIBIT` |
| `Plugin`：`configure` `resetConfig` `register` `unregister` | `Plugin`：`configure` `reset_config` `register` `unregister` |
| `ChartPlugin({ metadata, controlPanel, loadChart, transformProps })` | `PipelinePlugin(metadata=, control_panel=, load_run=, transform_props=)` |
| `ChartMetadata`（name、description、tags、category、behaviors、credits、deprecated、thumbnail） | `PipelineMetadata`（同，沒有 thumbnail） |
| `ControlPanelConfig`（`controlPanelSections`、`controlOverrides`、`formDataOverrides`） | `ControlPanelConfig`（`control_panel_sections`、`control_overrides`、`form_data_overrides`） |
| `ControlPanelSectionConfig`（label、description、expanded、`controlSetRows`） | `ControlPanelSectionConfig`（`label`、`description`、`expanded`、`control_set_rows`） |
| control 的 `type`（`TextControl`、`SelectControl`、`CheckboxControl`…）、`default`、`label`、`description`、`choices` | `ControlConfig(type, label, description, default, choices)`，type 名字照抄 |
| `formData` | `form_data` |
| `getChartMetadataRegistry()` `getChartControlPanelRegistry()` `getChartComponentRegistry()` | `get_pipeline_metadata_registry()` `get_pipeline_control_panel_registry()` `get_pipeline_run_registry()`；key＝`<段>.<名字>` |
| `new EchartsTimeseriesChartPlugin().configure({ key: 'echarts_timeseries' }).register()` | `GoldenSignalsPipelinePlugin().configure({"key": "golden_signals"}).register()` |
| `Preset({ name, description, presets, plugins })` | `Preset(name=, description=, presets=, plugins=)`；`MainPreset` 繼承它 |

### 階段 A 的搬移對照

| 現在 | 之後 |
|---|---|
| `common/blocks.py` 手寫 `Stage("severity", {"golden_signals": ("incident.alert_severity:golden_signals", 說明)…})` | `plugins/plugin_pipeline_severity/golden_signals/`、`plugins/plugin_pipeline_severity/p0/` 兩個外掛資料夾；`presets/main_preset.py` 登記 |
| 說明字串寫在登記表 | `PipelineMetadata.description` |
| 參數寫在 YAML、說明寫在 docstring | 各外掛的 `control_panel.py`：`golden_signals` 宣告 `window`、`min_coverage`、`min_duration`、`min_span_critical`、`min_points_per_class`、`deploy_grace`；`p0` 宣告 `p0_services` |
| `BLOCKS.stage("severity").loaded(sc)` 回 `[(名字, 函式)]` | 介面不變（舊呼叫端不用改）；內部改讀 `get_pipeline_run_registry()` |
| `main blocks` 印名字＋說明 | 多印每個外掛的可調參數（名字、type、預設） |

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
