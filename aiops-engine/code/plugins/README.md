# plugin：選一個情境，或自己寫一個

> **類型：操作 How-To**　原理（為什麼拆成主程式＋plugin、擴充點怎麼挑）見 [docs/architecture.md §4.2](../../../docs/architecture.md)・紀錄 [docs/pdca/meta/2026-11-plugin-architecture.md](../../../docs/pdca/meta/2026-11-plugin-architecture.md)

engine 一份程式；「IT 可觀測性」「全套工廠」「只有指標」各是一個 plugin，環境變數 `AIOPS_PLUGIN` 選。主程式（`core/`、`detector/`、`investigator/`、`api/`）只透過 [core/plugin.py](../core/plugin.py) 的 `current()` 問 plugin，不認得 `factory`、`lot`、`equipment` 這些字；中立程式裡對象叫 `entity`（服務／機台）、單位叫 `unit`（請求／lot）、把單位串起來的叫 `path`（trace／履歷）、「在哪裡」叫 `where`（API／recipe）。

情境專用的程式全在 plugin 底下、一個功能一個資料夾（引擎、`command.py` 子命令、`config.py` 設定模型）：`observability/` 有 `peer/`、`quality/`、`chronic/`、`trace/`（`trace.py` 分段 baseline、`trace_merge.py` 合成）、`profiles.py`（欄位對照、用語、黃金訊號）、`chain.py`、`changes.py`；`factory/` 有 `ingest/`、`paths/`、`chain.py`、`changes.py`；`metrics_only/` 繼承 factory。刪掉 `plugins/factory/`（連同繼承它的 `metrics_only/`）或 `plugins/observability/`，其餘程式與測試照跑（`tools/check-layering.py` 量分層）。

## 選哪個

| `AIOPS_PLUGIN` | 什麼情境 | 設定檔 | chart |
|---|---|---|---|
| `observability`（預設） | IT：對象是服務、API 是 method＋route、trace 的呼叫關係決定誰連帶誰、版號第一次出現當換版。原始資料再用 `AIOPS_SOURCE` 選 `otel`／`apm` | `configs/schedule/`、`configs/baseline/`、`configs/rca/`；apm 用 [configs/apm/](../configs/apm/README.md) | `--set plugin=observability [--set source=apm]` |
| `factory` | 工廠：對象是機台、API 是 recipe、ML／FDC 判好的異常接進 ⑤、MES＋S6F11 合成履歷取代 trace、後站依 lot 併進前站、recipe 變更當換版、嚴重度依參數家族 | [configs/factory/](../configs/factory/README.md) | `--set plugin=factory` |
| `metrics_only` | 只有 ML／FDC 的異常、**沒有履歷**：同機台一件、不併跨機台、報告只有異常／log／事故前的變更 | `configs/metrics_only/`（`ingest.yaml` 跟 factory 共用） | `--set plugin=metrics_only` |
| `套件.模組:類別` | 客製（下節） | 自己放 | `--set plugin=套件.模組:類別` |

`AIOPS_DOMAIN` 已經不用了（用語、分類表跟著 plugin）。

## 三個內建各填了什麼

| 擴充點 | observability | factory | metrics_only（繼承 factory） |
|---|---|---|---|
| `source_profile()` 索引、欄位對照 | 依 `AIOPS_SOURCE`：otel／apm | `eqp-master`（機台主檔）、`eqp-logs-*` | 同 factory |
| `domain()` 用語、分類表 | 服務／請求／Trace；db、network、deploy… | 機台／lot／履歷；equipment、process_drift、recipe… | 同 factory |
| `entity_keys` 對象在 keys 的哪個欄位 | `service`、`entry.service`、`service.name`… | `equipment` | 同 |
| `where_of()` 「在哪裡」 | 哪支 API：`entry.name`、method＋route、APM 的 `route` | 哪個 `recipe` | 同 |
| `chain()` 跨對象併件的依據 | trace 的呼叫關係（`call_edges`） | 履歷的前後站（`lot_links`） | 不併 |
| `change_at()` 「換版」 | 版號第一次出現（`deploy_at`） | recipe 變更紀錄最近一筆 | 同 factory |
| `severity_classes` 嚴重度的訊號分類 | 黃金訊號（延遲、錯誤、往外呼叫、飽和；`profiles.py`） | 參數家族（pressure、temperature、flow、power、time、metrology、alarm） | 同 |
| `critical_levels` | rule、peer、quality | ＋fdc | 同 |
| `sections` 報告有哪幾段 | anomalies、entries、segments、slowest、logs、changes、topology、conclusion | 同 | anomalies、logs、changes、conclusion |
| `report_sections()` 多出來的段 | `investigator/rca/path_sections.py` 的 entries、segments、slowest、topology（有 trace） | 同（履歷跟 trace 同一種格式） | 沒有 |
| `commands()` 這個情境的 detector 子命令 | `peer`、`quality`、`chronic`、`trace-collect`／`trace-train`／`trace-detect`／`trace-merge` | `ingest`、`paths-merge` | `ingest` |
| `schedule` 預設排程表 | `configs/schedule/`（apm：`configs/apm/`） | `configs/factory/schedule.yaml` | `configs/metrics_only/schedule.yaml` |

