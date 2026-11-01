# 回饋建議不只調 z、規則核准前要回測（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/learning-loop-gaps`；來源 [open-issues 9.2](../meta/2026-10-open-issues.md)
**模組**：⑨ feedback

## Plan：這一次要達到什麼

- **需求**：
  - `feedback suggest` 對誤報累積的對象只會提「z 乘 1.4」。月底批次這種「固定時段才高」的誤報，該提的是週期放寬（`recurring_override`）；偏差的絕對值很小、z 卻很高的誤報（平常不動的序列），z 乘 1.4 也壓不掉，該提的是 `min_abs`。
  - 「升級成規則」（`rule_suggestion`）核准時不用回測，override 類卻一定要。規則一核准就每次都報，誤報的代價更高。
- **目的**：
  - 同一批誤報，依誤報的時間與偏差選一種建議：時間集中在同一個時段 → 週期放寬；偏差小、z 高 → `min_abs`；其他 → z。仍是 proposed，回測加人核准才生效。
  - `backtest` 也能跑 `rule_suggestion`：過去 N 週每 5 分鐘一點，看那幾件事故抓不抓得到、事故以外會多報幾次；`adjust approve` 對規則也要求回測（`--force` 可跳過）。
- **範圍**：不提粒度（改粒度要重新 train，回測現在比不了）；判斷用的門檻（同一時段的比例、偏差幾倍 `min_abs`）是估的。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/feedback.py` | 新增 `suggest_for_false_positives`：八成以上的誤報在同一個時段（先看每月最後一天的同一小時，再看同一個星期幾的同一小時）→ `recurring_override`（那 1 小時 z × 1.4）；每一筆 z 都 ≥ z × 1.4 → `min_abs` = 最大偏差 × 1.2；其他 → 原本的 `suggest_override` | 見 Plan。時段只放寬 1 小時，其他時間的真異常照報；z 放寬壓不掉的，只有 `min_abs` 有用 |
| `detector/cmd_feedback.py` | `suggest` 把被標誤報的那幾筆異常讀回來（`_anomaly_details`：時間、值、平常值、z）交給上面的函式；同一個對象已經有建議（任何 kind）就不重寫 | 原本只拿到「誤報幾次」，選不了放寬方式 |
| `detector/cmd_backtest.py` | 新增 `rule_backtest_result`（純計算）與 `run_rule_backtest`：規則草稿過去 N 週每 `lookback` 一點算值，事故時間前後各放一個 `lookback` 內有觸發算抓到，其他觸發算多報；全部抓到、平均每週多報 ≤ 1 次才建議；`backtest` 遇到 `rule_suggestion` 走這條 | 規則核准後每次直接報，誤報代價比 baseline 高；原本只有手動 `rules --at` 看事故時間 |
| `detector/cmd_adjust.py` | `approve` 對 `rule_suggestion` 也要求回測且建議（`--force` 可跳過） | 同上 |
| `tests/test_suggest_kind.py`（新）、`tests/loop_e2e.py` | 單元測試；loop_e2e 的規則改成「沒回測不能核准 → 回測 → 核准」 | 跟著程式 |
| `configs/baseline/reference.md`、`docs/feedback/README.md` | `feedback suggest`、`backtest`、`adjust approve` 的說明 | 跟著程式 |

## Check：測試的目的與結果

環境：Rancher Desktop、ES 8.19.23；單元測試加 `tests.loop_e2e`（真的 ES、`feedback_web`，`LLM_PROVIDER=template`）。

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 依誤報選放寬方式 | `tests/test_suggest_kind.py`：月底同一小時、同一個星期幾同一小時、z 高偏差小、時間分散、欄位缺 | 月底 → `recurring_override`（`0 20 * * *`、`days_of_month: [-1]`，用 `in_schedule` 驗證涵蓋月底 20 點、不涵蓋月中）；星期一 9 點 → `0 9 * * 1`；z 都 ≥ 20 → `min_abs` 0.036；其他與欄位缺 → 原本的 z × 1.4 |
| 規則回測的判斷 | 同上檔案 `RuleBacktest`：抓得到不多報、多報太多、抓不到事故、`min_events` 擋掉薄的點 | 2 件都抓到且事故以外 0 次 → 建議；事故以外 2 次（2 天，每週 2 次，上限 1）→ 不建議；門檻提到 700 抓不到 → 不建議；`n` < `min_events` 的點不算觸發 |
| 全流程 | `tests.loop_e2e` | 45 項全過：規則沒回測不能核准 → 回測（3 件事故都抓得到、事故以外 0 次）→ 核准 → `detector rules` 自動載入、在第 2 次事故的時間觸發、平常時間不觸發；誤報建議、回測、核准、漏報建議、metrics 都照舊 |
| 單元測試 | `pytest` | 422 個通過 |

第一次跑 `loop_e2e` 時規則回測 3 項 FAIL：`fetch` 的 `interval` 我傳了 pandas 的 `5min`，ES 的 `fixed_interval` 只收 `5m`，回測丟例外、沒寫結果。改成直接用規則的 `lookback`（`5m`）後全過。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 選放寬方式的門檻（八成同一時段、z 都 ≥ z × 1.4、`min_abs` = 最大偏差 × 1.2）、規則回測每週多報上限 1 次是估的 | 設計時 | 選錯方式時，回測會擋下（告警變多或漏事故），不會直接生效 | 上線後看值班實際核准／退回的比例再調 |
| 規則回測用 `lookback` 一點算一次，對齊方式跟 `rules` 每分鐘跑不完全一樣 | 設計時 | 回測可能少看到或多看到邊界上的一次觸發 | 可以接受；要精確得改成每分鐘 |
| e2e 只有「月底」那一類用合成資料驗過 `recurring_override` 的涵蓋範圍（單元測試），沒有用真的誤報走一遍 `suggest → backtest → approve` | `loop_e2e` 的批次誤報在 13:00、17:00、09:00，沒有集中在同一小時 | 週期放寬那條路只有單元測試 | 有真實月底誤報時再驗 |
