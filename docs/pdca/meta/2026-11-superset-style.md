# 程式架構仿造 Superset（superset-ui）：core、plugins、presets（2026-11）

**狀態**：計畫（還沒動程式）　　**日期**：2026-11-07　　**相關**：分支 `claude/superset-style`（從 master 開）
**模組**：④⑥⑦⑧⑨（積木機制與檔案結構；演算法本身不改）

## Plan：這一次要達到什麼

- **需求**：現在的積木（`common/blocks.py`）是「主程式手寫一張登記表，表裡指向各自不同形狀的函式」；參數寫在 YAML、說明寫在 docstring、契約寫在文字，演算法散在 `detect/`、`incident/`、`learn/`。使用者要**程式架構與命名照 Superset 前端（superset-ui）**：框架、外掛、主程式三塊分開；一個外掛是有固定形狀、會自我描述、由主程式登記的單位；名字照 Superset（Plugin、Preset、Registry、metadata、controlPanel…）。程式語言不同沒關係，內部細節照我們的。
- **目的**：
  - 新增一個外掛＝新增一個資料夾，不改既有檔。
  - 外掛自帶名字、說明、可調參數（control panel）；`main blocks` 從登記表自動印出；YAML 的參數用 control panel 驗證，打錯字當場擋。
  - 主程式（`detect/`、`incident/`、`learn/`）只做編排：準備 props、從登記表拿外掛、照順序跑、寫結果；不認得任何一個外掛。
  - 之後能把外掛做成外部 pip 套件（階段 F）。
- **範圍**：
  - 做：下面六個階段，**一個階段一輪**（各自 commit、各自驗證）。
  - 不做：React 元件、畫面、RBAC、Flask-AppBuilder 的 api／schemas／commands／dao 那一套。

### 整體結構（`aiops-engine/src/`）

三塊對應 Superset 的三塊：**框架**（`@superset-ui/core`）、**外掛**（`superset-frontend/plugins/`）、**主程式**（`superset-frontend/src/`）。

```
src/
├─ core/                    框架（對應 @superset-ui/core；不認得任何業務、不 import 任何其他層）
│  ├─ models/               plugin.py  preset.py  registry.py
│  ├─ pipeline/
│  │  ├─ models/            plugin.py（PipelinePlugin）  metadata.py（PipelineMetadata）  props.py（PipelineProps）
│  │  └─ registries/        metadata.py  control_panel.py  run.py
│  └─ controls/             types.py（ControlPanelConfig、ControlPanelSectionConfig、ControlConfig）  shared_controls.py
├─ plugins/                 外掛（對應 superset-frontend/plugins/）
│  ├─ plugin_detect/        dynamic_baseline/ peer_compare/ threshold_rule/ chronic_issue/ quality_check/ level_shift/ trace_latency/   ④
│  ├─ plugin_grouping/      same_entity/                                                                        ⑥
│  ├─ plugin_severity/      golden_signals/ p0/                                                                 ⑥
│  ├─ plugin_rca/           anomalies/ entries/ segments/ slowest/ logs/ changes/ topology/ conclusion/          ⑦
│  ├─ plugin_similar/       fields/ vector/                                                                     ⑦
│  ├─ plugin_hints/         segment_hints/ checklist/                                                           ⑦
│  ├─ plugin_summary/       llm/                                                                                ⑦
│  ├─ plugin_suggest/       false_positive/ missed/ precision/ rule_upgrade/ from_text/                         ⑨
│  └─ plugin_notify/        grafana/ webhook/                                                        ⑧（新增）
├─ presets/                 對應 visualizations/presets/：main_preset.py（MainPreset）  observability.py  factory.py  metrics_only.py
├─ common/                  主程式共用：clock、config、infra（es、pg、repos、prune、pack）、types
├─ data/                    ① 原始資料：來源、欄位對照、trace 合成
├─ detect/                  ④⑤ 編排：讀資料、跑 plugin_detect、寫異常總表
├─ incident/                ⑥⑦⑧ 編排：收斂、跑併件／嚴重度／報告段、寫告警
├─ llm/                     LLM、向量的連線（給外掛用的工具）
├─ learn/                   ⑨ 編排：回饋、跑 plugin_suggest、回測、核准
└─ interface/               入口：cli、web（api）、jobs（scheduler、worker）；啟動時 MainPreset().register()
```

