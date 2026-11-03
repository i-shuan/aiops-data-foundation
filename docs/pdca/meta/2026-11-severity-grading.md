# 嚴重度分級：critical／high／warning 依訊號廣度與持續時間，只通知 critical、high（2026-11）

**狀態**：已完成　　**日期**：2026-11-03　　**相關**：分支 `claude/semiconductor-design`（接在 [case-classify](2026-11-case-classify.md) 後）
**模組**：⑥⑧

## Plan：這一次要達到什麼

- **需求**：合成的 5 個情境（換版震盪、換版後退化、只有 CPU 高、資源＋黃金訊號事故、外部依賴慢）跑出 7 件，其中 4 件是誤報（換版後 4 分鐘的震盪、三個單點錯誤率跳一下），但 7 件全是 `warning`，真事故（118、110、46 筆異常）跟單點誤報同一級，分不出來。原本只有靜態規則會給 `critical`，動態基準線學習的異常一律 `warning`。
- **目的**：嚴重度代表「要不要吵人」而不是「偏離幾倍」：依**訊號廣度**（幾類黃金訊號同時異常）＋**持續時間**（滑動窗覆蓋率）＋**換版上限**分成 critical／high／warning；Grafana 只通知 critical、high。同一組資料重跑要是 critical 2、high 1、warning 4，通知管道 0 誤報、0 漏報。
- **範圍**：`investigator/severity.py`（純函式）、`cmd_alert` 接上、Grafana policies；不改偵測（④）、不改併件規則。

### 分級規則

| 級別 | 條件 |
|---|---|
| critical | 靜態規則 critical；或 ≥ 3 類黃金訊號同時異常、跨 ≥ 3 分鐘（單點不算） |
| high | 2 類；或 1 類但持續（第一筆到最後一筆 ≥ 10 分鐘、最後 15 分鐘窗內 ≥ 80% 的分鐘有異常）；或 1 類但 ≥ 2 條序列／對象都跳 |
| warning | 其餘：單點、單一序列、沒持續 |
| 換版上限 | 最後一筆異常離換版 < 10 分鐘 → 上限 warning；過了 10 分鐘還有異常就照上面重評（看異常的時間、不看現在，回放也一樣） |

黃金訊號的類（看序列名）：latency、errors、downstream（`dep.*`、往外呼叫）、saturation（CPU、記憶體、連線池、lag）。traffic 不算廣度。一類至少 2 筆異常才算一個維度（1 筆零星的不算）。廣度算「幾類」不是「幾條」：3 支 API 的錯誤率都跳仍是 errors 一類。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `investigator/severity.py`（新） | `signal_class`（序列名 → 類）、`coverage`（滑動窗覆蓋率、跨度）、`grade`（廣度、持續、換版上限 → 級別與原因）、`GradeOptions`（窗 15、覆蓋 0.8、持續 10、critical 至少跨 3 分鐘、一類至少 2 筆、換版寬限 10） | 純函式，測試不用 ES；參數可從 `rca.yaml` 的 `alerts.severity` 蓋過 |
| `investigator/cmd_alert.py` | `grade_alert`：併件後對每件服務類的件算嚴重度（舊異常用 `_mget` 補齊才算得出持續）、`deploy_at`（查 `service.version` 第一次出現的時間，最早的版本不算）；寫 `severity_why`、`deploy_at` 兩欄 | 一件的嚴重度要看它全部的異常，不只這一輪的 |
| `investigator/alerts.py` | `SEVERITY` 加 `high` | `_attach` 取最高時要認得 |
| `grafana/helmchart/files/policies.yaml` | `muteTimes` 加 `always`；第一條路由 `severity=warning` → 永遠靜音 | warning 仍在 Grafana 告警清單、儀表板看得到，不送通知 |
| `tests/test_severity.py`（新，17 個） | 見 Check | |

## Check：測試的目的與結果

資料：合成的 5 個新服務（10 天歷史＋最近 3 小時 5 個情境，標準答案在測試 ES 的 `scn-truth`），`train` 學到情境之前、`detect --window 3h` 回放、`alert --at` 回放。

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 改之前分不出來 | 同一組資料，改之前 | 7 件全 warning；通知管道若含 warning：誤報 4／7 |
| 改之後對到標準答案 | 同一組資料重跑 `alert` | **critical 2（ord-svc、bill-svc：3 類跨 37／30 分鐘）、high 1（cart-api：errors 一類、2 條序列、持續 84 分鐘）、warning 4（pay-api 換版後 4 分鐘內、原本 critical 被鎖；3 件單點錯誤率）**；通知管道（critical＋high）0 誤報、0 漏報 |
| 廣度算類不算條 | 3 支 API 的錯誤率；錯誤率 46 筆＋延遲 1 筆 | 都是 1 類 → high，不是 critical |
| 持續的滑動窗 | 第 3、7 分鐘掉回去（15 分鐘窗 13/15） | 仍算持續 → high；每 10 分鐘一點跨 30 分鐘 → 不持續 → warning |
| 換版上限與重評 | 異常都在換版後 6 分鐘內 → warning；延續到 20 分鐘 → critical；回放時「現在」是 5 小時後也一樣 | 如預期 |
| `grade_alert` 補舊異常、查換版 | 假 ES：`_mget` 只要這一輪沒有的 id；`service.version` 兩個版本取新的第一次出現 | 如預期 |
| 沒弄壞既有的 | 全部測試 | 623 個過（606 ＋ 17） |

- **發現的問題**：第一版用「現在」算換版寬限，回放時換版已是 2 小時前，pay-api 被評成 high；改成看最後一筆異常離換版多久。第一版 traffic 有別的類就一起算、一筆零星的延遲也算一類，cart-api 被評成 critical（errors＋latency 1 筆＋traffic 3 筆）；改成 traffic 不算、一類至少 2 筆。
- **沒測到的**：Grafana 的 `muteTimes` 只改了檔，沒重新 provision 看通知真的不送；真實資料上「持續 10 分鐘、80%」這兩個數字沒調過；`deploy_at` 只對 APM 的 `service.version`（OTel 來源的欄位不同，查不到就不設上限）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| inv-svc 只有 CPU 高的情境完全偵測不到 | 情境資料 | APM 的 `metrics-apm.app.*` 有 CPU，但 `configs/apm/series.yaml` 的 `pod.cpu` 是註解掉的（公司的 agent 沒送） | 公司有 system metrics 時打開；分級已經能把「只有 saturation 一類」壓在 warning |
| 基準線會把正在發生的事故學進去 | 第一次 train 把情境窗學進去，cart-api 的退化幾乎偵測不到 | 事故拖過每天 02:00 的 train 就會被當平常 | train 排除 open 的件涵蓋的時段（用 `aiops-alerts` 當排除時段） |
| warning 的件沒有人會看 | 設計 | 單點誤報不吵人了，但真的小事故也沒人看 | 儀表板加「warning 的件」一區；累積回饋後看 warning 裡真事故的比例 |
| 換版偵測只看 `service.version` | 設計 | 設定變更、重啟 Pod 不算換版 | 之後接 CMDB 的變更單或 k8s 的 Deployment 事件 |

- **決定**：traffic 不算廣度；一類至少 2 筆；換版上限看異常時間不看現在。
- **下一輪**：真實資料上看 warning 裡有多少真事故，調 `min_duration`、`min_coverage`。
