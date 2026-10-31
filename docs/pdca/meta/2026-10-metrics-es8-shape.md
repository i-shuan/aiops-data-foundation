# metrics 測試與文件對齊 ES 8.19 的直方圖存法（2026-10）

**狀態**：已完成　　**日期**：2026-10-31　　**相關**：分支 `claude/metrics-es8-expectations`；前一輪 [2026-10-pin-es8-grafana13.md](2026-10-pin-es8-grafana13.md)
**模組**：③ 存、④ 判斷

## Plan：這一次要達到什麼

- **需求**：master（1ba634d）在 k3d、ES 8.19.23 上重跑 `run-metrics-e2e.sh`，44 項有 2 項失敗：直方圖型別是 `histogram` 不是 `exponential_histogram`、文件裡沒有 `temporality`。「每筆 ≤ 140 個請求」那項讀 `positive.counts`，8.19 上讀不到值、沒驗到東西卻通過。
- **目的**：測試照 ES 8.19 的實際存法驗「每筆是 30 秒的差值」；文件寫的型別、欄位跟 8.19 一致；engine 讀直方圖次數的地方在 8.19 上算得出來。
- **範圍**：`run-metrics-e2e.sh` 的直方圖檢查、`docs/metrics/reference.md`、`docs/metrics/catalog.md`、`engine/detector/rules.py` 的 `hist_count`。第二輪（同一分支）：GC 時間規則讀不到 sum 的問題、合成資料測試改成 8.19 的形狀。

## Do：改動哪些 code、改了什麼、為何要改

