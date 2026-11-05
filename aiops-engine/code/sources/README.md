# 資料來源：ES 以外的資料也能跑 train／detect／peer／rules

> **類型：操作 How-To**　為什麼這樣切見 [docs/pdca/meta/2026-11-model-modules.md](../../../docs/pdca/meta/2026-11-model-modules.md)

設定檔（`series.yaml`、`peer.yaml`、`rules.yaml`…）最上層加 `source:`、`store:`，指令和模型不用改。沒寫就是現在的 ES（`ESBackend`，跟 file／http／memory 一樣是 `Backend` 的實作；程式裡拿到 `core.es_client.ES` 時用 `sources.as_backend(es)` 包起來）。

| 情況 | 怎麼做 |
|---|---|
| 資料在 ES（現況） | 什麼都不加 |
| 資料是檔案（CSV、Parquet、NDJSON、JSON） | `source: {kind: file, format: events, files: {<index>: <路徑>}}`，結果放 `store: {kind: file, dir: .aiops-store}` |
| 資料在別的系統，有 HTTP API | `source: {kind: http, format: events, url: "https://…?index={index}&start={start}&end={end}"}` |
| 別的 Python 程式手上有 DataFrame | `be = sources.MemoryBackend({"<index>": df})`，傳給指令 `detector.baseline.train.cmd_train(cfg, args, backend=be)`，或自己 `be.series(spec, …)` 餵 `detector/baseline/model.py` 的 `fit_and_score` |
| 資料在 ES、結果不要寫 ES（離線實驗） | 只加 `store: {kind: file, dir: …}` |

## 檔案來源

```yaml
source:
  kind: file
  format: events                       # 一列一個事件（請求、log、一個時間點的量測值）
  files:
    metrics-apm.transaction.1m-*: data/tx.csv          # key 是序列寫的 index（可含 *）
    metrics-apm.service_destination.1m-*: data/dest.parquet
  spans: data/spans.ndjson             # 選填：M4 用的 span（sources/traces 的格式）
store:
  kind: file
  dir: .aiops-store                    # 每個 aiops-* index 一個 .ndjson；train 寫的 baseline 下次 detect 讀得到
```

```bash
cd aiops-engine/code && python3 -m detector train --config my-series.yaml --end 2026-11-02T00:00:00Z
```

```bash
cd aiops-engine/code && python3 -m detector detect --config my-series.yaml --at 2026-11-02T10:00:00Z --window 2h
```

- 欄位名用 ES 的點號路徑（`service.name`、`event.outcome`、`transaction.duration.us`）；JSON 的巢狀物件會自動攤平。
- 時間欄位預設 `@timestamp`（序列可用 `time_field` 改），ISO 字串或毫秒數。
- 直方圖欄位（APM 1 分鐘指標的 `transaction.duration.histogram`）一格 `{"values": […], "counts": […]}`，或攤成 `.values`、`.counts` 兩欄。
- `format: series`：列已經是標準輸入（`keys` 或 `by` 的欄位 + `ts` + `value` [+ `n`]），只做時間窗、改名、合併、補 0；同一窗多列時筆數類相加、其餘依 `n` 加權平均。
- 每個檔讀一次留在記憶體：幾十萬列可以，再大就先切時間或改用 ES。

## HTTP 來源

```yaml
source:
  kind: http
  format: events
  url: https://api.example.internal/rows?index={index}&start={start}&end={end}    # 回 JSON 陣列或 {"rows": […]}
  headers: {Authorization: Bearer …}
  timeout: 60
```

`{start}`、`{end}` 是 ISO 時間。也可以每個 index 一個網址：`urls: {metrics-apm.transaction.1m-*: https://…}`。每次查詢都打一次（detect 每分鐘跑）。

## `value` 的寫法與 ES 算得一不一樣

`events` 格式的 `value` 寫法跟 [series.yaml](../configs/baseline/reference.md) 一樣，在 pandas 算（[shape.py](shape.py)）。跟 ES 比過（同一批合成列，見 PDCA 的 Check）：

| value | 一樣？ |
|---|---|
| `count`、`ratio`、`sum`／`avg`／`max`／`min`、`div`、`percentile`（一列一個事件）、`hist_count`、`hist_ratio` | 完全一樣 |
| `percentile` 在直方圖欄位 | ES 是近似（TDigest），差約 2% 內 |
| `hist_ratio_above` | ES 在桶之間內插、這裡整格算一邊，差到 9%（門檻剛好落在某一格時） |

`filter` 支援 `term`、`terms`、`range`、`exists`、`prefix`、`wildcard`、`match`、`match_all`、`bool`；其他寫法會報錯說不支援。

## 做不到的

- `trace-merge`（讀 raw trace 合成一筆）、根因分析的 log 比對、`tune`／`adjust` 寫 `aiops-tuning`、回饋網頁：只有 ES。`store` 不是 ES 時 `tune` 會拒絕，請直接改 `series.yaml` 的 `overrides`。
- 檔案、記憶體來源沒有 dept／group 的資料可查：異常全部 `unassigned`。
