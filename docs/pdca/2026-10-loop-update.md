# 回饋回到系統：更新知識庫、門檻調整加回測（2026-10）

**狀態**：完成（docker ES 實測：建議 → 回測 → 核准 → 不再報）　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-plans`；前置 [老師傅回饋入口](2026-10-expert-feedback.md)、[相似事故檢索](2026-10-similar-incidents.md)；主線 [product-loop](2026-10-product-loop.md) 第 8 步
**模組**：⑨ feedback、④ 判斷（門檻、baseline）

## Plan：這一次要達到什麼

- **需求**：回饋收進來之後，現在只有一小圈會動：同一個對象誤報 3 次 → `feedback suggest` 提「z 放寬 1.4 倍」（proposed），人 `adjust approve` 才生效。問題：
  1. **核准的人不知道改了會不會漏**：建議沒有回測，只能憑感覺按。
  2. **確認過的事故不會讓下一次更好**：沒有回到知識庫，也不會變成規則。
  3. **漏報只記下來，沒有下一步**。
- **目的**：每一種回饋都有明確的去處，**改門檻之前一定先回測**，結果附在建議上：

| 回饋 | 去處 | 生效方式 |
|---|---|---|
| 真的（已確認） | 事故進知識庫，下次相似事故檢索查得到 | 自動（只是「可以被查到」，不改判斷） |
| 根因錯 | 用老師傅寫的正確根因蓋掉；這件進評測集 | 自動 |
| 同一個根因分類、同一個服務 ≥ 3 次 | 提「升級成規則」：例如「Oracle 那段 p95 > 1 秒就直接報」，不用等 baseline | proposed，**回測後**人核准 |
| 誤報 ≥ 3 次（現有） | 提放寬門檻、或提排除時段（月底批次、壓測） | proposed，**回測後**人核准 |
| 漏報 | 提補序列、降門檻、或新規則 | proposed，**回測後**人核准 |
| 單一次回饋 | 只記錄 | 不改任何東西（防污染：一個人按錯不會改壞） |

- **範圍**：
  - 做：回測指令（用新設定重跑過去 N 週的 detect／rules，跟現行設定比）；建議上附回測結果；「升級成規則」的建議；知識庫狀態（已確認、已更正）；漏報的建議。
  - 不做：自動核准（全部要人按）；回測 RAG（那是 [similar-incidents](2026-10-similar-incidents.md) 的評測）。

### 回測要回答的三個問題

用新設定重跑過去 4 週（`detect --at` 已經能拿過去的時間當「現在」），跟現行設定比：

1. **告警少了幾則**（誤報有沒有真的變少）。
2. **已確認的事故還抓不抓得到**：每一件已確認的事故，新設定下在同一段時間還有沒有告警。**少抓一件就標「不建議」**。
3. **漏報的有沒有抓到**（漏報的建議）。

結果寫在建議上，`adjust list` 看得到：「回測 4 週：告警 −12 則；已確認事故 7 件全部還抓得到；建議核准」。

### 驗收

1. 造一個情境：某服務月底批次被誤報 3 次 → 建議「這段時間排除」→ 回測顯示告警少了、已確認事故不受影響 → 核准 → 下一次同樣的時段不報。
2. 造一個會漏事故的建議（z 放太寬）→ 回測標「不建議」，列出會漏的那件。
3. 同一個根因 3 次 → 出現「升級成規則」的建議，規則內容看得懂、回測過。
4. 核准、撤銷都留紀錄（誰、何時、回測結果），在 `aiops-tuning`。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/detector/cmd_baseline.py` | `cmd_detect` 的內圈抽成 `detect_series(es, cfg, spec, base, now, window, dopts, tz, overrides, adjs, quiet) → [(結果, DataFrame)]` | 回測要「不寫 ES、換一套調整重跑」，不能複製一份 detect |
| `engine/detector/cmd_backtest.py`（新） | `run_backtest(es, cfg, proposal, weeks)`：現行 vs 現行＋建議，逐日跑 `detect_series`，比 (1) `status: anomaly` 的點數、(2) 已確認事故（confirmed／corrected／resolved）在同服務、同時間範圍是否仍至少有一個 anomaly、(3) 漏報的點抓到沒；少抓一件 → `recommended: false`；告警反而變多又沒抓到漏報 → 不建議。結果寫回 `aiops-tuning` 那筆的 `backtest` | Plan 的三個問題 |
| `engine/detector/cmd_adjust.py` | `approve`：override 類沒回測、或回測不建議 → 拒絕（`--force` 跳過）；記 `approved_by`、`approved_at`；`list` 顯示回測結果 | 核准的人要看得到會不會漏 |
| `engine/detector/feedback.py` | `suggest_missed`（漏報 → 那條序列、那個對象 z × 0.8）；`suggest_rules(incidents, min_recurring)`（同服務、同根因分類確認 ≥ 3 次 → `kind: rule_suggestion`，附 `rule_draft`：`window` 類、p95 規則草稿，慢的那段寫在 `_note`，門檻留給人填） | 漏報有下一步；同根因 3 次升級成規則 |
| `engine/detector/cmd_feedback.py` | `suggest` 多兩種建議；`_confirmed_incidents` | |
| `engine/detector/tuning.py`、`adjust.py`、`__main__.py` | mapping 加 `approved_by/at`、`backtest`、`rule_draft`；`describe` 認 `rule_suggestion`；子指令 `backtest`、`adjust --force`、`feedback --min-recurring` | |
| `engine/feedback_web/app.py`、`investigator/cmd_alert.py` | 確認 → `confirmed`（進知識庫）；根因錯 → `corrected` 並存正確根因；alert 不覆蓋已確認的 | Plan 的表：真的／根因錯的去處 |
| `engine/tests/test_feedback.py`、`test_backtest.py` | 漏報降 z、3 次升級成規則（2 次不提、`--min-recurring 2` 就提）、回測結果的文字 | |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 誤報 3 次 → 建議 → 回測 → 核准 → 不再報（Plan 驗收 1） | `loop_e2e`：mes-report 的「月底批次」請求 +50（z 約 6）三次，老師傅在網頁都標誤報 → `feedback suggest` → `backtest --weeks 1` → `adjust approve` → 刪掉舊異常、同一段再 `detect` | 建議 z 5 → 7（proposed）；回測「告警 3 → 1；已確認事故 3 件都不是這條序列抓的，不受影響 → 建議核准」；核准記 `approved_by: amy`；再 detect 那段沒有 anomaly |
| 沒回測不能核准 | 回測前先 `approve` | 拒絕：「這筆還沒回測：先跑 backtest --id …」 |
| 同根因 3 次 → 升級成規則（驗收 3） | 3 件 mes-api 都確認 db | 一筆 `rule_suggestion`，草稿 `mes-api.db.recurring`（`window`、p95、`threshold: <填：…>`）；只有 2 件時不提（單元測試） |
| 漏報 → 建議 | `feedback add --verdict missed`（eap-gw 凌晨錯誤）→ `suggest` | 一筆 z 5 → 4 的建議（proposed） |
| 會漏事故的建議要標「不建議」（驗收 2） | 單元測試驗文字；**實際情境沒造**（見 Act） | 文字：「不建議（會漏掉 1 件已確認的事故：…）」 |
| 核准留紀錄（驗收 4） | 看 `aiops-tuning` 那筆 | `backtest`、`approved_by`、`approved_at` 都在 |
| 第一次的回測文字誤導 | 第一次跑時回測印「已確認事故 0/3 件還抓得到」 | 那 3 件是別條序列（延遲）抓的，本來就跟這條（請求數）無關，卻像抓不到。改成「3 件都不是這條序列抓的，不受影響」 |

