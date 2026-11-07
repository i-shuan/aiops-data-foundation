# engine 改成「主程式＋plugin」：可觀測性、全套工廠、只有指標各一個 plugin（2026-11）

**狀態**：已完成　　**日期**：2026-11-04　　**相關**：分支 `claude/plugin-architecture`（接在 `claude/factory-engine` 後）；操作 [plugins/README.md](../../../aiops-engine/src/common/scenario/README.md)；原理 [architecture.md §4.2](../../architecture.md)
**模組**：④⑥⑦⑨

## Plan：這一次要達到什麼

- **需求**：工廠版做完後，IT 與工廠的差別散在 `if chain_source == …`、`if deploy_kind == …`、`AIOPS_SOURCE`／`AIOPS_DOMAIN` 兩個 env 讀兩個 dict、`investigator/factory.py` 這類「某一邊專用」的模組裡。第三種情境（廠方只有 ML／FDC 的指標、沒有 MES 履歷）要再加一層 if；哪些功能在哪個情境能用，沒有一個地方寫清楚。
- **目的**：Superset 那種結構——**一個主程式定義擴充點，每個情境是一個 plugin 套件**，用一個 env（`AIOPS_PLUGIN`）選；也能自己寫一個掛上去（`AIOPS_PLUGIN=套件.模組:類別`）。做完後：
  - 三個內建 plugin：`observability`（IT，otel／apm）、`factory`（ML／FDC＋履歷）、`metrics_only`（只有指標）。
  - 主程式（`core/`、`detector/`、`investigator/`）不再有 `factory`、`lot`、`equipment`、`recipe` 這些字；專用程式在 `plugins/<名>/`。
  - 行為不變：既有單元測試、工廠 e2e 15/15 照過；`AIOPS_PLUGIN` 沒設時等於今天的 otel。
  - 「只有指標」那條路第一次跑通：沒有履歷時報告略過履歷那幾節、不炸，log 與事故前的變更照出。
- **範圍**：
  - 做：`core/plugin.py`（基底類別＋registry）；`core/source.py`、`core/domain.py` 變成薄殼；實體 keys 三份合一；併件依據、換版偵測、報告段清單、額外子命令、預設排程改由 plugin 給；RCA 報告拆成段；`investigator/factory.py`、`detector/path_merge.py`、`cmd_paths.py` 搬進 `plugins/factory/`；`call_edges`、`deploy_at` 搬進 `plugins/observability/`；`configs/metrics_only/`；helm 改 `plugin:`；e2e 多 `--plugin metrics_only`。
  - 不做：`configs/baseline`、`rca`、`apm` 搬進 plugin（文件引用太多）；pip entry points 式的 plugin 發現（code 不是套件）；`trace-merge`、`ingest` 留主程式（通用）；兩套共用一個 ES 的索引前綴（另案）。