ES 8.19.23 上實際存的直方圖（`metrics-generic.otel-imc`，Collector `elasticsearch` 寫入元件 otel mode）：mapping 型別 `histogram`，`_source` 是 `{"values": [0.0375, 0.0625, 0.375], "counts": [48, 60, 12]}`，沒有 `positive`、`zero`、`sum`、`temporality`；`attributes` 是扁平的 key（`"http.response.status_code": 200`）。

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/local-k3s/run-metrics-e2e.sh` | 型別預期改 `histogram`；拿掉 `temporality` 檢查；每筆請求數改用 `counts` 加總，並加一項「讀得到請求數（> 0）」 | 8.19 不存 `temporality`，差值只能從每筆的量看。原本的 ≤ 140 檢查讀 `positive.counts`、又用 `.attributes.http.response.status_code` 取扁平 key，兩處都讀不到值，結果是空陣列也通過；加 > 0 那項才擋得住這種空跑 |
| `engine/detector/rules.py` `hist_count` | 加總頂層的 `counts`（8.19），保留 `positive`／`negative`／`zero`（ES 9） | 規則 `hist_count: true`（JVM major GC 次數）在 8.19 上原本永遠算出 0，不會觸發 |
| `engine/tests/test_rules.py` | 加 `test_hist_count_es8_histogram`，用實測那筆的 values／counts | 確認 8.19 的形狀算得出 120 |
| `docs/metrics/reference.md`、`docs/metrics/catalog.md` | 型別寫 `histogram`（`values`＋`counts`、不存 sum）；拿掉 `temporality`；補 `_sum` 欄位 | 原本寫的是 ES 9.5 的存法 |
| `telemetry/collectors/node.yaml` | 加 `transform/hist-sum`（OTTL `extract_sum_metric(true)`，只對直方圖），放在兩條 metrics pipeline 的 `cumulativetodelta` 之後 | 8.19 的 `histogram` 欄位只存 values、counts，直方圖的 sum 寫進去就沒了；抽成 `<名稱>_sum` 另存。放在換差值之後，抽出來的才是這 30 秒的量。實測寫在同一份文件、型別 `double`／gauge |
| `engine/configs/baseline/rules.yaml` `jvm.gc_overhead` | `field` 改 `metrics.jvm_gc_duration_seconds_sum`，拿掉 `exists` | 原本讀 `metrics.jvm_gc_duration_seconds.sum`，8.19 沒有這個子欄位，規則永遠不觸發；新欄位是數值，`exists` 直接用它就行 |
| `engine/tests/rules_edge_e2e.py` | 直方圖改成 `histogram`（`values`＋`counts`）；GC 案例同時寫 `jvm_gc_duration_seconds_sum`；mapping 不帶 `time_series_metric` | 原本合成 ES 9 的 `exponential_histogram`，8.19 的 metrics template 沒有這個 dynamic template、寫不進去 |
| `engine/tests/otlp_synth.py` | `exp_histogram` → `es_histogram`（bucket 中點、0 的格不寫）；`metric_doc` 照 8.19 的 `_source`（沒有 `unit`、`temporality`，`scope` 只有 name，加 `_sum`）；bulk 的 dynamic template 改 `histogram`＋`gauge_double`；刪 `check_bulk_matches_otlp` | 比對的對象（`/_otlp` 入口）8.19 沒有，而且現在 bulk 寫的形狀跟 ES 9 的入口本來就不一樣，那個比對已經沒有意義；形狀改成照 k3d 上實際那筆 |
| `engine/tests/baseline_edge_e2e.py` | 不再呼叫 `check_bulk_matches_otlp`，`names_hash` 用常數；docstring 註明 log 那段只有 ES 9 能跑 | 同上 |
| `docs/metrics/cases.md` | GC 時間比例改寫成讀 `_sum` | 跟規則一致 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 測試的預期跟 8.19 實際存法一致，且「每筆是 30 秒的量」真的有驗到 | k3d（k3s v1.30.14）＋ cluster 內 ES 8.19.23，`ES_URL=http://localhost:9210 KCTX=k3d-aiops run-metrics-e2e.sh` | 第一次跑新加的「讀得到請求數」得到 0，才發現 attributes 是扁平 key；改用 `.attributes["http.response.status_code"]` 後 44 項全過，成功請求每筆最多 120 個（測試應用每 30 秒 120 個） |
| `hist_count` 在兩種形狀都算對 | `python -m pytest -q`（docker `python:3.12-slim`，Windows 本機沒裝 pandas） | 346 passed、5 skipped；新測試 8.19 形狀算出 120，原本 ES 9 形狀的 `test_major_gc_count` 照樣是 10 |
| 錯誤率、p95 不受影響 | 同一次 e2e（用 ES 聚合 `value_count`／`percentiles`，不讀 `_source`） | 錯誤率在 4%～5.6%、p95 在 0.25～0.5 秒，通過 |
| Collector 抽出來的 `_sum` 真的寫進 8.19、跟直方圖同一份文件、是差值 | k3d 上 `kubectl apply -k e2e/local-k3s/otel-collector`、重啟 node Collector 後查 `metrics-generic.otel-imc` 最新文件與 mapping；e2e 加一項「`_sum` 跟直方圖同一份文件」 | 同一份 `_source` 裡 `http.server.request.duration_sum: 12.0`（120 個請求）、下一筆 9.0（6 個 1.75 秒的）；mapping `double`、`time_series_metric: gauge`；Collector 沒有錯誤 log；e2e 45 項全過 |
| 39 條靜態臨界值規則（含 GC 次數、GC 時間比例）在 8.19 的形狀上判斷正確 | `python -m tests.rules_edge_e2e` 對 k3d 的 ES 8.19.23（docker `python:3.12-slim`，`ES_URL=http://host.docker.internal:9210`） | 規則 156／156、interval 4／4、動態調整 10／10 全過；`jvm.gc_overhead` 「每 30 秒 GC 3 秒＝10%」觸發、9.7% 不觸發 |
| 沒弄壞別的 | `pytest`；`ruff check` 三個改過的測試檔跟 HEAD 比 | 346 passed、5 skipped；ruff 錯誤數 HEAD 4／12／15 → 3／12／15（沒新增） |
| 主線 8 步（回饋 → 建議 → 回測 → 核准 → 再跑不報）在 8.19 能走完（2026-10-31 重跑） | `loop_e2e`（`LLM_PROVIDER=template`，對 k3d 旁的 ES 8.19.23） | 42 項過、1 項沒過：「metrics 印出件數、誤報率」，是寫死日期範圍的檢查，之前就是這一項，跟 8.19 無關 |
| 合成資料改成 `_bulk` 後 `baseline_edge_e2e` 在 8.19 跑得完（Act 第一項） | `python -m tests.baseline_edge_e2e`（metrics 47.6 萬點、log 4.2 萬筆，log 走 `logs-generic.otel-it` → `k8s-logs-otel` → `k8s-it-bedge`） | 先後遇到兩個問題：測試引用的元件模板 `otel@settings` 8.19 沒有（400）；`(ni + k)` 是 numpy 整數，轉 JSON 失敗。兩處修好後跑完：67 過、12 沒過，沒過的都是偵測品質（一週 anomaly 率 0.594%＞0.1%、warning 2.151%＞1%、流量掉到一成沒抓到、緩慢漂移報太多等），跟當初 ES 9.5.3 上「誤報率都沒過」同一類，沒有放寬預期 |
| Collector 的 `_sum` 抽取在這個分支的設定下仍正常，且新的 ILM `metrics-hist-otel` 套得進 8.19 | `SKIP_KEYS=1 elasticsearch-setup.sh` 後，`OTEL_KUSTOMIZE=e2e/local-k3d/.run run-metrics-e2e.sh`（k3d） | setup 成功，rollover 換到 `metrics-hist-otel`；metrics e2e 46 項全過 |

