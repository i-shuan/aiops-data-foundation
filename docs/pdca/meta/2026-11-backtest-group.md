# 回測改成整組測、範圍命中、逐筆剔除；結論分有效／無效／無法判斷／有害（2026-11）

**狀態**：已完成　　**日期**：2026-11-04　　**相關**：分支 `claude/semiconductor-design`（接在 [suggest-features](2026-11-suggest-features.md) 後）
**模組**：⑨

## Plan：這一次要達到什麼

- **需求**：上一輪把「回測告警一筆沒少就不建議」加進去，使用者指出會踩雷：(1) 同一個對象的兩三筆建議（`min_abs`＋`min_count`）各自單獨壓不掉、合起來才壓得掉，一筆一筆測會全被判無效；(2) 建議是「每月最後一天不報」但回測只跑 2 週沒跨到月底，一筆沒少不是建議錯，是沒機會驗。
- **目的**：同一個對象的建議一起測；整組有效才逐筆剔除，看哪筆多餘、哪筆必要；「一筆沒少」先看回測窗內有沒有落在建議範圍的異常，沒有就是「無法判斷」不是「無效」。
- **範圍**：只改 `cmd_backtest`、CLI、`adjust approve` 的擋法；回測的資料流（每天一段跑 `detect_series`、快取）不動。

### 決定：一起跑，再逐筆剔除

| | 一個一個跑 | 一起跑＋逐筆剔除 |
|---|---|---|
| 要合起來才有效 | 各自「一筆沒少」→ 全被殺 | 整組有效 |
| 其中一筆多餘 | 看得出來 | 剔除那步看得出來 |
| 其中一筆有害 | 抓得到是哪筆 | 整組有害就整組退回（不進剔除） |
| 跑幾次 | n | 1 ＋ n |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/cmd_backtest.py` | `run_backtest` 收一筆或一組（list）；資料只跑一次 `before`，`after` 是「現行＋整組」；`_scope_hits`（`before` 裡落在建議範圍——序列、對象、時段／時窗／重置點之後——的筆數）；結論四種 `verdict`：harmful（漏掉已確認事故）、worse（告警變多）、unknown（範圍內 0 筆）、ineffective（範圍內有、一筆沒少）、effective；整組有效且 > 1 筆才逐筆剔除（`ablation`：redundant／necessary）；`siblings`（同序列、同 match、還是 proposed 的其他建議） | 特徵查表後一筆回饋可能提兩筆動作，它們是一個方案 |
| `detector/__main__.py` | `backtest --id` 可給多個；`--group` 自動把同一對象的其他 proposed 拉進來 | 不用人自己找哪幾筆是一組 |
| `detector/cmd_adjust.py` | `approve`：整組有效但這筆 `ablation` 是 redundant → 擋（`--force` 可硬核准） | 多餘的不要寫進去 |
| `tests/test_backtest_group.py`（新，5 個） | 見 Check | 假的 `detect_series` 模擬每種調整壓掉哪些列 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 要合起來才有效的不會被殺 | 假資料：`min_abs`、`min_count` 各自壓不掉、兩個都在才壓掉 | 單獨測 → ineffective；一組測 → effective、兩筆都 necessary |
| 多餘的看得出來 | `min_abs` 自己就壓得掉、`min_count` 拿掉也一樣 | 整組 effective，`ablation` 標 `min_count` redundant，reason 寫「1 筆拿掉也一樣」 |
| 沒機會驗不算無效 | 建議是每月最後一天、回測窗內沒有那個時段 | unknown、`scope_hits` 0、reason「無法判斷」；同資料換成週一 20 點有命中但沒壓掉 → ineffective |
| 整組有害整組退 | 一筆會漏掉已確認事故 | harmful、不進剔除 |
| 舊的單筆路徑不變 | 既有 `test_backtest`（快取、`detect_series` 呼叫次數 2 × 8） | 過 |
| 測試 ES 上整條走一遍 | 清掉今天的建議 → `feedback suggest` → 每個對象 `backtest --group` → 核准 → 重偵測 | wip-worker、order-svc 的 mute 各自 effective（範圍內 19、21 筆）；report-svc 我多塞一筆 `min_count 50` 跟它的 `min_abs` 一組：整組 effective（23461 → 21480、範圍內 1981 筆）、`min_abs` 標 redundant；核准 `min_count` 成功、核准 `min_abs` 被擋「整組有效但這筆多餘」；重偵測：**誤報 17 → 3、誤報率 61% → 21%，真的 11 筆沒掉**（跟上一輪核准 `min_abs` 的結果一樣） |
| 沒弄壞既有的 | 全部測試 | 640 個過（635 ＋ 5） |

- **發現的問題**：替換 `run_backtest` 時把它後面的常數 `RULE_MAX_EXTRA_PER_WEEK` 一起切掉，規則回測的測試抓到，補回。假的 `detect_series` 在窗長 0 的最後一段也回資料，數字變兩倍，是測試的問題，已修。
- **沒測到的**：真實資料上「範圍內有幾筆」的門檻（現在 > 0 就算有機會驗，1 筆也算）；`--group` 的同對象判斷是「序列＋match 完全相同」，同一對象但 match 寫法不同（別名）會被當成不同組。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 迴圈沒排程、沒自動核准 | 設計 | 建議、回測、核准仍要人跑 | 排程 `feedback suggest` ＋ `backtest --group`；安全範圍內自動核准 |
| 逐筆剔除是「拿掉一筆」，三筆以上時兩兩互補的看不出來 | 設計 | 三筆裡兩筆互相替代，各自拿掉都「一樣」→ 兩筆都標多餘，核准時都被擋 | 標多餘的筆數 ≥ 2 時改提示「互相替代，至少留一筆」 |
| precision 建議仍走舊的 z 路 | 回測時 | 對 z 上億的序列提整條 z 7，回測減 8 筆就過 | 改走 `fp_features` |

- **決定**：一起跑再逐筆剔除；0 命中是「無法判斷」不是「無效」；整組有害就整組退。
- **下一輪**：排程與自動核准。
