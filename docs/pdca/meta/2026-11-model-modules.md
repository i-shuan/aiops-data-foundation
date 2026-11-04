# 五個模型拆成模組：資料來源介面＋萬用資料處理器（2026-11）

**狀態**：已完成　　**日期**：2026-11-03　　**相關**：分支 `claude/model-modules`
**模組**：④⑥⑦

## Plan：這一次要達到什麼

- **需求**：五個模型（動態基準線學習、Pod 同儕橫向比對、靜態臨界值規則、trace 分段、相似事故檢索）的演算法本身已經不碰 ES（`detector/model.py`、`peer.py` 等輸入輸出都是 DataFrame），但 `train`、`detect`、`peer`、`rules`、`trace-*`、`alert` 這些指令從查資料、整形到寫結果全部寫死在 ES 上。資料不在 ES（檔案、別的資料庫或 API、另一支程式手上的 DataFrame）就一行都跑不了；回測、離線實驗、別人想拿模型去用，都得先把資料塞進 ES。
- **目的**：
  1. 一個**資料來源介面**（`Source`）：設定檔裡宣告資料從哪來——ES、檔案（CSV／Parquet／NDJSON）、HTTP API、或程式直接給 DataFrame——指令和模型不用改。
  2. 一個**萬用資料處理器**（`shape`）：把任何來源的原始列，依序列定義（`by`、`filter`、`value`、`interval`、`scale`、`fill_zero`）整成五個模型各自的標準輸入；ES 的 aggregation 是同一套定義在 ES 端的實作，兩邊算出來要一樣。
  3. 五個模型各有**明確的輸入規格**（欄位、單位、一列代表什麼），放在 `models/`，別的程式 `import` 進來自己準備 DataFrame 就能呼叫。
  4. 現有的 ES 路徑行為不變：562 個單元測試不動也要過；`AIOPS_SOURCE`、`configs/apm/`、排程表照舊。
- **範圍**：
  - 做：`Source` 介面與四種實作（`es`、`file`、`http`、`memory`）；`shape` 處理器（series、peer、points 三種整形）；`models/` 五個模型的輸入規格與 facade；`train`／`detect`／`peer`／`rules` 改走 `Source`；baseline 與異常的讀寫走 `Store`（`es`、`memory`、`file`），讓檔案來源不用 ES 也能 train → detect；trace 分段與相似事故提供「給 DataFrame／list 就能算」的模組入口與 `memory` 來源。
  - 不做：trace-merge（讀 raw trace 合成一筆，天生是 ES 上的工作）、根因分析的 log 比對（`investigator/es.py`）、LLM、feedback 網頁不改來源；`aiops-tuning` 的調整只在 `Store` 是 ES 時讀，其他 Store 回空；不改 chart、不改排程表；不重新命名 `detector/`、`investigator/` 套件（舊 import 路徑全部保留）。

### 五個模型與它們的標準輸入

| # | 模型 | 演算法在哪（不動） | 標準輸入（一列是什麼） |
|---|---|---|---|
| M1 | 動態基準線學習 (Dynamic Baseline Learning) | `detector/model.py`：`pick_resolution`、`train`、`score`、`score_poisson`、`mark_status` | `series`：`keys… + ts + value + n`，一列＝一條序列在一個時間窗的值；`n` 是底下的事件數（請求數） |
| M2 | Pod 同儕橫向比對 (Peer Comparison) | `detector/peer.py`：`compare` | `peer`：`group… + member + value (+ volume)`，一列＝一個 Pod 在這個 window 的一個值 |
| M3 | 靜態臨界值規則 (Static Threshold Rules) | `detector/rules.py`：`evaluate`、`evaluate_window` | `points`：`by… + ts + <欄位>…`，一列＝一個對象一個時間點的原始值（value／compare／ratio／increase／slope 型）；window 型用 `series` |
| M4 | trace 分段（慢在哪一段、錯從哪開始） | `detector/trace.py`：`summarize`、`new_segments`；`investigator/analyze.py`：`compare_segments` | `spans`：`core/traces.py` 的 span 格式（`trace_id, span_id, parent, service, name, kind, target, start_ms, dur_ms, error, …`），一列＝一段 |
| M5 | 相似事故檢索 | `investigator/similar.py`：`fingerprint`、`explain` | `incidents`：候選事故（含 `fingerprint`、`status`、根因欄位）＋新事故的 `alert`、`findings` |

