# 修四個會讓整輪失敗、或蓋掉資料的程式問題（2026-10）

**狀態**：完成（四個都修了、各有測試；docker ES 實測）　　**日期**：2026-10-25　　**相關**：分支 `claude/fix-critical`；來源 code-review（2026-10-25）的 10 項；還沒動的記在 [poc-status.md「還沒做、還不足的」](../../poc-status.md#還沒做還不足的)
**模組**：⑥⑧（alert）、⑨（回測、事故）

## Plan：這一次要達到什麼

- **需求**：10 個 code-review 發現裡，這 4 個一接真實資料或真的 LLM 就會發生：

| # | 檔案 | 問題 | 後果 |
|---|---|---|---|
| 1 | `investigator/cmd_alert.py` | LLM 那段沒有 try/except；`delta_ms` 是 NaN（沒有對照組）或 `value` 是 None 時，`round()`／格式化丟例外 | **整輪 alert 掛掉**：件、事故、心跳都沒寫；10 分鐘後 Grafana 誤報「AIOps 停了」 |
| 2 | `detector/cmd_backtest.py` | `incident add --start "2026-10-05 10:00"` 存成沒時區的時間，跟有時區的異常時間比 | pandas 丟 `TypeError`，**回測跑不過**，每筆建議都要 `--force` |
| 3 | `investigator/cmd_alert.py` | 「已確認的不覆蓋」是先 `_mget` 看狀態、再 `bulk_index` 整份覆蓋 | 老師傅在兩步之間送出回饋，根因分類等**被蓋回 `auto`**，沒有錯誤 |
| 4 | `deploy/gen-cronjobs.py` | alert 的 `activeDeadlineSeconds: 110`；真的 LLM 一件最多 60 秒 | 一輪兩件新的就被砍、什麼都沒寫；下一分鐘同樣兩件又是新的，**每分鐘被砍** |

- 第 1 點違反自己定的「只加註、不擋告警」。
- **目的**：四個各修掉、各補一個單元測試重現原本的失敗；`loop_e2e` 照過。
- **範圍**：只修這四個。另外 6 個（回饋跨段、漏報建議被擋、回測重複查、網頁錯誤沒回應、template 空句、相似事故前 20 截斷）不在這一輪。

## Do：改動哪些 code、改了什麼、為何要改

| # | 檔案 | 改了什麼 | 為何 |
|---|---|---|---|
| 1 | `engine/investigator/cmd_alert.py` | LLM 那段抽成 `llm_summary(al, findings, similar, acfg)`，整段 try/except | 「只加註、不擋告警」要程式保證，不靠輸入乾淨 |
| 1 | 同上 | 失敗印一行、這件沒摘要、這一輪照寫 | 同上 |
| 1 | `engine/core/llm.py` | `build_pack` 的數字經過 `_num`／`_r`：NaN、None、inf 變 None | 沒有對照組時 `delta_ms` 是 NaN，是正常情況 |
| 1 | 同上 | `template` 的 `extra_ms`、`value`、`usual` 容許 None | 同上 |
| 2 | `engine/detector/cmd_backtest.py` | `caught()` 抽到模組層、多一個 `tz`；`_ts(x, tz)`：沒時區的當設定檔時區（`train.timezone`，台北） | 不丟 TypeError；人打的時間本來就是台北時間 |
| 2 | 同上 | 漏報的 `anomaly_at` 也走 `_ts` | 同上 |
| 2 | `engine/detector/cmd_feedback.py` | `incident add --start/--end`、`feedback add --verdict missed --at`：沒時區的存入時補成台北時間 | 從源頭帶時區；舊資料靠 `_ts` 兼容 |
| 3 | `engine/investigator/cmd_alert.py` | `write_incidents` 改用 bulk **scripted upsert**，拿掉 `_mget`＋`bulk_index` | 判斷和寫入在 ES 裡一次做（原子） |
| 4 | `engine/deploy/gen-cronjobs.py`、`cronjobs.yaml` | alert 的 `activeDeadlineSeconds` 110 → 600，註解寫原因 | `Forbid` 會擋重疊：跑久只是晚，被砍是全沒寫 |

- scripted upsert：`status` 是 null 或 auto 才 `putAll`，否則 `ctx.op = 'none'`；文件不存在就 upsert。

| 測試檔 | 加了什麼 |
|---|---|
| `tests/test_llm.py` | NaN／None 不丟例外 |
| `tests/test_cmd_alert.py`（新） | LLM 丟例外回 None；bulk 有狀態守門和 upsert；bulk 錯誤會丟 |
| `tests/test_backtest.py` | 沒時區的當台北時間；有時區的不動 |
| `tests/test_deploy.py`（新） | `cronjobs.yaml` 跟產生器一致；alert 時限 ≥ 6 分鐘；每個 Job 都 Forbid |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 修之前真的會壞（1） | 修之前的程式，餵 NaN 的 `delta_ms` 給 `build_pack` | `ValueError: cannot convert float NaN to integer` |
| 修之前真的會壞（2） | 修之前的程式，沒時區的時間跟有時區的欄位 `between` | `TypeError: Invalid comparison between dtype=datetime64[us, UTC] and Timestamp` |
| 修之後 | 同樣輸入 | 不丟例外 |
| LLM 壞了這一輪照寫 | 單元測試：`build_pack`／`summarize` 換成丟例外 | `llm_summary` 回 None，不往上丟 |
| 已確認的不被蓋（真的 ES） | docker ES 9.5.5：寫 auto → `_update` 成 confirmed → `write_incidents` 寫 auto | `updated: 0`；仍 confirmed、`root_cause_category: db` 在、title 沒變 |
| bulk 的內容對 | 單元測試看送出的 ndjson | 每筆 `update` 帶 painless 守門和 `upsert` |
| 沒時區的事故時間當台北時間 | 事故「10-05 10:00～10:20」沒時區，異常在 02:05Z | 台北時區抓到；當 UTC 抓不到 |
| CronJob 跟產生器一致、時限夠 | `test_deploy.py` | 一致；alert 600 秒；10 個 Job 都 Forbid、backoffLimit 0 |
| 整條線沒被改壞 | `tests.loop_e2e`（docker ES） | 37 項全過，含「件結束時不覆蓋已確認的事故」 |
| 全部 | pytest 290 個、ruff | 過 |

**沒測到**：兩個程序真的同時寫的壓力情境。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 競態只證明「寫法是原子的」 | 寫 Check 時；單機 ES | 沒造真的同時寫的情境 | 信 ES update-by-script 的文件層原子性 |
| LLM 逾時 60 秒、alert 時限 600 秒是估的 | — | 一輪超過 10 件新的、模型又慢，仍會被砍 | 先寫件和心跳、LLM 摘要最後用第二次 `_update` 補 |
| 舊 `aiops-incidents` 沒時區的 `start/end` 沒回填 | — | 讀時靠 `_ts` 兼容；`anomaly_at` 同樣 | 不改資料 |
| 其餘 6 個 code-review 項目 | 範圍外 | 見下 | 之後的輪次 |

- 其餘 6 個：回饋跨段、漏報建議被既有 override 擋住沒印、回測重複查 ES、網頁 ES 錯誤沒回應、template 空句、相似事故 ES 前 20 截斷。
