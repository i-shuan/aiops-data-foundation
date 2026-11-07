# 情境（scenario）：選一個，或自己寫一份

> **類型：操作 How-To**　原理（為什麼拆成主程式＋情境、擴充點怎麼挑）見 [docs/architecture.md §4.2](../../../../docs/architecture.md)・紀錄 [docs/pdca/meta/2026-11-plugin-architecture.md](../../../../docs/pdca/meta/2026-11-plugin-architecture.md)、[docs/pdca/meta/2026-11-target-architecture.md](../../../../docs/pdca/archive/meta/2026-11-target-architecture.md)

engine 一份程式；「IT 可觀測性」「全套工廠」「只有指標」各是一份資料檔（同資料夾的 `*.yaml`）加兩個策略，環境變數 `AIOPS_PLUGIN` 選。
主程式（`detect/`、`llm/`、`incident/`、`learn/`、`interface/`）只看 [base.py](base.py) 的 `Scenario` 欄位，不認得 `factory`、`lot`、`equipment` 這些字；
情境在組合根載入一次（`load_scenario()`：cli／web／jobs 的入口、`Repos(ES(), load_scenario())`），往下傳，沒有全域狀態。

## 選哪個

| `AIOPS_PLUGIN` | 什麼情境 | 資料檔 | 設定檔 | chart |
|---|---|---|---|---|
| `observability`（預設） | IT：對象是服務、API 是 method＋route、trace 的呼叫關係決定誰連帶誰、版號第一次出現當換版。原始資料再用 `AIOPS_SOURCE` 選 `otel`／`apm` | `it_otel.yaml`、`it_apm.yaml` | `configs/schedule/`、`configs/baseline/`、`configs/rca/`；apm 用 [configs/apm/](../../../configs/apm/README.md) | `--set plugin=observability [--set source=apm]` |
| `factory` | 工廠：對象是機台、API 是 recipe、ML／FDC 判好的異常接進 ⑤、MES＋S6F11 合成履歷取代 trace、後站依 lot 併進前站、recipe 變更當換版、嚴重度依參數家族 | `factory.yaml` | [configs/factory/](../../../configs/factory/README.md) | `--set plugin=factory` |
| `metrics_only` | 只有 ML／FDC 的異常、**沒有履歷**：同機台一件、不併跨機台、報告只有異常／log／事故前的變更 | `metrics_only.yaml`（`extends: factory`） | `configs/metrics_only/`（`ingest.yaml` 跟 factory 共用） | `--set plugin=metrics_only` |
| `<路徑>.yaml` | 客製：一份自己的資料檔（可 `extends` 內建的） | 自己放 | 自己放 | `--set plugin=/path/x.yaml` |
| `套件.模組:名字` | 客製：程式裡的 `Scenario` 實例（下節） | — | 自己放 | `--set plugin=套件.模組:名字` |

## 一份情境有什麼