**依賴方向**（只能往下 import；`tests/test_layers.py` 跟著改）：

```
core → common → data → detect → llm → incident → learn → plugins → presets → interface
```

### 一個外掛長怎樣（照 `plugin-chart-echarts/src/Timeseries/`）

```
plugins/plugin_severity/
├─ __init__.py              匯出 GoldenSignalsPipelinePlugin、P0PipelinePlugin（對應 src/index.ts）
├─ golden_signals/
│  ├─ __init__.py           GoldenSignalsPipelinePlugin（metadata、control_panel、load_run、transform_props）
│  ├─ control_panel.py      window、min_coverage、min_duration、min_span_critical、min_points_per_class、deploy_grace
│  ├─ transform_props.py    PipelineProps → golden_signals.py 要的參數
│  ├─ types.py              這個外掛自己的型別
│  └─ golden_signals.py     真正做事的（現在 incident/alert_severity.py 的 grade；對應 EchartsTimeseries.tsx）
└─ p0/ …
```

### 檔名對照

| superset-ui | 我們 |
|---|---|
| `superset-ui-core/src/models/` `Plugin.ts` `Preset.ts` `Registry.ts` | `core/models/` `plugin.py` `preset.py` `registry.py` |
| `superset-ui-core/src/chart/models/` `ChartPlugin.ts` `ChartMetadata.ts` `ChartProps.ts` | `core/pipeline/models/` `plugin.py` `metadata.py` `props.py` |
| `superset-ui-core/src/chart/registries/` `ChartMetadataRegistrySingleton.ts` 等 | `core/pipeline/registries/` `metadata.py` `control_panel.py` `run.py` |
| `superset-ui-chart-controls/` `ControlPanelConfig`、`sharedControls` | `core/controls/` `types.py` `shared_controls.py` |
| `plugins/plugin-chart-echarts/`（一包多個圖表，`src/index.ts` 匯出全部） | `plugins/plugin_severity/`（一包多個外掛，`__init__.py` 匯出全部） |
| `plugin-chart-echarts/src/Timeseries/` `index.ts` `controlPanel.tsx` `transformProps.ts` `buildQuery.ts` `types.ts` `EchartsTimeseries.tsx` | `plugin_severity/golden_signals/` `__init__.py` `control_panel.py` `transform_props.py` `build_query.py`（需要時） `types.py` `golden_signals.py` |
| `visualizations/presets/MainPreset.js`、`setupPlugins()` | `presets/main_preset.py`（`MainPreset`）；入口啟動時 `MainPreset().register()` |
| 外部 npm 套件 `@superset-ui/plugin-chart-xxx` | 外部 pip 套件 `aiops-plugin-xxx` |

跟 Superset 不同的三處，都是 Python 的限制：模組名不能有 `-`（`plugin-chart-echarts` → `plugin_severity`）；不需要 `src/` 一層；外掛資料夾用小寫（`Timeseries` → `golden_signals`）。檔名去掉重複的種類前綴（`core/pipeline/registries/metadata.py`，不寫成 `pipeline_metadata_registry_singleton.py`）：TypeScript 一個類別一個檔、檔名要帶種類，Python 靠模組路徑就分得出來。

### 類別與方法（詞照搬，Python 用 snake_case）

