# Pod 同儕橫向比對的 z 依其他 Pod 數分級；記憶體趨勢規則（照這個速度幾小時內到上限）（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/peer-z-and-slope`；來源 [open-issues 9.2「3 個 Pod、年紀不同時抓不到記憶體洩漏」](../meta/2026-10-open-issues.md)、[baseline reference「還沒加：趨勢」](../../../aiops-engine/code/configs/baseline/reference.md)
**模組**：④ 判斷（Pod 同儕橫向比對、靜態臨界值規則）

## Plan：這一次要達到什麼

- **需求**：
  - 2026-10-31 `run-peer-e2e` 第 5 項：3 個 Pod 只有一個洩漏（420 MB，另兩個 251、163 MB），多 213 MB、2.03 倍兩道門檻都過，卻因 z = 3.3 < 5 沒報。原因是 z 的分母是「其他 Pod 之間的差距」，其他只剩 2 個值時，滾動更新後新舊 Pod 本來就差 90 MB，把分母撐大。正式環境副本常只有 3 個。
  - 慢慢惡化（記憶體每天漲）沒有任何方法抓：基準線跟著漲、同儕比對受新舊 Pod 高低差影響、單 Pod 的服務沒得比。`container.memory_near_limit` 要到 90% 才報，那時離 OOM 已經很近。
- **目的**：
  - 同儕比對（A2）：z 的門檻依「其他 Pod 數」分級——其他 2 個用 3、3～4 個用 4、5 個以上維持 5；主要靠 `min_abs`、`min_ratio` 擋誤報。所有檢查都套用（錯誤率、p95 的其他 Pod 多半一樣，分母本來就是 `rel_floor`，不受影響）。
  - 趨勢規則 `container.memory_leak`：看每個容器最近 30 分鐘 working set 的斜率（Theil–Sen，對 GC 鋸齒、單點跳動穩健），而且三段的低點要一路墊高（GC 後回不到原來的低點才算漲，純鋸齒不算）；換算成「照這個速度幾小時後到上限（working_set + available）」，≤ 6 小時就報。對象要有滿 30 分鐘的資料，新容器啟動期的暖機不算。