| 欄位 | observability | factory | metrics_only（extends factory） | 誰用 |
|---|---|---|---|---|
| `source` 原始資料格式 | `otel`／`apm` | `factory` | 同 | trace 的 schema（`detect/store/models.py` 的 `schema_of`） |
| `owner`、`log` 索引與欄位對照 | 依 `AIOPS_SOURCE` | `eqp-master`（機台主檔）、`eqp-logs-*` | 同 | `data/store/owner_repo.py`、`incident/store/log_repo.py` |
| `domain` 用語、分類表（entity、unit、trace、new_value_hint、categories、fp_reasons、checked） | 服務／請求／Trace；db、network、deploy… | 機台／lot／履歷；equipment、process_drift、recipe… | 同 | 報告、LLM 範本、回饋網頁 |
| `entity_keys` 對象在 keys 的哪個欄位 | `service`、`entry.service`、`service.name`… | `equipment` | 同 | 事件整合、歸屬、根因分析 |
| `route` 「哪支 API」怎麼算 | `http`（`entry.name`、method＋route、APM 的 `route`） | `recipe` | 同 | 事件整合、根因分析挑樣本 |
| `chain` 跨對象併件的策略 | [incident/strategy/it_topology.py](../../incident/strategy/it_topology.py) `CallEdges`（trace 的呼叫關係） | [incident/strategy/fab_process_flow.py](../../incident/strategy/fab_process_flow.py) `LotLinks`（履歷的前後站） | `null`（不併） | `incident/alert_service.py` |
| `change` 「換版」的策略 | [incident/strategy/it_deployment.py](../../incident/strategy/it_deployment.py) `VersionFirstSeen` | [incident/strategy/fab_recipe_change.py](../../incident/strategy/fab_recipe_change.py) `LatestChange`（recipe 變更紀錄） | 同 factory | 嚴重度的換版上限、誤報建議 |
| `severity_classes` 嚴重度的訊號分類 | `null`＝內建黃金訊號（延遲、錯誤、往外呼叫、飽和） | 參數家族（pressure、temperature、flow、power、time、metrology、alarm） | 同 | `incident/alert_severity.py` |
| `critical_levels` | rule、peer、quality | ＋fdc | 同 | 同上 |
| `sections` 報告有哪幾段 | anomalies、entries、segments、slowest、logs、changes、topology、conclusion | 同 | anomalies、logs、changes、conclusion | `incident/rca_report.py` |
| `commands` 多出來的 detector 子命令 | — | `paths-merge`（[interface/cli/fab.py](../../interface/cli/fab.py)） | — | `interface/cli/detect.py` |
| `schedule` 預設排程表 | `configs/schedule/`（apm：`configs/apm/`） | `configs/factory/schedule.yaml` | `configs/metrics_only/schedule.yaml` | `interface/jobs`、api 的 ingest |

cfg 可以覆蓋情境的預設：`alerts.critical_levels`、`alerts.severity.classes`、`alerts.chain: false`、換版的索引（`alerts.deploy_index` 等）。報告的 `changes` 段要 cfg 有 `changes.query` 才出。

## 哪些功能在哪個情境能用

| 功能 | observability | factory | metrics_only |
|---|---|---|---|
| ④ 判斷（M1～M3、trace 基準） | ✅ | ❌ 異常由 ML、FDC 判，`ingest` 接 | ❌ 同 factory |
| ⑥ 同對象合成一件、嚴重度、換版上限 | ✅ | ✅ | ✅ |
| ⑥ 跨對象併件 | ✅ 呼叫關係 | ✅ 前後站 | ❌ 沒有履歷，後站（量測）的異常自己開一件 |
| ⑦ 慢在哪一段、共同點、瀑布圖、經過的對象 | ✅ | ✅ 哪一站哪個 chamber、壞 lot 的共同點 | ❌ 報告沒有這幾段 |
| ⑦ log 新樣式、事故前的變更 | ✅（變更要設 `changes`） | ✅ alarm、PM、recipe 變更 | ✅ |
| ⑦ 相似事故分類（M5） | ✅ | ✅ | ✅ |
| ⑧⑨ 通知、回饋、匯出 | ✅ | ✅ | ✅ |

## 功能積木：選要哪幾塊

④⑥⑦⑨ 每一段的功能是一塊一塊的，情境資料檔的 `blocks:` 選要哪幾塊；**沒寫的段就是預設**（多半是全部，跟沒有這個設定時一模一樣）。看全部有哪些、這個情境開了哪幾塊：

```bash
python3 -m main detector blocks
```

| 段（`blocks.` 底下的鍵） | 內建的積木 | 關掉或換掉時 |
|---|---|---|
| `detect`（④ 判斷） | `dynamic_baseline`、`peer_compare`、`threshold_rule`、`chronic_issue`、`quality_check`、`level_shift`、`trace_latency` | 關掉的那個指令跑了只印一行、回 0（排程照跑不會報錯）；手動調整（排除時段、重置點）是動態基準線的一部分 |
| `grouping`（⑥ 併件，只能一塊） | `same_entity` | 換成自己寫的 `merge(…)` |
| `severity`（⑥ 嚴重度，一串依序跑） | `golden_signals`、`p0` | 拿掉 `p0` 就不升級；可以接自己寫的在後面 |
| `rca_sections`（⑦ 報告段落） | `anomalies`、`entries`、`segments`、`slowest`、`logs`、`changes`、`topology`、`conclusion` | 沒寫 `blocks.rca_sections` 時照情境的 `sections` |
| `similar`（⑦ 相似事故） | `fields`、`vector` | 只開 `fields` 就不算向量 |
| `summary`（⑦ 摘要） | `llm` | 寫空清單 `[]` 就不叫 LLM |
| `hints`（⑦ 歷史 case） | `segment_hints`、`checklist` | 順序就是報告裡出現的順序 |
| `suggest`（⑨ 調整建議） | `false_positive`、`missed`、`precision`、`rule_upgrade`、`from_text` | `feedback suggest` 只跑開著的；`from_text` 關掉時一句話只存原文、不提調整 |