| superset-ui | 我們 |
|---|---|
| `Registry`：`registerValue` `registerLoader` `get` `has` `keys` `values` `entries` `remove` `clear` | `Registry`：`register_value` `register_loader` `get` `has` `keys` `values` `entries` `remove` `clear` |
| `OverwritePolicy.Allow／Warn／Prohibit` | `OverwritePolicy.ALLOW／WARN／PROHIBIT` |
| `Plugin`：`configure` `resetConfig` `register` `unregister` | `Plugin`：`configure` `reset_config` `register` `unregister` |
| `ChartPlugin({ metadata, controlPanel, loadChart, transformProps })` | `PipelinePlugin(metadata=, control_panel=, load_run=, transform_props=)` |
| `ChartMetadata`（name、description、tags、category、behaviors、credits、deprecated、thumbnail） | `PipelineMetadata`（同，沒有 thumbnail） |
| `ChartProps` | `PipelineProps`（外掛收到的東西：form_data、這一段的資料、現在時間…） |
| `ControlPanelConfig`（`controlPanelSections`、`controlOverrides`、`formDataOverrides`） | `ControlPanelConfig`（`control_panel_sections`、`control_overrides`、`form_data_overrides`） |
| `ControlPanelSectionConfig`（label、description、expanded、`controlSetRows`） | `ControlPanelSectionConfig`（`label`、`description`、`expanded`、`control_set_rows`） |
| control 的 `type`（`TextControl`、`SelectControl`、`CheckboxControl`…）、`default`、`label`、`description`、`choices` | `ControlConfig(type, label, description, default, choices)`，type 名字照抄 |
| `formData` | `form_data` |
| `getChartMetadataRegistry()` `getChartControlPanelRegistry()` `getChartComponentRegistry()` | `get_pipeline_metadata_registry()` `get_pipeline_control_panel_registry()` `get_pipeline_run_registry()`；key＝`<段>.<名字>` |
| `new EchartsTimeseriesChartPlugin().configure({ key: 'echarts_timeseries' }).register()` | `GoldenSignalsPipelinePlugin().configure({"key": "golden_signals"}).register()` |
| `Preset({ name, description, presets, plugins })` | `Preset(name=, description=, presets=, plugins=)`；`MainPreset` 繼承它 |

### 跟現在比的三個關鍵變動

| 變動 | 現在 | 之後 | 理由 |
|---|---|---|---|
| 演算法搬進外掛 | 在 `incident/alert_severity.py`、`detect/strategy/` 等處，登記表指過去 | 放進各外掛資料夾；`detect/`、`incident/`、`learn/` 只剩編排 | 外掛自成一包，才能抽成外部套件 |
| 誰負責登記 | `common/blocks.py` 自己手寫表 | 入口啟動時 `MainPreset().register()`（Superset 的 `setupPlugins()`）；測試在 `conftest` 登記 | 主程式只認登記表 |
| 框架放最底層 | 沒有框架 | 獨立的 `core/`，不依賴任何其他層 | 對應獨立套件 `@superset-ui/core`；之後抽成 SDK 最方便 |

### 分階段（一階段一輪）

| 階段 | 做什麼 | 風險 |
|---|---|---|
| A | `core/` 骨架＋`plugin_severity`（`golden_signals`、`p0`）當樣板＋`presets/main_preset.py`；舊的 `blocks.py` 對嚴重度改讀登記表，其他段照舊 | 低：2 個外掛 |
| B | `plugin_grouping`、`plugin_rca`、`plugin_similar`、`plugin_hints`、`plugin_summary`、`plugin_suggest` | 中：約 20 個外掛，行為不變 |
| C | `plugin_detect`（現在是子指令，要抽成 run，指令變成薄薄一層；牽動排程） | 中高 |
| D | 情境改成 `presets/`（observability、factory、metrics_only），刪 `common/blocks.py` 與情境檔的 `blocks:` | 中 |
| E | 新增 `plugin_notify`（`grafana` 維持現狀、`webhook`） | 低 |
| F | 外部外掛套件：`core/` 抽成 SDK、entry points 自動發現、image 帶外掛套件、範例套件 | 中 |

每階段都要：行為不變（既有測試全過）、`main blocks` 印得出來、`tests/test_layers.py` 依新的依賴方向驗證。

## Do：改動哪些 code、改了什麼、為何要改

還沒動程式。先前試做過一版（`common/core/`、`BlockPlugin` 等舊命名）已整批丟掉，不留。

## Check：測試的目的與結果

還沒開始。

## Act：Check 後沒改善的、還沒解決的

還沒開始。