- **範圍**：
  - 做：`peer.py` 的 `compare()` 多 `z_by_peers`；`peer.yaml`；`rules.py` 新 `type: slope`（有 `den` 就算到上限的小時數，沒有就是每小時漲多少）；`rules.yaml` 一條；單元測試；合成資料 e2e 各加案例（同儕：3 個年紀不同、一個洩漏；規則：線性洩漏、純 GC 鋸齒、鋸齒但低點墊高、離上限很遠、新容器、沒設上限）；對測試 ES 跑合成 e2e。
  - 不做：HPA 擴容後老 Pod 被誤報成洩漏（`warmup` 5 分鐘蓋不住 JVM heap 長大）——另案；JVM 老年代的趨勢（要 prometheus 的 JVM 指標，欄位名依 agent 版本不同）——先只做容器 working set；真的 cluster 上跑 `run-peer-e2e.sh`（約 40 分鐘，排程佔 cluster）——這次只跑合成資料。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/peer.py`、`cmd_peer.py` | `compare()` 多 `z_by_peers: {其他 Pod 數: z}`，這組的其他 Pod 數有列就用那個門檻、沒列用 `z`；`cmd_peer` 從設定帶進去 | 其他只有 2～4 個值時 MAD 沒統計意義；分級比一律降到 3 安全（5 個以上維持 5） |
| `configs/baseline/peer.yaml` | `z_by_peers: {2: 3, 3: 4, 4: 4}` | 10/31 的案例 z 3.3；`min_abs`、`min_ratio` 兩道硬門檻不動 |
| `detector/rules.py` | 新 `type: slope`：最近 `for` 這段的 Theil–Sen 斜率（所有兩點斜率的中位數）；`lows_rising`（三段的最低點一段比一段高）；有 `den` 就算 `(上限 − 最新值) ÷ 每小時漲多少` = 幾小時後到上限，沒有就是每小時漲多少；點不夠（`min_points`）、最新的太舊、`for` 開頭沒資料（新容器）不判斷；`show: hours` | 最小平方會被 GC 鋸齒、單點拉偏；純鋸齒的 Theil–Sen 斜率也可能略為正，低點墊高才是「回不來」；新容器啟動期 heap 一路長，等滿 30 分鐘再看 |
| `configs/baseline/rules.yaml` | `container.memory_leak`：`for: 30m`、`lookback: 35m`、`min_points: 40`、`≤ 6` 小時到上限（`working_set + available`），warning | 跟 `container.memory_near_limit` 同一組欄位；90% 那條是「已經快到」，這條是「照這個速度會到」 |
| `tests/test_peer.py`、`tests/test_rules.py` | A2 四種情況；斜率 10 種情況（線性、鋸齒墊高、純鋸齒、上限很遠、新容器、沒上限、平的／往下、單點飆高、沒 `den`、訊息）；設定檢查接受 `slope`、規則自己的 `lookback` | |
| `tests/peer_edge_e2e.py`、`tests/rules_edge_e2e.py` | 合成資料 e2e：`pe-ages3`（3 個 Pod 年紀不同、一個洩漏，洩漏前後兩個時間點）；`container.memory_leak` 8 個案例 | 跟其他邊緣情境一起對真 ES 跑 |
| `configs/baseline/reference.md`、`docs/rca/README.md`、`docs/architecture.md`、open-issues 9.2 | 規則表一列、同儕怎麼比補 `z_by_peers`、「還沒加：趨勢」改成只剩 JVM 老年代、「方法抓不到的：慢慢惡化」改成已有記憶體那條 | 跟著程式 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| A2：10/31 的案例抓得到、兩道硬門檻還在、其他 Pod 多時維持 5 | `tests/test_peer.py`（420 vs 251、163 MB → z 3.3：預設不報、`z_by_peers` 報；差 90 MB／1.4 倍不報；其他 4 個、同樣 z 3.3 不報） | 通過 |
| A2 在真 ES 上、連同原本 19 個同儕情境沒有多出誤報 | `tests/peer_edge_e2e.py` 對測試 ES（合成 metrics，20 個情境、38 個檢查點） | **38／38 通過**：`pe-ages3` 洩漏的 a 報了（412 MB，其他 2 個中位數 207 MB，z 3.1）、洩漏前不報；原本的情境（正常差距、暖機、換 Pod、擴縮容、全壞…）全部跟改前一樣 |
| 斜率算法：線性、鋸齒墊高要報；純鋸齒、上限很遠、新容器、沒上限、平的／往下、單點飆高不報；沒 `den` 時是每小時漲多少；訊息格式 | `tests/test_rules.py` 10 個 | 通過；500 → 800／30 分鐘、上限 1000 算出 0.33 小時、每小時 600 |
| `container.memory_leak` 在真 ES 的資料形狀上（kubeletstats 的 TSDS）判斷正確，其他 39 條規則不受影響 | `tests/rules_edge_e2e.py` 對測試 ES：8 個新案例＋原本 151 個 | 規則 **164／164 通過**（含新的 8 個：兩個要報的報、六個不報的不報）、interval 4／4 |
| 全部單元測試、文件檢查 | `pytest`、`tools/check-docs.py` | 516 過；連結、索引、舊說法、類型 0 個問題 |

**沒測到的**：真的 cluster 上的 `run-peer-e2e.sh`（第 5 項記憶體洩漏，真 .NET 應用）沒重跑——合成資料照 10/31 的數字做，應該會過；`container.memory_leak` 沒有真的洩漏程式跑過（`run-peer-e2e.sh` 的洩漏案例每個請求 32 KB，10 分鐘漲到 420 MB，30 分鐘會到上限，應該會報，沒跑）；6 小時、30 分鐘、`min_points` 40 都是估的。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `rules_edge_e2e` 動態調整那組 9／10：「`adjust approve` 後下一輪載入」沒過 | 這次跑合成 e2e；拿 master 的程式當對照一樣沒過 | 跟這次改的無關：11/01 起 `adjust approve` 對規則草稿也要求先 `backtest`（[suggest-and-rule-backtest](../phase5-learning-loop/2026-11-suggest-and-rule-backtest.md)），測試腳本還是直接 approve，被擋「這筆還沒回測」 | 測試腳本要先跑 `backtest --id` 或加 `--force`；之前說過 rules e2e 的失敗先不修，這次也沒動 |
| HPA 擴容後老 Pod 可能被誤報成洩漏 | 設計時推論（`warmup` 5 分鐘蓋不住 JVM heap 長大） | 新 Pod 150 MB、老 Pod 400 MB 時老 Pod z 約 16、兩道硬門檻也過 | 合成 e2e 的 `pe-scale`（新 Pod 5 分鐘從 200 長到 600 MB）沒報，因為新 Pod 長到跟老的一樣；heap 長更久的服務要另外驗；可能要 `warmup` 依服務設 |
| 其他 Pod 只有 2 個時 z 門檻 3 是從一個案例訂的 | 10/31 的案例 | 太鬆會多誤報 | 真環境跑一陣子看 `peer.pod_memory_bytes`、`peer.pod_cpu` 的誤報 |
| 趨勢只做了容器 working set | 範圍 | JVM 老年代慢慢滿、連線池慢慢用光沒有趨勢規則 | 等 prometheus 的 JVM 指標欄位名在真環境確定後，用同一個 `type: slope` 加規則（不用改程式） |
