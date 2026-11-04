# engine 改成「主程式＋plugin」：可觀測性、全套工廠、只有指標各一個 plugin（2026-11）

**狀態**：進行中　　**日期**：2026-11-04　　**相關**：分支 `claude/plugin-architecture`（接在 `claude/factory-engine` 後）；操作 [plugins/README.md](../../../aiops-engine/code/plugins/README.md)；原理 [architecture.md §4.2](../../architecture.md)
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

（動手後補）

## Check：測試的目的與結果

（動手後補）

## Act：Check 後沒改善的、還沒解決的

（動手後補）