cfg 可以覆蓋 plugin 的預設：`alerts.critical_levels`、`alerts.severity.classes`、`alerts.chain: false`、換版的索引（`alerts.deploy_index` 等）。報告的 `changes` 段要 cfg 有 `changes.query` 才出。

## 哪些功能在哪個 plugin 能用

| 功能 | observability | factory | metrics_only |
|---|---|---|---|
| ④ 判斷（M1～M3、trace 基準） | ✅ | ❌ 異常由 ML、FDC 判，`ingest` 接 | ❌ 同 factory |
| ⑥ 同對象合成一件、嚴重度、換版上限 | ✅ | ✅ | ✅ |
| ⑥ 跨對象併件 | ✅ 呼叫關係 | ✅ 前後站 | ❌ 沒有履歷，後站（量測）的異常自己開一件 |
| ⑦ 慢在哪一段、共同點、瀑布圖、經過的對象 | ✅ | ✅ 哪一站哪個 chamber、壞 lot 的共同點 | ❌ 報告沒有這幾段 |
| ⑦ log 新樣式、事故前的變更 | ✅（變更要設 `changes`） | ✅ alarm、PM、recipe 變更 | ✅ |
| ⑦ 相似事故分類（M5） | ✅ | ✅ | ✅ |
| ⑧⑨ 通知、回饋、匯出 | ✅ | ✅ | ✅ |

## 自己寫一個

繼承 `core.plugin.Plugin`（或任一內建），覆寫要改的那幾個，放在 `aiops-engine/code` 底下 import 得到的地方：

```python
# myplugins/network.py
from core.plugin import Chain, Plugin as Base
from plugins.observability import Plugin as Observability

class Plugin(Observability):
    name = "network"
    entity_keys = ("device",)                       # 異常的 keys 裡哪個是對象

    def where_of(self, keys):                        # 「在哪裡」：IT 是哪支 API
        return keys.get("interface")

    def chain(self, es, cfg, anomalies, open_alerts, start, now):
        return Chain(calls={(up, down) for up, down in topology_edges(es, cfg)})   # 誰連帶誰：{(上游, 下游)}
```

`AIOPS_PLUGIN=myplugins.network:Plugin`，chart `--set plugin=myplugins.network:Plugin`（image 要含那個套件；排程表 chart 只認三個內建的名字，客製的用 `configOverrides` 掛自己的排程表）。

- `source_profile()` 要回 `{"owner": {...}, "log": {...}}`（欄位見 [plugins/observability/__init__.py](observability/__init__.py) 的 `PROFILES`）；`domain()` 要有 `entity`、`unit`、`trace`、`new_value_hint`、`categories`、`fp_reasons`、`checked`，`categories`、`fp_reasons` 最後一項是 `other`。
- `sections` 只能從 [investigator/rca/report.py](../investigator/rca/report.py) 的 `SECTIONS`（anomalies、logs、changes、conclusion）加 `report_sections()` 回的裡面挑；有 trace／履歷的回 [investigator/rca/path_sections.py](../investigator/rca/path_sections.py) 的 `SECTIONS`（entries、segments、slowest、topology），沒有 trace 時那幾段自己寫一行略過，不用你管；也可以回自己寫的段 `fn(Ctx)`。
- `commands()` 回 `{"子命令名": register}`，`register(app)` 用 `app.command("子命令名", help=…)` 掛一個 typer 函式；函式用 `detector.cli.run(處理函式, 設定模型, locals())`（讀設定 `core.config.load(config, Model)`、組 `argparse.Namespace`、呼叫處理函式 `(cfg, args) -> int`；寫法見 [plugins/observability/peer/command.py](observability/peer/command.py)、[plugins/factory/paths/command.py](factory/paths/command.py)）。內建子命令也是同一種形狀。
- 單元測試：`AIOPS_PLUGIN` 用 `mock.patch.dict(os.environ, …)` 包，`core.plugin.current()` 看到環境變數變了會自己換；寫法見 [tests/test_plugin.py](../tests/test_plugin.py)。

## 怎麼驗

- `python -m pytest -q tests/test_plugin.py`：預設、env 選、客製 dotted path、三個內建各填了什麼、子命令跟著 plugin、報告段跟著 plugin（沒 trace 時 log 段照跑、不炸）。
- `python -m tests.factory_e2e`（有履歷，15 項）、`python -m tests.factory_e2e --plugin metrics_only`（沒履歷，13 項）：都要有測試 ES（[configs/factory/README.md](../configs/factory/README.md)）。
- `helm template x aiops-engine/helmchart --set plugin=<名>`：`AIOPS_PLUGIN` 進 env、排程表對；不認得的名字 render 失敗。
