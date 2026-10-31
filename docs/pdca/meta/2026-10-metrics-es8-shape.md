# metrics 測試與文件對齊 ES 8.19 的直方圖存法（2026-10）

**狀態**：已完成　　**日期**：2026-10-31　　**相關**：分支 `claude/metrics-es8-expectations`；前一輪 [2026-10-pin-es8-grafana13.md](2026-10-pin-es8-grafana13.md)
**模組**：③ 存、④ 判斷

## Plan：這一次要達到什麼

- **需求**：master（1ba634d）在 k3d、ES 8.19.23 上重跑 `run-metrics-e2e.sh`，44 項有 2 項失敗：直方圖型別是 `histogram` 不是 `exponential_histogram`、文件裡沒有 `temporality`。「每筆 ≤ 140 個請求」那項讀 `positive.counts`，8.19 上讀不到值、沒驗到東西卻通過。
- **目的**：測試照 ES 8.19 的實際存法驗「每筆是 30 秒的差值」；文件寫的型別、欄位跟 8.19 一致；engine 讀直方圖次數的地方在 8.19 上算得出來。
- **範圍**：`run-metrics-e2e.sh` 的直方圖檢查、`docs/metrics/reference.md`、`docs/metrics/catalog.md`、`engine/detector/rules.py` 的 `hist_count`。不改 Collector 設定；ES 9 專用的合成資料測試（`otlp_synth.py`、`rules_edge_e2e.py`）不在這次。

## Do：改動哪些 code、改了什麼、為何要改

ES 8.19.23 上實際存的直方圖（`metrics-generic.otel-imc`，Collector `elasticsearch` 寫入元件 otel mode）：mapping 型別 `histogram`，`_source` 是 `{"values": [0.0375, 0.0625, 0.375], "counts": [48, 60, 12]}`，沒有 `positive`、`zero`、`sum`、`temporality`；`attributes` 是扁平的 key（`"http.response.status_code": 200`）。

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/local-k3s/run-metrics-e2e.sh` | 型別預期改 `histogram`；拿掉 `temporality` 檢查；每筆請求數改用 `counts` 加總，並加一項「讀得到請求數（> 0）」 | 8.19 不存 `temporality`，差值只能從每筆的量看。原本的 ≤ 140 檢查讀 `positive.counts`、又用 `.attributes.http.response.status_code` 取扁平 key，兩處都讀不到值，結果是空陣列也通過；加 > 0 那項才擋得住這種空跑 |
| `engine/detector/rules.py` `hist_count` | 加總頂層的 `counts`（8.19），保留 `positive`／`negative`／`zero`（ES 9） | 規則 `hist_count: true`（JVM major GC 次數）在 8.19 上原本永遠算出 0，不會觸發 |
| `engine/tests/test_rules.py` | 加 `test_hist_count_es8_histogram`，用實測那筆的 values／counts | 確認 8.19 的形狀算得出 120 |
| `docs/metrics/reference.md`、`docs/metrics/catalog.md` | 型別寫 `histogram`（`values`＋`counts`、不存 sum）；拿掉 `temporality` | 原本寫的是 ES 9.5 的存法 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 測試的預期跟 8.19 實際存法一致，且「每筆是 30 秒的量」真的有驗到 | k3d（k3s v1.30.14）＋ cluster 內 ES 8.19.23，`ES_URL=http://localhost:9210 KCTX=k3d-aiops run-metrics-e2e.sh` | 第一次跑新加的「讀得到請求數」得到 0，才發現 attributes 是扁平 key；改用 `.attributes["http.response.status_code"]` 後 44 項全過，成功請求每筆最多 120 個（測試應用每 30 秒 120 個） |
| `hist_count` 在兩種形狀都算對 | `python -m pytest -q`（docker `python:3.12-slim`，Windows 本機沒裝 pandas） | 346 passed、5 skipped；新測試 8.19 形狀算出 120，原本 ES 9 形狀的 `test_major_gc_count` 照樣是 10 |
| 錯誤率、p95 不受影響 | 同一次 e2e（用 ES 聚合 `value_count`／`percentiles`，不讀 `_source`） | 錯誤率在 4%～5.6%、p95 在 0.25～0.5 秒，通過 |

- **沒測到的**：沒用累加值（關掉 `cumulativetodelta`）確認 ≤ 140 那項會失敗；`hist_count` 規則沒在 cluster 上用真的 JVM GC 指標跑過。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [④] 規則「JVM 花太多時間在 GC」讀 `metrics.jvm_gc_duration_seconds.sum`，8.19 的 histogram 不存 sum | 查 8.19 的 `_source` 與 `_field_caps`（沒有任何 `*.sum` 直方圖欄位）時推論；沒用真的 JVM 驗 | 推論：這條規則在 8.19 上永遠不會觸發 | 要決定改用 values×counts 估（有誤差）、或讓 Collector 另寫一個 sum 欄位 |
| [④] `engine/tests/otlp_synth.py`、`rules_edge_e2e.py` 合成的是 `exponential_histogram`（ES 9 的形狀，走 `/_otlp`） | grep `exponential_histogram` 時看到 | 這兩個合成資料測試在 8.19 上的形狀跟正式資料不一樣 | 改成合成 8.19 的 `histogram` |
| `docs/retention-how-to.md`、`docs/troubleshooting.md` 還有 `exponential_histogram` 的實測紀錄 | 同上 | 那是 ES 9.5 上量的；降解析度在 8.19 上沒重測 | 8.19 上重跑降解析度再改 |