- **沒測到的**：沒用累加值（關掉 `cumulativetodelta`）確認 ≤ 140 那項會失敗；GC 規則沒在 cluster 上用真的 JVM 指標跑過（k3d 上沒有 Java 應用，`_sum` 是用模擬 Java agent 的 `http.server.request.duration` 驗的）；`baseline_edge_e2e.py` 沒跑（見 Act）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [④] `baseline_edge_e2e.py` 在 8.19 上的 12 項沒過（偵測品質：誤報率、流量掉一成沒抓到、緩慢漂移等） | 2026-10-31 在 8.19 重跑（log 已改 `_bulk`，能跑完） | 這是動態基準線學習本身的限制，與 8.19 無關，但還沒逐項比對和 ES 9.5.3 時的清單是否完全一樣 | 逐項對照 baseline-verify 的結果，確認 12 項都在已知清單內 |
| `docs/retention-how-to.md`、`docs/troubleshooting.md` 還有 `exponential_histogram` 的實測紀錄 | grep `exponential_histogram` 時看到 | 那是 ES 9.5 上量的；降解析度在 8.19 上沒重測 | 8.19 上重跑降解析度再改 |
| 每個直方圖多一個 `_sum` 欄位（`http.server.request.duration_sum` 也會有） | 設計時決定對所有直方圖抽，不只 GC | 多一個 double 欄位；平均延遲可以直接算（sum ÷ counts） | 觀察 TSDS 大小，沒問題就留著 |

- **決定**：sum 由 Collector 抽成另一個欄位，不在規則端用 values×counts 估（中點估的誤差不可控，而且 Collector 本來就有 sum）。

- **這次順手改的**：`engine/tests/baseline_edge_e2e.py`（拿掉 8.19 沒有的 `otel@settings`、numpy 整數轉一般整數）；`e2e/local-k3s/run-metrics-e2e.sh` 加環境變數 `OTEL_KUSTOMIZE`，k3d 上 ES 在主機 docker 時可以指到 `e2e/local-k3d/.run`。
- **還沒做**：盤點第 6 項其餘的 e2e（keys、dotnet、schedule、trace-baseline、es-2node、kafka、tail-sampling）沒有在 8.19 重跑；降解析度的新設定只確認套得進 8.19，沒有拿真資料跑到降解析度那一步。
