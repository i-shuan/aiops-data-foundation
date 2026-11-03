# 回饋提建議改成特徵查表：不累積、讀老師傅的原因、回測擋無效建議（2026-11）

**狀態**：已完成　　**日期**：2026-11-03　　**相關**：分支 `claude/semiconductor-design`（接在 [severity-grading](2026-11-severity-grading.md) 後）
**模組**：⑨

## Plan：這一次要達到什麼

- **需求**：按了既有的回饋迴圈（標誤報 → 累積 ≥ 3 次 → 提建議 → 回測 → 核准），誤報率 61% 按完還是 61%。原因：`suggest_for_false_positives` 三條規則依序判、命中就停——這批誤報「集中在週一 20 點」且「z 上億」，第 1 條看到時段就提「那 1 小時 z 放寬到 7」，沒看第 2 條；z 上億放寬到 7 擋不到。回測只擋「漏掉已確認事故」，告警 43750 → 43750 一筆沒少也寫「建議核准」。另外老師傅在表單選的「為什麼不是問題」（`fp_reason`）完全沒被讀；而且要累積 3 次才提，大部分對象永遠湊不到。
- **目的**：有人標誤報就提；特徵全部算完再查表，一筆可以多個動作；老師傅的原因優先；回測沒效果不建議。同一批 28 筆標記重跑，誤報率要明顯降、真的不掉。
- **範圍**：只改 ⑨ 的提建議與回測；不加新的異常判斷方法；旋鈕都用既有的（`recurring_override` 的 `mute`／`train: exclude`、`exclude_window`、`reset_point`、`min_abs`、`min_count`）。

### 查表

| 時段固定 | z 放寬壓得掉 | 動作 |
|---|---|---|
| 是 | 是 | 那 1 小時 z × 1.4 |
| 是 | 否 | **那 1 小時不報、不學**（`mute` + `train: exclude`） |
| 否 | 是 | 整條 z × 1.4 |
| 否 | 否 | `min_abs` 調到最大偏差 × 1.2 |

老師傅的原因優先：換版（或都在換版後 10 分鐘內）→ `reset_point`；壓測 → `exclude_window`（不報不學）；批次 → 右上那格，1 筆也算得出時段；本來就慢 → `min_abs`；量太少（說的或算的：每筆符合的事件數 ≤ 5）→ 再加一筆 `min_count`。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/feedback.py` | `fp_features`（reason、slot、z_ok、low_volume、after_deploy、max_dev、max_hits 各自獨立算）；`suggest_for_false_positives` 改成查表、回傳 list（一到兩筆）；`_slot` 加 `min_n`（老師傅說批次時 1 筆也算）；`summarize` 的 `min_false_positive` 預設 1 | 特徵不互斥；不累積 |
| `detector/cmd_feedback.py` | `_feedback_rows` 多讀 `fp_reason`；`_anomaly_details` 多讀 `n`、`keys`；`_deploy_before`（用 `cmd_alert.deploy_at` 查換版）；建議的 id 含 kind 與動作（同一對象可以多筆）；`rule.*` 的誤報不提（靜態規則沒有 z） | 把老師傅的原因、事件數、換版時間餵給查表 |
| `detector/__main__.py` | `--min-false-positive` 預設 3 → 1 | 不累積 |
| `detector/model.py`、`cmd_baseline.py`、`adjust.py` | `OVERRIDE_FIELDS` 加 `min_count`；`apply_min_count` 依對象查 override | 「量太少」的建議要能生效 |
| `detector/cmd_backtest.py` | 告警一筆沒少、也沒抓到漏報 → 「無效，不建議」 | 回測要擋無效建議 |
| `tests/test_suggest_features.py`（新，12 個）；`test_suggest_kind.py` 改 5 處取 `[0]` | 見 Check | 舊測試只是回傳型別從 dict 變 list |

## Check：測試的目的與結果

資料：測試 ES 的 28 筆有人標記、對得回異常的告警（誤報 17、真的 11），重跑 11-02 12:00～13:30Z 的 `http.error_rate` 偵測。

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 舊迴圈按了沒用 | 核准舊建議（那 1 小時 z 5 → 7）後重偵測 | 64 筆異常一筆沒變，誤報率 61% → 61% |
| 新迴圈提對建議 | 清掉舊建議，`feedback suggest --days 30` | wip-worker、order-svc 週一 20 點 → `mute` + `train: exclude`（reason 寫「z 放寬到 7 也壓不掉」）；report-svc 1 筆 → `min_abs 0.05 → 0.18`；精確到對象 |
| 回測有把關 | 4 筆各回測 2 週 | 兩筆 mute：43750 → 43731 → 43710；min_abs：23461 → 21651；已確認事故 2 件不受影響 → 都建議核准 |
| **誤報降多少** | 核准後重偵測，同一批標記 | **誤報 17 → 3、誤報率 61% → 21%；真的 11 筆一筆沒掉**；窗內異常 64 → 45 |
| 查表每一格 | `test_suggest_features`：時段＋z 大 → mute；時段＋z 小 → 那 1 小時 z；無時段＋z 小 → 整條 z；無時段＋z 大 → min_abs；1 筆就提；量太少多一筆 min_count | 如預期 |
| 原因優先 | 說換版 → `reset_point`（有換版時間就用它）；說壓測 → `exclude_window` train+detect；說本來就慢 → z 壓得掉也提 `min_abs` | 如預期 |
| `min_count` 依對象生效 | `apply_min_count` 兩個對象一個有 override | 只有有 override 的那個被擋 |
| 沒弄壞既有的 | 全部測試 | 635 個過（623 ＋ 12） |

- **發現的問題**：`rule.*` 的誤報也會被提「z × 1.4」（靜態規則沒有 z，沒意義）→ 跳過並印提示。我上一輪加的 precision 建議（整條 z 5 → 7）回測 21651 → 21643 有減 8 筆所以過關，但它實際壓不到這批誤報；留著無害。
- **沒測到的**：`reset_point`、`exclude_window`、`min_count` 這三種建議只有單元測試，沒在 ES 上走完回測、核准；`_deploy_before` 在這批資料查不到換版（舊服務沒有 `service.version`）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 迴圈沒排程 | 設計 | 建議、回測、核准都要人手動跑 | 把 `feedback suggest`、`backtest` 排進 `schedule.yaml`；安全範圍內自動核准（另一輪） |
| 剩 3 筆誤報（mes-api 3、wip-worker 1 之外的） | 核准後 | 不在週一 20 點、z 上億、沒人選原因 → 提了 `min_abs`／`min_count` 但沒核准 | 核准後再量 |
| 精確到「對象」的 mute 只擋那支 API | 設計 | 同一批次影響的別支 API 要各自被標一次 | 回饋表單讓老師傅選「整個服務」 |
| precision 建議提 z 的問題 | 回測時 | 對 z 上億的序列無效但會過回測 | 改成也走 `fp_features`（z 壓不掉就不提 z） |

- **決定**：不累積（有標就提）、特徵獨立算再查表、原因優先、回測擋無效。
- **下一輪**：排程與安全範圍內自動核准。
