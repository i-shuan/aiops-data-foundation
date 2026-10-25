# 單次就報：依序列設「連續幾次才算」、偏太多就不用等（2026-10）

**狀態**：已完成　　**日期**：2026-10-25　　**相關**：分支 `claude/instant-anomaly`（從 `claude/alerts` 開）
**模組**：④ 判斷（⑨ feedback 多一個可調的旋鈕）

## Plan：這一次要達到什麼

- **需求**：baseline（Median＋MAD）抓到超標時，一律要**連續 2 個時間窗**才算 anomaly（只跳一次是 warning，不告警）。這擋掉了零星跳動的誤報，但有些序列一跳就是出事：錯誤率平常 0.1%、突然 30%，等下一個時間窗（30 秒到 5 分鐘，看粒度）才報，值班晚了一個窗。規則（`rules.yaml`）那邊 critical 的本來就一次就報，baseline 這邊沒有辦法。
- **目的**：
  1. **指定的序列可以一次就算**：`series.yaml` 每條序列可以自己設 `confirm_points`（蓋過全域的 2），先給 `http.error_rate` 設 1。
  2. **偏太多就不用等**：任何序列，一個點偏離 ≥ `instant_z`（先估 15 倍穩健標準差，是正常門檻 5 倍的 3 倍）就直接算 anomaly，擋住沒想到的暴衝。
  3. 這兩個旋鈕都能被 **learning loop 調**：`tune` 指令、之後回饋的建議，寫進 `aiops-tuning`，依序列、依對象生效（跟 z、min_abs 一樣）。
- **範圍**：只改 baseline 的 status 判斷和 tune 的欄位；不改規則、不改 peer；不做回測（[loop-update](2026-10-loop-update.md)）。

### 設定的優先順序（跟 z、min_abs 一樣）

`aiops-tuning` 的 override（依序列＋對象）＞ 該序列在 `series.yaml` 的設定 ＞ `series.yaml` 的 `status:` 全域值。

### 驗收

1. `http.error_rate` 超標一次就是 anomaly；`http.latency_p95_s` 一次還是 warning、兩次才 anomaly。
2. 任何序列一個點 z ≥ 15 → anomaly（不用等第二個）；z 介於 5～15 照原本規則。
3. `tune --series http.latency_p95_s --match service=^pay --confirm-points 1` 之後，只有 pay 的 p95 一次就算，其他服務照舊；`--instant-z` 同樣。
4. 既有的 warning／anomaly 行為（連續、間斷、方向改變、粗粒度一個點就夠）不變。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/model.py` | `mark_status` 多一個 `instant_z`：超標的點 \|z\| ≥ instant_z 就直接 anomaly（連續計數照算）；`override_for` 多認 `confirm_points`、`instant_z` 兩個欄位 | 判斷的核心在這裡；z 是 `score`／`score_poisson` 本來就算出來的欄位，不用多查 |
| `engine/detector/cmd_baseline.py` | 新增 `status_params`（對象 override ＞ 序列 ＞ 全域）；`with_status` 改成依每個對象的 (confirm_points, instant_z) 分組呼叫 `mark_status`；`cmd_detect`、`detect_borrowed` 把 spec、overrides 傳進去；`tune` 寫入、顯示多兩個欄位 | 跟 z、min_abs 的 `score_tuned` 同一個做法：同一組設定的對象一起算 |
| `engine/detector/tuning.py` | `aiops-tuning` mapping 加 `confirm_points`（integer）、`instant_z`（double） | — |
| `engine/detector/adjust.py` | `tune --list` 的顯示加這兩個欄位 | 第一次實跑 `--list` 只印出「（manual；測試）」，看不到調了什麼 |
| `engine/detector/__main__.py` | `tune --confirm-points`、`--instant-z` | learning loop 的旋鈕 |
| `engine/configs/baseline/series.yaml` | `status.instant_z: 15`；`http.error_rate` 設 `confirm_points: 1` | 錯誤率一跳就是出事；15 = 正常門檻 5 的 3 倍，估的 |
| `engine/configs/baseline/README.md` | status 那一節加兩列、優先順序 | — |
| `engine/tests/test_model.py`、`test_status_params.py`（新） | 10 個測試 | — |

`cmd_trace.py`（trace 每一段的 baseline）呼叫 `with_status` 沒傳 spec：用全域值（`instant_z` 也會生效），沒有依序列設定。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| `mark_status` 的新行為、舊行為不變 | 單元測試 | `confirm_points=1` 一次就 anomaly；z 20 → anomaly、z 6 → warning；沒給 `instant_z` 或 z 是 NaN 照舊；原本的 7 個情境（連續、間斷、方向反轉、粗粒度一個點就夠）照過 |
| 優先順序與分組 | `test_status_params.py` | override（對象）＞ 序列 ＞ 全域；同一批資料裡 `http.error_rate` 一次就 anomaly、`http.latency_p95_s` 還是 warning；override 只打到 `^pay` 的服務，`mes` 照舊；start 之前的窗用來算連續、不回傳；空的不炸 |
| `tune` 寫得進、列得出、刪得掉 | 對 es-t 跑 `tune --confirm-points 1 --instant-z 10`、`--list`、`--remove` | 寫入訊息列出 `confirm_points=1，instant_z=10.0`；`--list` 第一次看不到（修了 `describe`）；`--remove` 正常 |
| 整條 train → detect 沒壞 | `tests/synth_e2e.py`（4 週模擬資料、5 個已知異常、2,592 個正常點）用原本的 `synth.yaml`（沒設 instant_z） | 全部通過：4 個單點暴衝是 warning、連續 3 個是 warning→anomaly→anomaly、誤報 1／2592（0.04%） |
| 開了之後單點暴衝會不會變 anomaly、誤報會不會跟著變多 | 同一批模擬資料，`status.instant_z: 15`＋`synth.errors` 設 `confirm_points: 1`，再 detect 一次 | **z 46（半夜白天流量）、z 20（流量 3 倍）、z 49（延遲 4 倍）三個單點都變 anomaly**；`synth.errors` 4 個點全部 anomaly（含 16:00 那個單點）；那個 z=5.2 的誤報**還是 warning**（沒有被 instant_z 升級），所以誤報沒變多 |

**沒測到**：真實資料下 z ≥ 15 的點有多少是真的（15 是估的）；trace 每一段的 baseline 用全域 instant_z 會不會太敏感。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `instant_z: 15` 是估的 | 沒有真實資料 | 太低會把零星暴衝都報成 anomaly | 正式環境只寫入不通知跑 2～4 週，看 z 的分佈再定；之後回饋能調（已是旋鈕） |
| `http.error_rate` 設 1 之後，量少的 API 一個 5xx 就可能報 | 設計時 | 誤報 | `min_abs: 0.05`（錯誤率至少多 5 個百分點）和「事件數不夠的窗不判斷」還在；真的太吵再對那些 API `tune --confirm-points 2` |
| trace 每一段的 baseline 沒有依序列設定 | `cmd_trace.py` 呼叫時沒傳 spec | 只能用全域 instant_z | 需要時再加 |
| 改了門檻沒有回測 | 範圍 | 核准的人不知道改了會不會漏 | [loop-update](2026-10-loop-update.md) |

- **決定**：兩個旋鈕都做；預設只有 `http.error_rate` 一次就算，其他靠 `instant_z: 15` 兜底。