### 介面怎麼切

```
設定檔 source: {kind: es}                ─┐
       source: {kind: file, path: …}     ─┤
       source: {kind: http, url: …}      ─┼─► Source ──► 標準輸入 ──► models/ ──► 結果 DataFrame ──► Store（es／memory／file）
程式：Source.memory(frames={…})          ─┘      │
                                                 └─ es：aggregation 在 ES 端算（現在的 detector/es.py）
                                                    file／http／memory：原始列交給 shape（pandas）算同一套
```

- `Source` 的方法對應五種標準輸入：`series(spec, start, end, interval)`、`peer(spec, start, end)`、`points(rule, start, end)`、`spans(trace_ids, start, end)`、`incidents(query)`。ES 的實作就是現在的 `detector/es.fetch`、`rules.fetch`、`core/traces.spans_of`、`similar.query` 搬過去。
- `shape` 只做一件事：原始列（每列一個事件或一個預先統計好的點）＋序列定義 → 標準輸入。支援的 `value` 寫法跟 `detector/es._value_aggs` 一樣（`count`、`sum/avg/max/min`、`percentile`、`ratio`、`div`）；直方圖類（`hist_*`）原始列不會有，檔案來源要先算好（列裡直接給 `value`、`n`）。
- 原始列的格式由來源的 `format` 決定：`events`（一列一個事件，shape 聚合）或 `series`（已經是標準輸入，只做改名、換單位、補 0）。
- `Store`：`baseline`、`anomalies`、`tuning` 三個索引的讀寫。`es` 是現在的；`memory` 給程式呼叫與測試；`file` 是一個資料夾裡的 NDJSON，給離線跑。

### 怎麼驗（Check 要回答的）