- **決定**：擴充點只開現在真的有兩種以上做法的地方（欄位對照、用語、實體 keys、併件依據、換版、報告段、子命令、排程），沒有的不預先抽。`ingest` 留主程式——它接的是「別人判好的異常」，哪個情境都用得到。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `core/plugin.py`（新） | `Plugin` 基底（`source_profile`、`domain`、`entity_keys`、`route_of`、`chain`、`change_at`、`severity_classes`、`critical_levels`、`sections`、`commands`、`schedule`）、`Chain`、registry（`BUILTIN`、`load`、`current`、`reset`；`AIOPS_PLUGIN` 不在內建就當 `套件.模組:類別`） | 擴充點只開現在真的有兩種以上做法的地方；`current()` 看到環境變數變了會自己換，測試不用到處 reset |
| `core/source.py`、`core/domain.py` | 兩個 dict 搬進 plugin，剩門面：`source.name()`／`profile()`、`domain.*` 都轉問 `plugin.current()` | 呼叫端（owner、es、feedback_web、llm、報告）一行不改 |
| `plugins/observability/`（新） | otel／apm 的 PROFILES、IT 的 DOMAIN、`ENTITY_KEYS`、`route_of`；`chain.py`（原 `cmd_alert.call_edges`）、`changes.py`（原 `cmd_alert.deploy_at`） | IT 的做法集中一處；`AIOPS_SOURCE` 降為這個 plugin 自己的選項 |
| `plugins/factory/`（新） | 原 `core/source.PROFILES["factory"]`、`domain.DOMAINS["factory"]`、參數家族（原在 rca.yaml）當預設；`chain.py`（原 `investigator/factory.py` 的 `lot_links`）、`changes.py`（`change_at`）、`paths.py`（原 `detector/path_merge.py`）、`cmd_paths.py`（原 `detector/cmd_paths.py`，多 `add_parser`） | 工廠專用的程式全在這裡，主程式沒有 `equipment`、`lot`、`recipe` |
| `plugins/metrics_only/`（新） | 繼承 factory：`chain` 回空、`sections` 只剩 anomalies／logs／changes／conclusion、沒有 `paths-merge` | 只有指標的情境是「工廠少了履歷」，繼承最誠實 |
| `investigator/report.py`（新） | 原 `investigate_full` 190 行拆成 `Ctx`＋8 個段函式（`SECTIONS`）；`investigate_full` 依 plugin 的 `sections` 跑；段的編號照出現順序；沒 trace 時 `entries` 只記一行**不再 return**，要 trace 的段各自寫「略過」，log／changes 照跑；`slowest` 空的不 `max()`；沒給 traces 索引時「平常」直接用一週前 | 第三種情境不用再加 if；順手修「沒 trace 連 log 都不看」「第 4 段空會炸」 |
| `investigator/__main__.py` | 剩 `investigate`、`topology`、`main`；`_svc_of`／`_route_of` 改讀 plugin（原本跟 `alerts.SVC_KEYS` 不一致） | 三份實體 keys 合一 |
| `investigator/alerts.py`、`detector/owner.py`、`investigator/es.py` | `SVC_KEYS` 改讀 `plugin.current().entity_keys`；`route_of` 轉問 plugin；`anomalies()` 的對象查詢用 `entity_keys` 組 | 同上 |
| `investigator/cmd_alert.py` | `run`：`chain_source` 的 if 換成 `plugin.chain(...)`；`grade_alert`：`deploy_kind` 的 if 換成 `plugin.change_at(...)`，`classes`／`critical_levels` 預設取 plugin；刪 `deploy_at`、`call_edges` | ⑥ 不再認得情境 |
| `detector/__main__.py` | `cmds` 加 `plugin.commands()`；`paths-merge` 從主程式移除 | 子命令跟著 plugin |
| `detector/cmd_feedback.py` | `_deploy_before` 改用 `plugin.change_at`、對象欄位用 `entity_keys` | 原本寫死 `cmd_alert.deploy_at` 與 `keys.service` |
| `detector/feedback.py` | 刪沒人用的 `ROOT_CAUSE_CATEGORIES` | 探索時發現沒有呼叫端 |
| `configs/factory/rca.yaml` | 刪 `chain_source`、`deploy_kind`、`deploy_*`、`critical_levels`、`severity.classes`（plugin 預設） | 設定檔只剩索引與時間尺度 |
| `configs/metrics_only/`（新） | `rca.yaml`（沒有 `indices.traces`、`chain: false`、`rca_retry: false`）、`schedule.yaml`（ingest、alert、prune） | 第三種情境 |
| `Dockerfile`、`helmchart` | `COPY plugins/`；values `plugin:`（刪 `domain`）、env `AIOPS_PLUGIN`、排程表依 plugin（observability 再看 `source`），不認得的名字 `fail` | 部署一個開關 |
| `tests/test_plugin.py`（新，11 個） | 見 Check | |
| `tests/factory_e2e.py` | `AIOPS_PLUGIN=factory`；`--plugin metrics_only` 模式（不跑 paths-merge、`configs/metrics_only/rca.yaml`、13 項檢查）；`_delete_index` 萬用字元先列再刪 | 「只有指標」第一次整條跑；ES 8 不讓用萬用字元刪索引，`--clean` 原本沒清掉履歷 |
| `tests/test_cmd_alert.py`、`test_factory_alerts.py`、`test_path_merge.py` | import 路徑改到 plugins；工廠的測試模組整個包在 `AIOPS_PLUGIN=factory` 下；Domain 測試改成遍歷 `plugin.BUILTIN` | 搬家 |
| 文件 | `plugins/README.md`（操作，新）、`architecture.md` §4.2、`docs/README.md`、`CLAUDE.md` 結構表、`semiconductor-design.md` 現況、`configs/factory/README.md`、`aiops-engine/README.md`；程式註解裡的舊路徑 | |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 沒弄壞任何一邊 | 既有 671 個單元測試不改邏輯（只改 import 路徑與 env）全跑 | 全過；加新的 11 個共 682 |
| 選 plugin：預設、env、客製、錯的 | `test_plugin.Registry`：沒設 env 是 observability／otel／「服務」；`AIOPS_PLUGIN` 切 factory／metrics_only／apm 立即生效；`mars`、不存在的模組、不是 Plugin 的類別都 `ValueError`；`tests.test_plugin:MyPlugin` 這種 dotted path 載得到，`entity_of`、`owner_of`、門面都跟著它 | 全過 |
| 三個內建各填對 | `test_plugin.Builtins`：observability 的 `route_of` 認 method＋route、APM 的 `route`、不認 recipe；factory／metrics_only 的 `entity_keys=("equipment",)`、recipe 優先、`fdc` 在 `critical_levels`、參數家族；只有 factory 有 `paths-merge`；factory 的 `chain`／`change_at` 從 cfg 拿索引（`paths_lookback` 真的進查詢） | 全過 |
| 子命令跟著 plugin | `detector --help` 在三個 plugin 下 | observability、metrics_only 沒有 `paths-merge`，factory 有 |
| 報告段跟著 plugin；沒 trace 不炸、log 照跑 | `test_plugin.ReportSections`（假 ES）：metrics_only 的報告是 1 Metrics、2 Log、3 事故前的變更、結論，`findings.requests` 是 None（不是 0：`cmd_alert` 不會為它重跑）；factory 在 `entry_stats` 回 0 時 2、3、4 段寫「（沒有履歷，略過）」，**第 5 段 Log、第 6 段變更照出**，`findings.requests.requests == 0`（會重跑）；不認得的段名 `ValueError` | 全過；原本沒 trace 在第 2 段就 return、第 4 段 `max()` 空會炸，都不會了 |
| 工廠整條不變 | `tests.factory_e2e`（測試 ES） | **15/15** 跟改之前一樣：ETCH-01 critical、METRO-01 併進、慢在 CH-B +31.9 分、共同點 CH-B 100%、ALID 樣式、事故前的 PM、CVD-01 換版 warning、匯出帶 run_id。參數家族、`critical_levels`、換版索引都已從 rca.yaml 刪掉，所以這次證明的是 plugin 的預設 |
| **只有指標整條第一次跑** | `tests.factory_e2e --plugin metrics_only`：同一批假資料、不跑 paths-merge、`configs/metrics_only/rca.yaml` | **13/13**：履歷 0 筆、32 筆異常全進、ETCH-01 一件 critical（18 項異常，少了 METRO-01 的 5 筆）、報告只有 Metrics／Log／事故前的變更／結論、ALID 新樣式與 PM 都在結論裡；**METRO-01 自己開一件 warning**（沒有履歷併不進前站，已知）；CVD-01 換版 warning、LITHO weak 不開件、CMP-01 單點自己結束、匯出照常 |
| chart 三種 plugin 都 render 對 | `helm template --set plugin=…`、`--set source=apm`、`--set plugin=mars` | `AIOPS_PLUGIN` 進 env；排程表 observability→`configs/schedule`、apm→`configs/apm`、factory→`configs/factory`、metrics_only→`configs/metrics_only`；`mars` render 失敗 |