```yaml
blocks:
  detect: [dynamic_baseline, threshold_rule, my_pkg.checks:register]   # 只開這兩種，加一個自己寫的判斷
  severity: [golden_signals]                                           # 不要 P0 升級
  similar: [fields]                                                    # 不算向量
```

**自己寫的積木**填「套件.模組:名字」，每一段要長什麼樣印在 `detector blocks` 的「自己寫的積木」那一行（定義在 [common/blocks.py](../blocks.py) 的 `STAGES`）。寫錯名字，第一次用到那一段時會講清楚有哪些可以填。

## 自己寫一份

最省事：一份 yaml，`extends` 一個內建的、只寫要改的欄位，`AIOPS_PLUGIN=/path/network.yaml`：

```yaml
extends: it_otel
name: network
domain: {entity: 設備, unit: 封包, trace: 路徑, new_value_hint: 新韌體}
entity_keys: [device]                                 # 異常的 keys 裡哪個是對象
route: none
chain: {strategy: "myplugins.network:TopologyEdges"}  # 自己的策略：可呼叫物件 (repo, rca_config, anomalies, open_alerts, start, now) -> Chain（rca_config 是 incident/config/rca.py 的 RcaConfig，不是 dict）
change: null
```

或在程式裡組一個 `Scenario`（`dataclasses.replace(load_scenario("observability"), …)`），`AIOPS_PLUGIN=myplugins.network:SCENARIO`（image 要含那個套件；排程表 chart 只認三個內建的名字，客製的用 `configOverrides` 掛自己的排程表）。

- `owner` 要有 `index`、`namespace`、`service`、`dept`、`group`（主檔沒有時間欄位加 `static: true`）；`log` 要有 `service`、`level`、`trace_id`、`message`（list）、`problem_levels`、`problem_query`。
- `domain.categories`、`fp_reasons` 是 `[代碼, 顯示文字]` 的清單，最後一項要是 `other`；`checked` 是常查的地方。
- `sections` 只能從 [incident/rca_report.py](../../incident/rca_report.py) 的 `SECTIONS` 挑；要 trace 的段在沒有 trace 時自己寫一行略過，不用你管。
- `commands` 是 `{子命令名: "套件.模組:register"}`，`register(app)` 用 `app.command("子命令名", help=…)` 掛一個 typer 函式；函式自己 `load_config(config, kind)`、組 `argparse.Namespace` 再呼叫處理函式 `(cfg, args) -> int`（寫法見 [interface/cli/fab.py](../../interface/cli/fab.py)）。內建子命令也是同一種形狀。
- 策略是「可呼叫物件」：`chain(repo, rca_config, anomalies, open_alerts, start, now) -> Chain（rca_config 是 incident/config/rca.py 的 RcaConfig，不是 dict）`、`change(repo, cfg, entity, since, now) -> datetime | None`；要查 ES 走 `repo.traces`、`repo.change`（[common/README.md](../README.md)），不要自己打查詢。
- 單元測試：`AIOPS_PLUGIN` 用 `mock.patch.dict(os.environ, …)` 包，每次 `load_scenario()` 都重讀；寫法見 [tests/test_scenario.py](../../../tests/test_scenario.py)。

## 怎麼驗

- `python -m pytest -q tests/test_scenario.py`：預設、env 選、yaml 的 `extends`、客製 dotted path、三個內建各填了什麼、子命令跟著情境、報告段跟著情境（沒 trace 時 log 段照跑、不炸）。
- `python -m tests.factory_e2e`（有履歷，15 項）、`python -m tests.factory_e2e --plugin metrics_only`（沒履歷，13 項）：都要有測試 ES（[configs/factory/README.md](../../../configs/factory/README.md)）。
- `helm template x aiops-engine/helmchart --set plugin=<名>`：`AIOPS_PLUGIN` 進 env、排程表對；不認得的名字 render 失敗。
