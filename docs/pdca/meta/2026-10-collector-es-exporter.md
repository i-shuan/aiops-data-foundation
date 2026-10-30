# Collector 出口改用 elasticsearch 寫入元件：ES 停機、沒回應會不會掉資料（2026-10）

**狀態**：進行中　　**日期**：2026-10-30　　**相關**：分支 `claude/collector-es-exporter`；前置 [compat-es8-grafana13](2026-10-compat-es8-grafana13.md)、[durability](../phase1-collect/2026-10-durability.md)
**模組**：① 收值（Collector 出口）、③ 存

## Plan：這一次要達到什麼

- **需求**：公司的 ES 是 8.19，沒有 `/_otlp/v1/*` 入口；Collector 三條出口（node、cluster、trace-sampler）都走它，8.19 上一筆都寫不進去。
- **目的**：出口改成 `elasticsearch` 寫入元件（`mapping.mode: otel`），並證明當初不用它的理由已經不成立：ES **停機**（連線被拒）與 **沒回應**（連線掛住到逾時）兩種情況，停機期間的 log、metrics、trace 恢復後會補送，不會掉。
- **範圍**：`telemetry/collectors/common.yaml`、`sampler.yaml`（出口定義）、`node.yaml`、`cluster.yaml`（引用名）。佇列、重試、檔案儲存的設定不動。不改中央 pipeline、不改 engine。
- **為什麼當初不用它**：`common.yaml` 舊註解——「把連線逾時、沒拿到 HTTP 回應當永久錯誤，不重試；實測 ES 停 3 分鐘 metrics 幾乎全掉」。這次用 0.162.0，`retry_on_failure` 開、`max_elapsed_time: 0`，要重測同樣情境。

## Do：改了什麼

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `common.yaml` | `otlp_http/es` → `elasticsearch/es`：`endpoint`、`mapping: {mode: otel}`、`auth` 同一個 `bearertokenauth/es`（scheme ApiKey）；加 `retry: {enabled, max_retries: 3}`（bulk 裡單筆被退的重試）；`sending_queue`、`retry_on_failure` 原樣 | 8.19 沒有 `_otlp` |
| `sampler.yaml` | 同上 | trace-sampler 自己有出口 |
| `node.yaml`、`cluster.yaml` | pipelines 的 `exporters` 改名 | |

## Check：怎麼驗

k3d（單節點）＋主機 docker 的 ES 8.19.23；Collector 用正式設定（k3d overlay 只改 ES 位址與取樣）。產生器：log 每秒 200 行帶序號（`e2e/local-k3s/load/generator.yaml`）、trace 每秒 20 個帶序號（`trace-gen.py`）、metrics 是 kubeletstats 每 30 秒一點。

| 情境 | 怎麼做 | 預期 |
|---|---|---|
| 正常 | 跑 2 分鐘 | log、trace 0 遺失 0 重複；metrics 無空檔 |
| ES 停機 180 秒 | `docker stop es8`（連線被拒） | 停機期間的資料留在節點佇列，ES 起來後補送：0 遺失；重複 0 |
| ES 沒回應 90 秒 | `docker pause es8`（連線掛住，30 秒逾時） | 同上；這是舊註解說會全掉的情境 |
| ES 退單筆（429） | 無法在 docker 單節點穩定製造；改看 `retry` 設定存在 | 記為未測 |
