# 回饋回到系統：更新知識庫、門檻調整加回測（2026-10）

**狀態**：完成（docker ES 實測：建議 → 回測 → 核准 → 不再報）　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-plans`；前置 [老師傅回饋入口](2026-10-expert-feedback.md)、[相似事故檢索](../phase4-rca-llm/2026-10-similar-incidents.md)；[產品主線 8 步](../../end-to-end-flow.md#產品主線-8-步) 第 8 步
**模組**：⑨ feedback、④ 判斷（門檻、baseline）

## Plan：這一次要達到什麼

- **需求**：回饋進來後只有一小圈會動：同一對象誤報 3 次 → `feedback suggest` 提「z 放寬 1.4 倍」（proposed）→ 人 `adjust approve` 才生效。
  1. **核准的人不知道改了會不會漏**：建議沒有回測。
  2. **確認過的事故不會讓下一次更好**：沒回到知識庫，也不會變成規則。
  3. **漏報只記下來，沒有下一步**。
- **目的**：每一種回饋都有明確的去處；**改門檻之前一定先回測**，結果附在建議上。

| 回饋 | 去處 | 生效方式 |
|---|---|---|
| 真的（已確認） | 事故進知識庫，相似事故檢索查得到 | 自動（只是可被查到，不改判斷） |
| 根因錯 | 老師傅寫的正確根因蓋掉；這件進評測集 | 自動 |
| 同根因分類、同服務 ≥ 3 次 | 提「升級成規則」（如 Oracle 那段 p95 > 1 秒直接報） | proposed，**回測後**人核准 |
| 誤報 ≥ 3 次（現有） | 提放寬門檻、或排除時段（月底批次、壓測） | proposed，**回測後**人核准 |
| 漏報 | 提補序列、降門檻、或新規則 | proposed，**回測後**人核准 |
| 單一次回饋 | 只記錄 | 不改任何東西（防污染） |

- **範圍**：
  - 做：回測指令（新設定重跑過去 N 週的 detect／rules，跟現行比）；建議附回測結果；「升級成規則」的建議；知識庫狀態（已確認、已更正）；漏報的建議。
  - 不做：自動核准（全部要人按）；回測 RAG（[similar-incidents](../phase4-rca-llm/2026-10-similar-incidents.md) 的評測）。

### 回測要回答的三個問題

用新設定重跑過去 4 週（`detect --at` 能拿過去的時間當「現在」），跟現行設定比：

1. **告警少了幾則**。
2. **已確認的事故還抓不抓得到**：每件已確認事故在同一段時間還有沒有告警；**少抓一件就標「不建議」**。
3. **漏報的有沒有抓到**（漏報的建議）。

結果寫在建議上，`adjust list` 看得到，例如「回測 4 週：告警 −12 則；已確認事故 7 件全部還抓得到；建議核准」。

### 驗收

1. 某服務月底批次被誤報 3 次 → 建議排除這段 → 回測顯示告警少了、已確認事故不受影響 → 核准 → 下次同時段不報。
2. 會漏事故的建議（z 放太寬）→ 回測標「不建議」，列出會漏的那件。
3. 同一個根因 3 次 → 出現「升級成規則」的建議，規則看得懂、回測過。
4. 核准、撤銷都在 `aiops-tuning` 留紀錄（誰、何時、回測結果）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/detector/cmd_baseline.py` | `cmd_detect` 內圈抽成 `detect_series(...)`（參數見下） | 回測要不寫 ES、換一套調整重跑，不複製 detect |
| `engine/detector/cmd_backtest.py`（新） | `run_backtest(es, cfg, proposal, weeks)`：現行 vs 現行＋建議，逐日跑 `detect_series`；比較見下 | Plan 的三個問題 |
| 同上 | 結果寫回 `aiops-tuning` 那筆的 `backtest` | 核准的人看得到 |
| `engine/detector/cmd_adjust.py` | `approve`：override 類沒回測、或回測不建議 → 拒絕（`--force` 跳過） | 核准前要知道會不會漏 |
| 同上 | 記 `approved_by`、`approved_at`；`list` 顯示回測結果 | 驗收 4 |
| `engine/detector/feedback.py` | `suggest_missed`：漏報 → 那條序列、那個對象 z × 0.8 | 漏報有下一步 |
| 同上 | `suggest_rules(incidents, min_recurring)`：同服務同根因確認 ≥ 3 次 → 規則建議 | 同根因 3 次升級成規則 |
| `engine/detector/cmd_feedback.py` | `suggest` 多兩種建議；`_confirmed_incidents` | — |
| `engine/detector/tuning.py`、`adjust.py`、`__main__.py` | mapping 加 `approved_by/at`、`backtest`、`rule_draft` | — |
| 同上 | `describe` 認 `rule_suggestion` | — |
| 同上 | 子指令 `backtest`、`adjust --force`、`feedback --min-recurring` | — |
| `engine/feedback_web/app.py`、`investigator/cmd_alert.py` | 確認 → `confirmed`；根因錯 → `corrected`＋正確根因；alert 不覆蓋已確認的 | Plan 表裡真的／根因錯的去處 |
| `engine/tests/test_feedback.py`、`test_backtest.py` | 漏報降 z、3 次升級成規則（2 次不提、`--min-recurring 2` 就提）、回測結果的文字 | — |