| 要確認的 | 怎麼測 |
|---|---|
| ES 路徑沒變 | 現有 562 個測試不改、全過；`tests/synth_e2e.py` 在測試 ES 上跑一次，異常數跟改前一樣 |
| shape 跟 ES 算出來一樣 | 同一批合成的 APM 原始列：一份寫進測試 ES 用 `es` 來源查，一份用 `memory` 來源走 shape；`http.requests`、`http.error_rate`、`http.latency_p95_s`、`dep.latency_avg_s` 四條序列的 value、n 逐窗比，差異 < 1e-6（百分位允許 ES 的 TDigest 誤差，比相對誤差） |
| 不用 ES 也能 train → detect | CSV（events 格式）→ `train --source file` → `detect` → `memory` Store 裡有 baseline、異常；異常的時間窗跟注入的一致 |
| 五個模型 import 就能用 | 每個模型一個單元測試：只準備標準輸入的 DataFrame／list 呼叫 `models.<名>.run`，不 mock ES |
| 設定錯誤看得懂 | 來源的 `format`、欄位缺了、`value` 寫法不支援，錯誤訊息要說是哪條序列、缺什麼 |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `sources/base.py`（新） | `Backend`（Source 五個方法＋轉交 Store）、`Store`（baseline、異常、tuning、incidents 的讀寫）兩個抽象類別，`is_backend()` | 指令只拿到一個物件；ES 本身**不是** Backend，沒寫 `source` 時指令拿到的還是 `core.es.ES`，走原路 |
| `sources/shape.py`（新） | 萬用處理器：`mask`（ES query DSL 子集 → 列的遮罩）、`series_from_rows`（events／series 兩種格式 → `keys + ts + value + n`，含直方圖欄位、補 0）、`points_from_rows`（→ 規則的 points，含 join）；`fill_zero` 從 `detector/es.py` 搬來 | ES 端 aggregation 的 pandas 版，語意照 `_value_aggs`／`_bucket_value`／`_bucket_events` 一比一寫；補 0 兩邊共用一份 |
| `sources/stores.py`（新） | `MemoryStore`、`FileStore`（每個 index 一個 NDJSON）、`ESStore`（source 不是 ES、結果要進 ES 時用）、`open_store` | 檔案來源不用 ES 也能 train → detect（baseline 要留到下一次行程） |
| `sources/rows.py`（新） | `RowsBackend`（原始列 → shape）、`MemoryBackend`（程式給 DataFrame）、`FileBackend`（CSV／Parquet／NDJSON／JSON）、`HttpBackend`（GET 回 JSON）、`ESSourceBackend`（讀 ES、寫別處） | 四種來源只負責「拿原始列」，整形都在 shape |
| `sources/__init__.py`（新） | `open_backend(cfg, es_factory)`：依 `source`／`store` 給 ES 或 Backend | `es_factory` 傳指令模組自己的 `ES`，測試蓋 `C.ES` 照樣有效；只在真的要連 ES 時才建連線 |
| `detector/es.py`、`rules.py`、`owner.py`、`adjust.py`、`core/traces.py`、`investigator/similar.py` | `fetch`、`alive_times`、`load_baseline`、`load_group_baselines`、`rules.fetch`、`write_anomalies`、`adjust.load`、`spans_of`、`spans_by_id`、`similar.search` 開頭多一句：是 Backend 就交給它 | 指令、backtest、既有測試的假 ES 一行都不用改；`similar.search` 把打分拆成 `rank()`（M5 的純函式） |
| `detector/cmd_baseline.py`、`cmd_peer.py`、`cmd_rules.py` | `ES()` → `backend or open_backend(cfg, ES)`（`cmd_*` 多一個 `backend=` 參數，程式直接給 `MemoryBackend` 用）；`save_baseline`、`_keep_upgraded` 是 Backend 時交給 Store；`tune` 在 store 不是 ES 時拒絕 | 入口只改這一處 |
| `models/`（新：`baseline`、`peer`、`rules`、`trace`、`similar`、`README.md`） | 五個模型的門面：docstring 寫輸入格式、`check_*()` 檢查欄位、re-export 演算法；`baseline.fit_and_score`、`peer.compare_checked`、`rules.points_of`、`trace.check_spans`、`similar.check_candidates` | 別的程式 `import models` 自己準備 DataFrame 就能叫；演算法沒搬 |
| `Dockerfile` | 多 `COPY sources/`、`COPY models/` | `detector/es.py` 現在 import `sources`，image 少了這兩個資料夾起不來（測試不會抓到，靠這一行） |
| `tests/test_shape.py`、`test_sources_backend.py`、`test_models.py`（新，31 個） | 見 Check | |
| `sources/README.md`（操作）、`models/README.md`（規格）、`aiops-engine/README.md`、`docs/README.md`、`CLAUDE.md` | 怎麼寫 `source`／`store`、五個模型的輸入規格、索引 | |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| ES 路徑沒變 | 既有 562 個測試不改 | 全過（含假 ES 蓋 `C.ES` 的 `test_detect_status`、`test_tune_show`、`test_peer` 蓋 `fetch`、`test_backtest`） |
| shape 跟 ES 算出來一樣 | 12,669 筆合成請求列（3 條序列 × 3 小時）＋ 988 筆 APM 式 1 分鐘直方圖文件，寫進測試 ES；10 種 `value` 各用 `detector.es.fetch`（ES）與 `MemoryBackend.series`（shape）算，逐窗比 value、n | `count`、`ratio`、`percentile`（事件列）、`avg`、`div`、`hist_count`、`hist_ratio`、整段一個值：**完全一樣**（差 0，窗數、n 都一樣）。直方圖欄位的 `percentile`：最大相對差 2.2%（ES 用 TDigest 近似）；`hist_ratio_above`：最大絕對差 0.0034、相對差 9.1%（ES 在桶之間內插，這裡整格算一邊） |
| 不用 ES 也能 train → detect | `test_sources_backend`：15 天、每小時 60 個請求的合成列，第 14 天 10 點錯誤率 2% → 60%；`MemoryBackend` 一個行程內 train → detect；`FileBackend`（CSV）＋ `FileStore` 分兩次 `open_backend`（模擬兩個行程） | 兩種都只報那一小時的 `http.error_rate`，status `anomaly`，detect-status 的 `missing_baseline` 0；第二次行程從 `.ndjson` 讀回 baseline、沒印「沒有 baseline」 |
| rules、peer、相似事故也走 Backend | 同上檔：`increase` 型規則（p1 重啟 1→3、p2 不變）、peer（4 個 Pod 一個 CPU 1.5）、`similar.search` 給 Backend 的 incidents（confirmed／proposed 各一） | 只報 p1、只報那個 Pod、只回 confirmed 那件並寫出「同一段變慢」 |
| 五個模型 import 就能用 | `test_models`：每個模型只給 DataFrame／list | M1 抓到注入的 0.5；M2 流量 5 的 Pod 不比；M3 增加 1 觸發；M4 慢的那段是 oracle、多 280 ms；M5 只排 confirmed |
| 設定錯誤看得懂 | `test_shape`：`hist_count` 用在事件列、`by` 欄位不存在、不支援的 query | 訊息有序列名、缺的欄位、「改用 count、ratio、percentile」 |
| 文件 | `tools/check-docs.py` | 連結、索引、舊說法、類型 0 個問題 |

