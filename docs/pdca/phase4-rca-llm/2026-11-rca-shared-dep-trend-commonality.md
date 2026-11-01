# 根因分析補三樣：共用依賴的同源標記、出事前的趨勢、慢請求的共同點（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/rca-shared-dep-trend-commonality`；來源 [open-issues 第 6 節](../meta/2026-10-open-issues.md)、[rca/README.md「15 個故障案例」B](../../rca/README.md)
**模組**：⑥ 收斂、⑦ 根因分析

## Plan：這一次要達到什麼

- **需求**（open-issues 第 6 節「根因分析的限制」）：
  - 多個服務共用同一個資料庫時，每件事故各算各的，看不出同源。
  - 報告只比「出事時 vs 平常」，看不出是突然壞的（換版、依賴掛掉）還是慢慢惡化（容量、洩漏、資料量長大）；兩種的處置方向不同。
  - 慢請求的共同點（Pod、版本、批號、SQL）沒做：15 個故障案例裡 4 個靠這個抓（單一 Pod 慢、某一版慢、某個批號／SQL 慢）。
- **目的**：
  - **同源標記**（事件整合）：每件從根因結果取「卡在哪個外部依賴」（慢的那幾段、錯誤源頭那幾段 `→` 後面的對象，例如 `oracle`）；開著的、不同服務的件卡在同一個依賴，互相記 `related_alerts`、`shared_dependency`，通知摘要多一行「可能同源：a-… 也卡在 oracle」。**不合併**：是不同服務各自的件（部門不同、處置不同），只提示很可能同一個源頭。
  - **趨勢**（報告第 2 節）：出事前 6 段、每段 10 分鐘的 p95（trace 入口的 `entries.duration_ms`），一路往上且最後 ≥ 1.5 倍＝慢慢惡化；前面平、出事時跳 ≥ 1.5 倍＝突然；其餘＝起伏。寫進報告與結論，存 `findings.trend`。
  - **慢請求的共同點**（報告第 3 節之後）：出事時抽的那批最慢請求 vs 平常那批，每個請求的 span 帶的 Pod、版本、節點（resource）、業務 ID、SQL（attributes）；慢請求裡 ≥ 60% 有同一個值、而且是平常比例的 ≥ 2 倍才列。只有 1 個 Pod 的服務 Pod 這一項兩邊都 100%，不會被列。存 `findings.commonality`。
- **範圍**：
  - 做：`analyze.py` 的 `commonality()`、`classify_trend()`（純計算）；`investigate_full` 兩節；`alerts.py` 的 `dependencies()`、`link_shared()`；`cmd_alert` 跑完根因分析後連結、mapping 三個欄位；`rca.yaml` 參數；單元測試；對測試 ES 上今天 MTTI 情境的 rca-mes-api 事故跑一次 `investigate` 看兩節長什麼樣。
  - 不做：graph RAG（同源標記夠用就不做圖，見 [rca/README.md 構想](../../rca/README.md)）；`e2e/k8s/rca-demo` 加第二個呼叫同一 Oracle 的服務＋真 cluster 跑 `run-rca-e2e.sh`（14 分鐘、佔 cluster）——同源用單元測試驗；LLM 輸入包加這三樣（先看報告好不好用再加）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `investigator/analyze.py` | `commonality(now, ref)`：每個請求的 span 帶的 (看什麼, 值)——resource 的 Pod、版本、節點（值前面帶服務名）、attributes 的 `lot.id`、`db.statement`；慢的那批裡占 ≥ `min_share` 且 ≥ `min_lift` 倍平常的列出來；`is_new`＝平常那批完全沒有這個值。`classify_trend(p95s, now_p95)`：gradual（≥ 70% 的步往上、最後 ≥ 1.5 倍）／sudden（前面在中位數 ±30% 內、出事時 ≥ 1.5 倍）／mixed／unknown；`describe_trend` 一句話 | 純計算、單元測試得到。`is_new` 是實跑時發現的：示範環境每次都先 rollout restart，慢的請求 100% 落在「平常沒見過的 Pod」，那不是「這個 Pod 比別的慢」，是換版——兩種要講不同的話 |