- `detect_series(es, cfg, spec, base, now, window, dopts, tz, overrides, adjs, quiet)` 回傳 `[(結果, DataFrame)]`。
- 回測比較：(1) `status: anomaly` 的點數；(2) 已確認事故（confirmed／corrected／resolved）在同服務、同時間範圍是否仍至少一個 anomaly；(3) 漏報的點抓到沒。
- 少抓一件 → `recommended: false`；告警反而變多又沒抓到漏報 → 不建議。
- 規則建議是 `kind: rule_suggestion`，附 `rule_draft`。
- `rule_draft`：`window` 類、p95 規則草稿；慢的那段寫在 `_note`；門檻留給人填。

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 誤報 3 次 → 建議（驗收 1） | `loop_e2e`：mes-report「月底批次」請求 +50（z 約 6）三次，網頁都標誤報 → `feedback suggest` | 建議 z 5 → 7（proposed） |
| 回測（驗收 1） | `backtest --weeks 1` | 「告警 3 → 1；已確認事故 3 件都不是這條序列抓的，不受影響 → 建議核准」 |
| 核准後不再報（驗收 1） | `adjust approve` → 刪掉舊異常、同一段再 `detect` | 記 `approved_by: amy`；那段沒有 anomaly |
| 沒回測不能核准 | 回測前 `approve` | 拒絕：「這筆還沒回測：先跑 backtest --id …」 |
| 同根因 3 次 → 升級成規則（驗收 3） | 3 件 mes-api 都確認 db | 一筆 `rule_suggestion`，草稿 `mes-api.db.recurring`（`window`、p95、`threshold: <填：…>`） |
| 只有 2 件時不提 | 單元測試 | 不提 |
| 漏報 → 建議 | `feedback add --verdict missed`（eap-gw 凌晨錯誤）→ `suggest` | 一筆 z 5 → 4 的建議（proposed） |
| 會漏事故的建議標「不建議」（驗收 2） | 單元測試驗文字；**實際情境沒造** | 「不建議（會漏掉 1 件已確認的事故：…）」 |
| 核准留紀錄（驗收 4） | 看 `aiops-tuning` 那筆 | `backtest`、`approved_by`、`approved_at` 都在 |
| 事故不是這條序列抓的時，回測文字不誤導 | 3 件事故是延遲序列抓的，建議改的是請求數 | 印「3 件都不是這條序列抓的，不受影響」，不印「0/3 件還抓得到」 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 「會漏事故」的情境沒實際造（驗收 2） | 寫 Check 時：模擬裡事故和誤報在不同序列 | 只有單元測試 | 造同一序列既有事故又有誤報、放寬後抓不到的情境 |
| `caught()` 用事故的 `services` 對 detect 結果的服務欄位 | 同上 | 欄位對不到的序列算成「沒抓到 → 不算漏」，偏寬鬆 | `service`、`resource.attributes.service.name` 已有對應；其他待補 |
| 月底批次用「放寬 z」是治標 | z 5 → 7 只是剛好壓過模擬的 z 6 | 真的月底批次 z 是 20、30，放寬到 7 還是會報 | 用已存的 `fp_reason: batch` 改提 `recurring_override`（每月 1 號 00:00～06:00）或排除時段 |
| 回測只跑 baseline 的 `detect` | — | 不跑 `rules`、`peer`、`trace-detect`；規則草稿核准前沒回測 | 規則草稿先由人自己試 |
| 回測用「現在這份」baseline，不是當時的 | — | 現行設定 = `aiops-tuning` active＋設定檔，但 baseline 不是當時的 | 每筆異常記 baseline 版本（還沒做） |
| 3 次可以是同一個人 | — | Plan 的「第二個人確認」沒做 | 待排 |
