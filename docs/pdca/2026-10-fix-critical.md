# 修四個會讓整輪失敗、或蓋掉資料的程式問題（2026-10）

**狀態**：完成（四個都修了、各有測試；docker ES 實測）　　**日期**：2026-10-25　　**相關**：分支 `claude/fix-critical`；來源 [end-to-end-flow.md「哪裡不足」](../end-to-end-flow.md) 第 2 點（code-review 2026-10-25）
**模組**：⑥⑧（alert）、⑨（回測、事故）

## Plan：這一次要達到什麼

- **需求**：對過程式，10 個 code-review 的發現裡有 4 個一接真實資料或真的 LLM 就會發生：
  1. `investigator/cmd_alert.py`：LLM 那段沒有 try/except。根因分析的結果裡一個 NaN（沒有對照組時 `delta_ms` 是 NaN）、或異常的 `value` 是 None，`build_pack` 的 `round()` 或 `template` 的格式化就丟例外，**整輪 alert 掛掉**：這一輪的件、事故、心跳都沒寫，10 分鐘後 Grafana 誤報「AIOps 停了」。違反自己定的「只加註、不擋告警」。
  2. `detector/cmd_backtest.py`：`incident add --start "2026-10-05 10:00"` 存的是沒有時區的時間；回測拿它跟有時區的異常時間比，pandas 丟 `TypeError`，**回測從此跑不過**，每筆建議都要 `--force`。
  3. `investigator/cmd_alert.py`：「已確認的事故不覆蓋」是先 `_mget` 看狀態、再 `bulk_index` 整份覆蓋。老師傅在這兩步之間送出回饋，**根因分類、怎麼看出來的被蓋回 `auto`**，而且沒有任何錯誤。
  4. `deploy/gen-cronjobs.py`：alert 的 `activeDeadlineSeconds: 110`，真的 LLM 一件最多 60 秒，一輪兩件新的就超時被砍；砍掉什麼都沒寫，下一分鐘同樣的兩件又是新的，**每分鐘都被砍**。
- **目的**：四個各修掉、各補一個單元測試重現原本的失敗；整合驗證 `loop_e2e` 照過。
- **範圍**：只修這四個。另外 6 個（回饋跨段、漏報建議被擋、回測重複查、網頁錯誤沒回應、template 空句、相似事故前 20 截斷）不在這一輪。

## Do：改動哪些 code、改了什麼、為何要改

