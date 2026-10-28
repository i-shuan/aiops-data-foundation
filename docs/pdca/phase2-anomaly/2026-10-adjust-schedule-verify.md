# 排除與調整、行事曆、慢性病清單的邊界；engine 第一次在 K8s 上排程跑（2026-10）

**狀態**：已完成　　**日期**：2026-10-28　　**相關**：[排除已知干擾](2026-10-exclude-adjust.md)、[慢性病清單與行事曆](2026-10-chronic-calendar.md)、[排程上線](../phase3-locate/2026-10-schedule.md)、[第一階段只開 4 個 CronJob](../phase5-learning-loop/2026-10-rule-threshold.md)
**模組**：②（排除、行事曆）、④（動態基準線學習、靜態臨界值規則）、⑤⑥（排程寫 `aiops-anomalies`、`aiops-alerts`）

## Plan：這一次要達到什麼

- **需求**：
  1. 排除時段、重置點、週期放寬、行事曆、慢性病清單只測過一般情況；跨日、重疊、暖機邊界、假日當天真的故障沒測過。
  2. `engine/deploy/` 的 CronJob 沒在 K8s 上跑過：照頻率跑、`Forbid`、`activeDeadlineSeconds`、失敗重試都沒驗。
- **目的**：每個邊界先寫預期再實測；第一階段 4 個 CronJob（rules、detect、alert、train）在本機 K8s 連 `es-test` 跑 30 分鐘。
- **範圍**：自己的 ES 灌合成資料（A）；namespace `aiops-engine-test`（B），做完刪掉。不改 Collector、不動 `es-test` 的 index 結構。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/sources.py` | 行事曆來源可寫 `match`、`series`、`detect`，套到它的每一筆 | 台灣行事曆每天沒有 `match`，假日那天所有服務都不判斷 |
| `engine/detector/adjust.py` | `local_iso`、`localize`：沒寫時區的時間當台北時間 | 以前當 UTC，差 8 小時；行事曆本來就當當地時間 |
| 同上 | `load` 讀 `adjustments.yaml` 時補時區 | 同上 |
| `engine/detector/cmd_adjust.py` | `--start`、`--end`、`--at`、`--expires` 用 `local_iso` | 同上 |
| `engine/deploy/gen-cronjobs.py` | 每天、每週的 `backoffLimit` 2；每幾分鐘的維持 0 | 每天一次的失敗不重試要等一整天 |
| `engine/deploy/cronjobs.yaml`、`cronjobs-stage2.yaml` | 重新產生（train、trace-train、chronic 變 2） | — |
| `engine/tests/test_sources.py`、`test_adjust.py`、`test_deploy.py` | 來源範圍、沒寫時區、重試次數 | — |
| `engine/tests/adjust_edge_e2e.py`（新） | A：31 項，可重跑 | — |
| `e2e/local-k3s/run-schedule-e2e.sh`、`engine-schedule/`（新） | B：overlay 只換 namespace、ES、image，拿掉回饋網頁 | CronJob 設定照 `engine/deploy` 原樣測 |
| `calendar.yaml`、`adjustments.yaml`、baseline `README.md`、`deploy/README.md` | 寫明來源要寫 `match`、時區、重試 | — |

## Check：測試的目的與結果

### 排除與調整（A）

`ES_URL=http://127.0.0.1:31505 ES_AUTH=x:x python -X utf8 -m tests.adjust_edge_e2e`（ES 9.5.3，5 分鐘一點，22 秒跑完；同一個 ES 連跑兩次結果相同）。