- **沒測到的**：真實 ES 上的 IT 報告（段的編號改成照順序出，IT 沒設 `changes` 時 1～6 跟以前一樣，設了的話變更段會占一個號，`configs/rca/example-report.md` 沒重產）；客製 plugin 在 image 裡怎麼放（只用 `tests.test_plugin:MyPlugin` 驗 dotted path）；`feedback_web` 在 `metrics_only` 下的表單（跟 factory 同一張）。
- **發現的問題**：`factory_e2e --clean` 原本用萬用字元 DELETE `lot-paths-fab1-*`，ES 8 預設拒絕，履歷一直沒清掉（上一輪沒發現，因為每次都重合成蓋掉）→ 先 `_cat/indices` 列出再刪。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [⑥] 只有指標時，後站（量測）的異常併不進前站 | metrics_only e2e：METRO-01 自己開一件 | 一個事故兩件；量測異常的根因在前站，但報告只能看量測機自己的 log | 沒有履歷就沒有鍵；要併得先有 MES 歷程（哪怕只有 lot→機台的對照表）。設計上可以再開一個「lot 對照表」的 chain 實作 |
| [⑦] 報告段的編號照出現順序 | 拆段時決定 | IT 設了 `changes` 時變更段占一個號、topology 變 7；`configs/rca/example-report.md` 的編號對不上這個情況 | 下次在真實 ES 重產範例報告 |
| 客製 plugin 的排程表 | chart 只認三個內建名字 | 客製的要用 `configOverrides` 自己掛 | 寫在 plugins/README.md；需求出現再做 |
| `configs/metrics_only/ingest.yaml` 跟 factory 共用 | 寫 schedule 時 | 兩邊欄位名一樣才行 | 拿到真資料若不一樣就分一份 |
| 兩套共用一個 ES 的索引前綴 | 上一輪就有 | 同一個 ES 裝兩個 plugin 會混 | 另案（`AIOPS_INDEX_PREFIX`） |

- **決定**：`ingest`、`trace-merge` 留主程式（通用）；`configs/` 不隨 plugin 搬；不做 pip entry points（code 不是套件，dotted path 夠用）；`AIOPS_DOMAIN` 直接移除不留相容（只有工廠 e2e 和 chart 用過，都改了）。
- **下一輪**：拿到廠方樣本後對欄位；兩層共同點（先看出事站的 chamber，再看全路徑）；Grafana 回饋表單的選項跟著 plugin。