| # | 檔案 | 改了什麼 | 為何 |
|---|---|---|---|
| 1 | `engine/investigator/cmd_alert.py` | LLM 那段抽成 `llm_summary(al, findings, similar, acfg)`，整段 try/except：失敗印一行、這件沒摘要、這一輪照寫 | 「只加註、不擋告警」要程式保證，不是靠輸入乾淨 |
| 1 | `engine/core/llm.py` | `build_pack` 的數字經過 `_num`／`_r`：NaN、None、inf 變 None，不再 `round(nan)`；`template` 的 `extra_ms`、`value`、`usual` 都容許 None | 根因分析沒有對照組時 `delta_ms` 就是 NaN，這是正常情況不是例外 |
| 2 | `engine/detector/cmd_backtest.py` | `caught()` 抽到模組層，多一個 `tz`；`_ts(x, tz)`：沒有時區的時間當成設定檔的時區（`train.timezone`，台北）；漏報的 `anomaly_at` 也走 `_ts` | 跟有時區的 detect 結果比才不會丟 TypeError；而且人打的時間本來就是台北時間 |
| 2 | `engine/detector/cmd_feedback.py` | `incident add` 的 `--start/--end`、`feedback add --verdict missed` 的 `--at`：沒寫時區的存進去時就補成台北時間 | 從源頭就帶時區，舊資料靠上面的 `_ts` 兼容 |
| 3 | `engine/investigator/cmd_alert.py` | `write_incidents(es, incidents)`：改用 ES bulk 的 **scripted upsert**：`status` 是 null 或 auto 才 `putAll`，否則 `ctx.op = 'none'`；文件不存在就 upsert。原本的 `_mget` 再 `bulk_index` 拿掉 | 判斷和寫入在 ES 裡一次做（原子），老師傅在中間送出回饋也不會被蓋 |
| 4 | `engine/deploy/gen-cronjobs.py`、`cronjobs.yaml` | alert 的 `activeDeadlineSeconds` 110 → 600，註解寫為什麼 | 真的 LLM 一件最多 60 秒，一輪好幾件新的要跑得完；`Forbid` 會擋重疊，跑久只是晚、被砍是全沒寫 |
| 測試 | `tests/test_llm.py`（NaN／None 不丟例外）、`tests/test_cmd_alert.py`（新：LLM 丟例外回 None、bulk 內容有狀態守門和 upsert、bulk 錯誤會丟）、`tests/test_backtest.py`（沒時區的當台北時間、有時區的不動）、`tests/test_deploy.py`（新：`cronjobs.yaml` 跟產生器一致、alert 時限 ≥ 6 分鐘、每個 Job 都 Forbid） | | |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 修之前真的會壞（測試抓得到） | `git stash` 回到修之前，餵 NaN 的 `delta_ms` 給 `build_pack`；拿沒時區的時間跟有時區的欄位 `between` | 1：`ValueError: cannot convert float NaN to integer`；2：`TypeError: Invalid comparison between dtype=datetime64[us, UTC] and Timestamp`。修之後同樣輸入不丟例外 |
| LLM 壞了這一輪照寫 | 單元測試：`build_pack`／`summarize` 被換成丟例外 | `llm_summary` 回 None，不往上丟 |
| 已確認的事故不被蓋（真的 ES） | docker ES 9.5.5：先寫 auto → 用 `_update` 改成 confirmed（模擬老師傅在中間送出）→ 再用 `write_incidents` 寫 auto | `updated: 0`，狀態仍 confirmed、`root_cause_category: db` 還在、title 沒變 |
| bulk 的內容對 | 單元測試看送出的 ndjson | 每筆 `update` 帶 painless 守門（`status == 'auto'` 才 `putAll`，否則 `ctx.op = 'none'`）和 `upsert` |
| 沒時區的事故時間當台北時間 | 單元測試：事故「10-05 10:00～10:20」沒時區，異常在 02:05Z | 台北時區抓到；當 UTC 抓不到（證明時區真的有作用） |
| CronJob 檔案跟產生器一致、時限夠 | `test_deploy.py` | 一致；alert 600 秒；10 個 Job 都 Forbid、backoffLimit 0 |
| 整條線沒被改壞 | `tests.loop_e2e`（docker ES） | 37 項全過，「件結束時不覆蓋老師傅確認過的事故」仍過（現在是 scripted upsert 擋的） |
| 全部 | pytest 290 個、ruff | 過 |

## Act：Check 後沒改善的、還沒解決的

- **競態只在單機 ES 上證明「寫法是原子的」**（在什麼情況下發現：寫 Check 時）：真的兩個程序同時打的壓力情境沒造；ES 的 update-by-script 本身是文件層原子的，這裡信它。
- **LLM 逾時 60 秒、alert 時限 600 秒都是估的**：一輪超過 10 件新的、模型又慢，還是會被砍。更好的做法是 alert 先把件和心跳寫了、LLM 摘要放最後補（第二次 `_update`），下一輪再做。
- **舊的 `aiops-incidents` 裡沒時區的 `start/end` 沒有回填**：讀的時候用 `_ts` 兼容，沒有改資料；`anomaly_at` 同樣。
- 其餘 6 個 code-review 的項目沒動（回饋跨段、漏報建議被既有 override 擋住沒印、回測重複查 ES、網頁 ES 錯誤沒回應、template 空句、相似事故 ES 前 20 截斷）。
