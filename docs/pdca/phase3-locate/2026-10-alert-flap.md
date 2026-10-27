# 件還開著，Grafana 卻發了恢復通知（2026-10）

**狀態**：已完成　　**日期**：2026-10-27　　**相關**：分支 `claude/edge-rca-demo`；發現於 [rca-demo](2026-10-rca-demo.md)
**模組**：⑥ 收斂、⑧ 出口

## Plan：這一次要達到什麼

- **需求**：rca-demo 裡 `rca-mes-api` 那件一直是 open、每分鐘都有寫入，`/it` 卻收到 6 則：真的 firing、兩組假的 resolved＋firing（13:55／13:57、14:18／14:20）、真的 resolved。值班會以為事故結束了。
- **目的**：件開著的期間，Grafana 不發 resolved。
- **範圍**：只處理這個抖動；`aiops-alerts` 的 schema 不改。

## 原因

| | |
|---|---|
| 一件只有一筆 | `aiops-alerts` 的 `_id` 是 alert_id，engine 每輪覆蓋，`@timestamp` 改成這一輪的時間 |
| Grafana 的查詢範圍 | 「排定的評估時間」往前 5 分鐘；查詢實際晚幾秒才送出（K8s 上排定 :20、送出 :25；docker 上 :00、:05） |
| 撞在一起 | 覆蓋落在排定與送出之間時，`@timestamp` 晚於範圍的結束點，那一件查不到 → Grafana 當成恢復；下一輪寫入落在送出之後，又查得到 → firing |
| 為什麼是 13:55、14:18 | `loop` 每輪 3～4 秒，加上睡 60 秒，寫入時間每分鐘往後飄 2～4 秒，約 20 分鐘飄過那 5 秒一次 |
| 正式環境 | CronJob 每分鐘固定秒數啟動、不會飄。寫入固定落在那幾秒的話，**每次**都查不到：事故不會通知，或一直假恢復 |

## 做法比較

| 做法 | 怎麼做 | 效果 | 代價 |
|---|---|---|---|
| **1. 往前寫 `@timestamp`（採用）** | engine 寫開著的件、心跳時用 `now - 1 分鐘` | 寫入的時間永遠早於範圍結束點；固定秒數的 CronJob 也不會撞 | 改一處；5 分鐘的窗剩 4 分鐘有效（每 1～2 分鐘寫一次，夠） |
| 2. 查詢範圍延到未來 | 規則的結束點設成 now 之後 | — | Grafana 規則的相對時間範圍結束點只能是 now 或更早（未實測） |
| 3. 改 schema：不靠時間窗判斷存活 | `@timestamp` 固定為開始時間；規則查 `status:open`＋`updated_at >= now-10m` | 沒有查詢上界，就沒有競態；最乾淨 | 要改 `aiops-alerts` 的寫法、規則、儀表板；engine 停了件會一直開著（心跳規則會報） |
| 4. Grafana 延遲恢復 | `keep_firing_for` 或多等幾次評估 | 偶發的一次查不到不會通知 | 固定秒數每次都撞時沒用；Grafana 12 對單一件不見（MissingSeries）預設已要連續 2 次才恢復，整個查不到（NoData）是一次就轉 |

以後重構 `aiops-alerts` 的 schema 時改走做法 3。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/investigator/cmd_alert.py` | 加 `STAMP_LAG = 1 分鐘`、`stamp_of(now)`；開著的件、心跳的 `@timestamp` 用它（結束的件仍是 `resolved_at`） | 做法 1 |
| `engine/tests/test_cmd_alert.py` | 加 `StampIsInThePast` | 固定「寫入時間在排定評估之後時，`@timestamp` 仍在範圍內」 |
| `e2e/pipeline/run-alert-flap.py`（新） | 對真的 Grafana 重現與驗證：兩件假事故，A 在排定評估前寫、B 卡在排定與送出之間寫；先用舊寫法、再用 `stamp_of` | 這個問題只有接上真的 Grafana 排程才看得到，單元測試抓不到 |
| `engine/configs/grafana/README.md`、`docs/troubleshooting.md` | 寫上 `@timestamp` 往前 1 分鐘與原因 | 文件跟程式一致 |

## Check：測試的目的與結果

| 測試目的 | 怎麼測 | 結果 |
|---|---|---|
| 舊寫法會不會重現 | `run-alert-flap.py`：docker 的 Grafana 12.2.0＋ES 9.5.3，告警設定用 `engine/configs/grafana/`；B 每分鐘在排定評估後 2.5 秒寫入 `@timestamp = 當下`，3 分鐘 | **重現**：B 在第 1、2 次評估都查不到，14:40:00 `Alerting → Normal (MissingSeries)`（被當成恢復），之後一直沒回來 |
| 新寫法還會不會 | 同上，B 寫入 `stamp_of(當下)` | **不再發生**：B 寫入的 `@timestamp` 比當下早 1 分鐘，3 次評估都是 Alerting，被當成恢復 0 次 |
| 對照組 | A 每分鐘在排定評估前 15 秒寫入 | 兩段都是 Alerting，被當成恢復 0 次（腳本印 `PASS`） |
| engine 的單元測試 | `pytest` | 全部通過，含新加的 `StampIsInThePast` |

**沒測到**：修正後在 K8s 的 rca-demo 再跑一次整條線；正式的 CronJob 排程。

## Act：Check 後沒改善的、還沒解決的

| 是什麼 | 在什麼情況下發現的 | 影響 | 打算怎麼處理 |
|---|---|---|---|
| 存活靠「時間窗＋覆蓋同一筆」 | 這次 | 做法 1 是加餘裕，不是消除競態：Grafana 的延遲若超過 1 分鐘仍會撞 | 重構 `aiops-alerts` 時改做法 3 |
| ES 連不上時發 `DatasourceError`、兩條規則各一則 | rca-demo 的心跳規則實測 | 值班看到的不是「AIOps 停了」 | 見 [rca-demo](2026-10-rca-demo.md) 的 Act |
