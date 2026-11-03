# 五個模型拆成模組：資料來源介面＋萬用資料處理器（2026-11）

**狀態**：進行中（Plan 已定，Do 未開始）　　**日期**：2026-11-03　　**相關**：分支 `claude/model-modules`
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

（未開始）

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| | | |

## Check：測試的目的與結果

（未開始）

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| | | |

## Act：Check 後沒改善的、還沒解決的

（未開始）

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| | | | |

- **決定**：不改套件名、不搬 `detector/`、`investigator/`；新加 `sources/`、`models/`，舊路徑用 re-export 保留。理由：500 多個測試和排程表都指著舊路徑，改名只是搬家，不增加「換來源不用改程式」這件事的價值。
- **下一輪**：（未開始）