- 過程中**發現的問題**：`mask` 的 `exists` 寫法是 `{"exists": {"field": …}}`，一開始照 `{欄位: 值}` 拆、永遠 False；不支援的 query 一開始沒先擋、當成「欄位不存在」回全 False——都是測試抓到的，已修。
- **沒測到的**：HTTP 來源只用假的 fetch 測（沒有真的 API）；`ESSourceBackend`（讀 ES、寫檔案）只測到建得起來；`store: es` 搭檔案來源沒跑；`tests/synth_e2e.py` 沒在測試 ES 上重跑（ES 路徑的程式碼只多了 `is_backend` 判斷，靠 562 個測試）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `hist_ratio_above` 在直方圖上跟 ES 差到 9% | 比對時 | 「超過 2 秒的請求比例」檔案來源算出來比 ES 略不同，門檻剛好落在某格時明顯 | 要一樣就照 ES 的 `percentile_ranks` 做線性內插 |
| 檔案、記憶體來源的異常沒有 dept／group | 設計時 | 分部門通知靠 Grafana 依 dept 分流，這些異常全進 `unassigned` | `source` 加一個可選的 owners 對照（`interfaces/` 的 owners 格式） |
| `trace-merge`、根因分析的 log、`tune`／`adjust`、回饋網頁仍只有 ES | 範圍外 | 檔案來源能跑 M1～M3 的指令與 M4、M5 的模組，跑不了整條 alert | 要離線跑整條線時再把 `investigator/es.py` 的讀 log 也走 Backend |

- **決定**：不改套件名、不搬 `detector/`、`investigator/`；新加 `sources/`、`models/`，舊路徑全保留；ES 本身不包成 Backend（是就交給它，不是就走原路）。理由：500 多個測試和排程表都指著舊路徑，改名只是搬家，不增加「換來源不用改程式」這件事的價值；ES 不包一層，假 ES 的測試才不用動。
- **下一輪**：拿真的 APM 匯出檔（Kibana 匯 CSV 或 `_search` 的 NDJSON）用 `source: file` 跑一次 train → detect，看欄位對不對、速度夠不夠。