| 項目 | 預期 | 實測 |
|---|---|---|
| 兩段重疊的排除時段 | 少學的點＝聯集 672 | 672 |
| 跨日排除邊界 [開始, 結束) | 週一 21、22 點、週四 5、6 點：0、12、12、0 | 0、12、12、0 |
| 事故期間不學 | 週二 12 點 p95：沒排除 ≥ 250、排除 ≤ 110 | 303、102 |
| `adjust add` 沒寫時區 | 02:00～05:00 台北時間：2、4、5、10 點 12、12、0、0 | 12、12、0、0 |
| 重置點補標前學的 baseline | 不判斷；沒標的對照組報 288 點 | 0、288 |
| 重置後重新 train | 只學換版後：level `hour`、平常 190～210 | `hour`、201 |
| 暖機 24 小時邊界 | 23.5 小時的爆量不報、24.5 小時的報 | 只報 24.5 小時那點 |
| 暖機後新的平常 | 200 不報、爆量 1000 報 | 只報爆量 |
| 不在週期內（09-29） | 批次 48 點照報 | 48 點 |
| 月底放寬 z 100 | 批次（z≈40）不報、爆量照報 | 只報 21:00 爆量 |
| 週期邊界 | 19:55、隔天 00:00 照報 | 兩點都報 |
| 假日不學（限定 office） | office 週三 12 點 36 點、fab 48 點 | 36、48 |
| 補班日、一般週末 | 補班日不學、一般週末不算假日：36 | 36 |
| 活動日 `detect: true` | 不學 36；當天 3 倍照報 144 點 | 36、144 |
| 補班日當天 | 有行事曆 0；沒有行事曆 ≥ 140 | 0、144 |
| 假日流量掉 | 有行事曆 0；沒有行事曆 ≥ 140 | 0、144 |
| 假日當天 fab 真的故障 | 24×7 照報 12 點 | 12 點 |
| 假日當天 office 故障 | 動態基準線學習不判斷（設計） | 0 |
| 同上，靜態臨界值規則 | 不看行事曆，office、fab 都報 | 都報 |
| 台灣來源沒寫 `match` | fab 的故障也被吞 | 0（所以要寫 `match`） |
| 維護 02:00～06:00（限 fab） | 期間不報；超時 06:00～06:25 報 6 點 | 6 點 |
| 維護期間 office 故障 | 不受 fab 的維護影響，報 6 點 | 6 點 |
| 慢性病：天天噴錯 | 進清單 7 天，沒有 `error.type` 歸「（無）」 | 7 天、`None` |
| 慢性病：修好後 | 下週不在、上週那份還在 | 下週沒有、上週有 |
| 慢性病：天數邊界 | 7 天 6 天進、5 天不進 | 進、不進 |
| 慢性病：筆數邊界 | 每天 20 筆進、19 筆不進 | 進、不進 |
| 慢性病：今天、只有 1 天 | 都不算 | 都不算 |
| 慢性病：天天觸發的規則 | 進清單，停了下週離開 | 進、離開 |
| 負責人 | 依 `match` 對到 | 對到 |

- 行事曆的台灣假日是 TaiwanCalendar 格式的測資（日期是挑的），放在快取資料夾，不連網路。
- 第一次跑「天天觸發的規則」FAIL：測試用 `keys.service` 找，規則那類的 `keys` 是 `series`＋對象。改成先用 `series` 找；ES 裡那筆本來就是 W41 7 天、W42 沒有。

**發現並修掉的問題**：

| 問題 | 證據 | 修法 |
|---|---|---|
| 台灣行事曆一打開，假日那天 24×7 的服務也不學、不判斷 | 測試「沒寫 `match`」：fab 的故障 0 點 | 來源可寫 `match`；`calendar.yaml` 註明一定要寫 |
| `adjust add --start 2026-09-21T02:00` 被當 UTC | 存成 `02:00:00`，判斷時是台北 10:00 | 沒寫時區當台北時間 |
| 每天的 train 失敗要等明天才重跑 | `backoffLimit: 0`＋每天一次 | 每天、每週的改 2 |

### K8s 排程（B）

`e2e/local-k3s/run-schedule-e2e.sh`：Rancher Desktop（k3s v1.30.14），namespace `aiops-engine-test`，連 `es-test`（ES 9.5.5），image 301 MB。21:57～22:27 觀察 30 分鐘；腳本修兩個自己的錯之後整支再跑一次（觀察 5 分鐘），全部通過。