| `investigator/__main__.py` | 第 2 節多一句趨勢（出事前 `trend.windows` × `trend.window` 的 p95，用入口的 `entries.duration_ms`）；第 3 節後多「這批慢的請求有什麼共同點」表；結論多兩條；`findings.trend`、`findings.commonality` | 報告要回答「突然還是慢慢惡化」「問題只在某個 Pod／版本／批號／SQL 上嗎」 |
| `investigator/alerts.py` | `dependencies(findings)`：慢的那幾段、錯誤源頭那幾段 `→` 後面的對象；`link_shared(alerts)`：開著的、不同服務的件卡同一個依賴 → `related_alerts`、`shared_dependency`（對象是另一件開著的件的服務時不算，那是上下游）；回傳有變的件。`summary` 多一行「可能同源：a-… 也卡在 oracle」 | 不合併：不同服務各自的件，部門、處置不同；只提示同源 |
| `investigator/cmd_alert.py` | 跑完每件的根因分析後 `link_shared`，有變的件重寫摘要；mapping 加 `dependencies`、`related_alerts`、`shared_dependency` | 要先有每件的 findings 才能比 |
| `configs/rca/rca.yaml` | `trend: {windows: 6, window: 10m}`、`commonality: {resource, attrs, min_share, min_lift}` | 參數可調 |
| `tests/test_analyze.py`、`tests/test_alerts.py` | 共同點 5 個（單一 Pod 集中、單一 Pod 服務不列、SQL 集中、太少／沒平常、換版的新 Pod 標 `is_new`）；趨勢 3 個；同源 4 個（取依賴、兩件同 Oracle 互標且不合併、上下游不算、resolved／同服務不算） | |
| `configs/rca/README.md`、`docs/rca/README.md`、open-issues 第 6 節 | 預設值兩列、事件整合多一點、15 個案例 B ✅、現況、graph RAG 構想改成先不做 | 跟著程式 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 共同點、趨勢、同源的計算 | 單元測試 12 個；全部 527 過 | 通過 |
| 真資料上兩節長什麼樣 | 測試 ES 今天 MTTI 情境 1（wip-worker 查 Oracle 變慢、1/4 失敗，03:09～03:21Z）跑 `investigate --service rca-mes-api` | 趨勢：「出事前 60 分鐘的 p95 都在 91 ms 左右（91、91、111、91、91、91），出事時 1.54 s → **突然**」——對（是注入的故障）。共同點：慢的 200 個 100% 在 `rca-wip-worker-…-4v2vj`、平常 0% → 標「平常沒見過（換版、新 Pod？）」——對（`run-rca-demo.sh` 每次先 rollout restart，Pod 名字變了）。原本的結論（SELECT LOT_HISTORY → oracle 多 794 ms、connection pool timeout log）不變 |
| 同一小時內重複注入時趨勢怎麼講 | 情境 4 第 3 次（04:31～04:41Z，前一小時已注入兩次） | 「出事前 60 分鐘的 p95 起伏（81 ms、1.54 s、10.04 s、81 ms、10.05 s、81 ms），看不出」——對，前面真的有兩次事故 |
| 同源標記寫進 `aiops-alerts`、摘要多一行 | 單元測試（`link_shared`、`summary`）；`cmd_alert` 的流程改動靠既有的 `test_cmd_alert` 與 527 個單元測試擋 | 通過；**沒有對真 ES 跑 `investigator alert`**（示範環境只有一個服務呼叫 Oracle，跑了也不會有同源） |

**沒測到的**：同源在真環境（兩個服務同時卡同一個 DB）沒看過；共同點的 `lot.id`、`db.statement` 兩項示範資料沒帶，沒看過真的 SQL 集中長什麼樣；LLM 輸入包還沒帶這三樣。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 「平常」是出事前一段時間時，前一小時若也出過事，趨勢會變 mixed、共同點的平常那批也髒 | 情境 4 連跑三次 | 真環境事故一個接一個時第二件的趨勢看不出 | 本來就有的限制（`compare_with` 一週前有資料就不會用出事前那段）；不處理 |
| 換版後慢的請求全在新 Pod 會被列成共同點 | 情境 1 實跑 | 已改成標「平常沒見過」並建議先看部署；但一個服務只有 1 個 Pod 又剛換版時，這條其實沒有新資訊 | 看真環境值班覺得吵不吵再決定要不要把 `is_new` 的 Pod 那列拿掉 |
| 同源只標、不合併，Grafana 兩件各自通知 | 設計 | 兩個部門各收到一則、摘要互相指到對方 | 真環境看過幾次後再決定要不要合成一件 |
| 門檻（60%、2 倍、6 段 × 10 分鐘、1.5 倍）是估的 | 設計 | — | 真事故校 |