## Act：Check 後沒改善的、還沒解決的

- **「會漏事故」的情境沒有實際造出來**（在什麼情況下發現：寫 Check 時）。模擬裡事故和誤報在不同序列；要造「同一條序列既有事故又有誤報、放寬後事故抓不到」的情境才算驗過驗收 2。回測的判斷邏輯有單元測試，但 `caught()` 用事故的 `services` 對 detect 結果的服務欄位，欄位名稱不是 `service` 的序列（例如 `resource.attributes.service.name`）有對應，其他的對不到會被算成「沒抓到 → 不算漏」，偏寬鬆。
- **月底批次用「放寬 z」是治標**：z 5 → 7 只是剛好壓過這次模擬的 z 6；真的月底批次 z 會是 20、30，放寬到 7 還是會報。該提的是 `recurring_override`（每月 1 號 00:00～06:00 放寬）或排除時段，`suggest` 現在還不會提這種（`fp_reason: batch` 已經存了，下一步用它決定提哪一種）。
- **回測只跑 baseline 的 `detect`**，不跑 `rules`、`peer`、`trace-detect`；規則草稿核准前沒有回測（Plan 說「回測後人核准」，現在規則草稿只是 proposed，要人自己試）。
- **回測的「現行設定」= `aiops-tuning` 裡 active 的 + 設定檔**，但 baseline 本身是「現在這份」，不是當時的；嚴格的回測要用當時的 baseline（每筆異常記版本，還沒做）。
- 單一次回饋不改任何東西（防污染）照 Plan；但 **3 次可以是同一個人**，Plan 寫的「第二個人確認」沒有做。
