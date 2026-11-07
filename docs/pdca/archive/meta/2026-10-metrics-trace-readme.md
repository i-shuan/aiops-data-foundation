# metrics、trace README 改成流程圖加四個重點（2026-10）

**狀態**：完成　　**日期**：2026-10-31　　**相關**：分支 `claude/metrics-trace-readme`（從 master `7e3f458` 開出）；照 [log README](2026-10-log-readme.md) 的做法
**模組**：①②③ 收值、清資料、存

## Plan：這一次要達到什麼

- **需求**：`docs/metrics/README.md`、`docs/trace/README.md` 的「資料怎麼走」是文字，讀不出流程；log 那份已改成流程圖加四個重點，metrics、trace 要同樣的結構。
- **目的**：兩份各有：流程圖、種類與來源（metrics 的來源與指標型別；trace 的 span 種類與取樣類別）、什麼時候與怎麼清洗、多久壓縮、共同鑰匙圈與自己獨立的欄位與 label。
- **範圍**：只改這兩份。被刪的段落（metrics 的能做到與做不到、.NET 與 Java 比較、部署與驗證、案例結論、現況；trace 的六個場景、跟 log 串的操作、現況）在 git 歷史。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `docs/metrics/README.md` | 流程圖（6 種來源 → Collector 清洗 → `metrics-{來源}.otel-{group}` → 兩種 ILM）；來源與指標型別兩張表；清洗步驟表；壓縮表（含 `metrics-generic` 不降解析度的原因）；共同鑰匙圈與 metrics 自己的欄位 | 對照 `telemetry/collectors/node.yaml`、`cluster.yaml`、`common.yaml`、`telemetry/storage/ilm-metrics-otel.json`、`ilm-metrics-hist-otel.json`、`metrics-otel-custom.json`、`retention.conf` |
| `docs/trace/README.md` | 流程圖（應用 → 節點 Collector → loadbalancing → trace-sampler → ES pipeline → 兩個資料流）；span 種類與五個取樣類別；清洗步驟表；壓縮表；共同鑰匙圈與 trace 自己的欄位 | 對照 `node.yaml`（`transform/traces-pre`、`transform/exceptions`）、`sampler.yaml`、`trace-sampler.yaml`、`service.yaml`、`pipeline-traces-otel.json`、`ilm-traces-otel.json`、`ilm-traces-problem-otel.json` |
| `docs/pdca/README.md` | 加一列 | 索引 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 沒有斷連結、索引完整 | `python3 tools/check-docs.py` | 四項都是 0 個問題 |
| 取樣類別、等待時間、保留天數與設定一致 | 對 `sampler.yaml` 的 policies 與順序、`trace-sampler.yaml` 的 `DECISION_WAIT` 30s、`RANDOM_PCT` 5、`SLOW_MS` 1000、`replicas` 2、`retention.conf` | 一致 |
| metrics 的壓縮階段與 ILM 一致 | 對 ES 上 `metrics-otel` policy（warm 14d 降 5m、cold 90d 降 1h、delete 400d）與 `metrics-hist-otel` | 一致 |

- **沒測到的**：來源與資料流的對應（kubelet → `kubeletstatsreceiver`、應用 → `generic`）是照 ES 上實際的資料流名稱與一份文件的 `scope.name` 推的，沒有逐個來源核對；兩個流程圖沒有在 GitHub 上看呈現。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 被刪掉的內容沒有搬到別處：metrics 的「能做到與做不到」、.NET 與 Java 比較、案例結論；trace 的六個場景對照 | 照「同理 log」只留四個重點 | 這幾塊只剩 git 歷史（`docs/metrics/README.md`、`docs/trace/README.md` 合併前的版本） | 需要時另開一份案例文件 |
| `cAdvisor` 歸 `prometheusreceiver` 是從設定推的 | 寫來源表時 | 資料流名稱可能有出入 | 在有資料的 ES 上查一次 `metrics-prometheusreceiver.otel-*` 有沒有 CPU 限流欄位 |
