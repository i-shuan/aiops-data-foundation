# 單次就報：依序列設「連續幾次才算」、偏太多就不用等（2026-10）

**狀態**：進行中　　**日期**：2026-10-25　　**相關**：分支 `claude/instant-anomaly`（從 `claude/alerts` 開）
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

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