| 項目 | 預期 | 實測 |
|---|---|---|
| rules、detect、alert 每分鐘 | 30 分鐘各排 29 次、都成功、沒漏 | 各 29／29／0 |
| 跑最久（含起 Pod） | ≤ `activeDeadlineSeconds` 的 60% | 8、10、7 秒（中位數 5、4、4） |
| 同一個 CronJob 同時在跑 | 最多 1 個 | 1 |
| 心跳 | 每分鐘更新、間隔 ≤ 120 秒 | 30 次、最長 60 秒 |
| rules 寫 `aiops-anomalies` | 有 | log 觸發 111 次；ES level rule 116 筆 |
| detect、alert 寫入 | 照實記 | baseline 異常 42 筆；`aiops-alerts` 更新 19 件 |
| train 手動 | 成功、≤ 2,160 秒 | 21 秒 |
| train 照台北時間排程 | 改成 2 分鐘後，那一分鐘排出、成功 | 22:52 排出、成功 |
| `Forbid` | alert 睡 150 秒時後兩分鐘不開新的 | 0 個；同時最多 1 |
| `activeDeadlineSeconds` | detect 卡住被砍 | 50 秒 `DeadlineExceeded` |
| key 壞掉（每分鐘的 3 個） | Job Failed；修好下一輪成功 | 都 401 → Failed；下一輪成功 |
| train 連不到 ES | Pod 跑 3 次、`BackoffLimitExceeded` | 3 次 |
| 收尾 | 刪 namespace、作廢 key | 都做了 |

- detect 的 baseline：手動 train 學到的都是 `all` 層（`es-test` 只有約 1 天的資料）。
- ES 的 116 筆含同時間別的工作寫的；log 數的 111 是這裡的 rules 觸發的。
- 腳本自己的錯（第一次 30 分鐘那輪 FAIL，修好重跑通過）：

| 錯在哪 | 現象 | 修法 |
|---|---|---|
| 心跳把部署前（10-27）的舊值算進去 | 最長間隔 84,239 秒 | 只看開始之後的；實際 30 次、最長 60 秒 |
| Git Bash 的 `TZ=Asia/Taipei` 默默變 UTC | 排程寫成 14:29，等不到 | 改 `TZ=UTC-8` |
| Git Bash 的 `kill` 沒停掉 port-forward | 第一輪結束後 31515 還在 | cleanup 再用 Windows PID `taskkill` |

**沒測到的**：真的台灣行事曆（用同格式的測資）；`trace-train`／`trace-detect` 套用調整；stage2 的 6 個 CronJob；Grafana 的「AIOps 停了」規則接這次的心跳；回饋網頁 Deployment（overlay 拿掉了）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [④] 假日當天週休系統自己的故障，動態基準線學習不判斷 | A 實測：office 假日故障 0 點 | 只剩靜態臨界值規則把關 | 要的話假日改用「週日」的 baseline 比（要改 `score`），請決定 |
| [④] 重置後 3.5 天只有 `hour` 層（不分星期幾） | A 實測 level `hour` | 週末量少的服務暖機後幾天可能誤報 | 用真實換版資料看 |
| [④] detect 的 `activeDeadlineSeconds` 50 秒只在小資料驗過 | B 實測最久 10 秒（30 多條序列、`all` 層） | 正式環境序列多可能被砍 | 上線第一週看 Job 時間再調 |
| [⑥] 只在單節點 k3s、本機 image 驗過 | B 用 `nerdctl -n k8s.io build`，不經 registry | 從 Nexus 拉 image、多節點排程未驗 | 正式環境部署時看第一輪 |

- **決定**：行事曆來源加範圍，不改「假日不學不報」的預設；每天的 CronJob 失敗重試 2 次。
- **下一輪**：stage2 的 6 個 CronJob 照順序開（quality → peer → trace → chronic）。
