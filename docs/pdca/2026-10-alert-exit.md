# 事件整合，通知交給 Grafana（2026-10）

**狀態**：進行中（只有 Plan；2026-10-25 改範圍：通知交給 Grafana）　　**日期**：2026-10-25　　**相關**：分支 `claude/alert-exit`（從 `claude/single-writer-key` 開，要用到它的保底規則）
**模組**：⑥ 收斂（最小版）；⑧ 出口交給 Grafana

## Plan：這一次要達到什麼

- **需求**：④ 判斷的結果（baseline、規則、Pod 互比、trace 每一段、資料品質）只寫進 `aiops-anomalies`，值班不會去查 ES，所以**現在做出來的判斷沒有人看得到**。直接把每筆異常送出去也不行：一次 DB 變慢，30 分鐘裡可能同時有好幾個 API 的 p95、錯誤率、連線池規則、跟兄弟比，上百筆（[system-stages.md 第 7 節](../system-stages.md)），值班會被洗版、直接關掉通知。
- **決定（2026-10-25）**：**通知不自己做，交給 Grafana**（公司有 Grafana 的話）。送到哪、每個 dept 送給誰、夜間要不要發、靜音、重複抑制，都是 Grafana Alerting 本來就有的（contact point、notification policy、mute timing），由管 Grafana 的人設定。**我們 focus 在 AIOps**：把異常整合成「一件事」、附上根因分析，寫成 Grafana 讀得到的格式。
- **目的**：
  1. **一件事一筆**：同一個服務、時間接在一起的異常合成一件（`aiops-alerts`），帶 `dept`、`group`、嚴重度、狀態（open／resolved）、摘要、根因報告連結；Grafana 依這個 index 發通知，**一件只響一次**。
  2. 摘要欄位是**看得懂的一段話**：哪個服務、從什麼時候、哪些指標異常、根因分析的前幾名（慢在哪一段、錯從哪開始）；Grafana 的通知範本直接帶這個欄位。
  3. **先只寫 `aiops-alerts`、Grafana 不開通知**跑 2～4 週：每天會有幾件、哪些是誤報，記下基準（[feedback/README.md](../feedback/README.md) 要的「上線前先記下現況」），門檻調過才在 Grafana 打開。
- **範圍**：
  - 做：收斂最小版（[system-stages.md 第 7 節](../system-stages.md) 的 ① 時間合併已有 `status`；這次做 ② **同一個服務合成一件**）；一件存一筆 `aiops-alerts`；一件事跑一次根因分析、報告連結和摘要寫進那一筆；給 Grafana 的設定範例（ES data source、alert rule、依 `dept` 分送的 notification policy）。
  - 不做：**通知管道、分送、夜間、靜音**（Grafana 做）；③ 沿呼叫關係合併上下游（要 trace 拓撲，下一輪）；LLM 摘要；通知裡的回饋按鈕（另一份：[老師傅回饋入口](2026-10-expert-feedback.md)；這份先在通知裡放 alert id，用現有的 `feedback add`）；排程部署（先手動或 cron 跑指令）。

### 哪些異常會變成告警

| 來源 | 進告警 | 為什麼 |
|---|---|---|
| 規則（`rules.yaml`）`severity: critical` | ✅ 馬上 | 狀態變化、一定是壞了（容器重啟、副本不夠、錯誤率 ≥ 5% 保底） |
| 規則 `severity: warning` | ✅ 但不單獨發：併進同一個服務已經開著的那件；沒有開著的就等到第 2 次 | 單一 warning 常是一下子的 |
| baseline `status: anomaly`（連續 2 個時間窗） | ✅ | 已經過「連續才算」 |
| baseline `status: warning`（只跳一次） | ❌ 只記在那件事裡當參考 | 零星跳動 |
| 跟兄弟比（peer） | ✅ 併進同一個服務 | 「哪一個 Pod」是根因線索 |
| 資料品質（`quality.*`） | ✅ **送平台的地方，不送應用團隊** | 是收值的問題（資料停了、被拒收、路由爆炸），應用團隊處理不了 |

### 兩件事不要搞混：「合成一件」看服務，「送給誰」看 dept

| | 依什麼 | 為什麼 |
|---|---|---|
| **合成一件**（⑥ 收斂） | **服務** | AIOps 的分析單位是服務：同一個服務的 p95、錯誤率、Pod 互比是同一件事；**同一個 dept 的兩個不相關服務出事，是兩件**，不能因為同部門就併 |
| **送給誰**（⑧ 出口） | **dept**（對不到 dept 的用 group，再沒有就 `unassigned` → 平台） | 值班是依部門分的；一件事的 dept 取自它的異常（[anomaly-owner](2026-10-anomaly-owner.md) 已讓每筆異常帶 `dept`、`group`） |
| 同一個 dept 的多件 | 各自一則，送到同一個地方；可以加一則「這個部門目前 N 件進行中」的彙總（之後） | 讓部門值班看到全貌，但每件的根因分析、回饋仍是各自的 |

一件事跨兩個 dept（例：IT 的 mes-api 慢，是因為 IMC 的 wip-worker 慢）：第一版**各自一件、各送各的**；沿呼叫關係合併上下游是下一輪（③ 拓撲合併），合併後送給兩邊。

### 怎麼合成一件（⑥ 最小版）

- **鑰匙**：服務（`resource.attributes.service.name`；trace 序列的 `entry.service`；規則的 context）。沒有服務的（節點、PV）用 namespace，再沒有用節點名。
- **開一件**：這個服務沒有開著的件，來了一筆會進告警的異常。
- **併進去**：開著的件，最後一筆異常在 15 分鐘內（`merge_gap`）。
- **結束**：30 分鐘（`close_after`）沒有新的異常 → `resolved`，發恢復通知。
- **嚴重度**：件裡最高的那筆；升級（warning → critical）時更新通知，降級不發。

### 交給 Grafana 的分工

| 誰做 | 做什麼 |
|---|---|
| **AIOps**（我們） | 異常 → 合成一件 → `aiops-alerts`：`service`、`dept`、`group`、`severity`、`status`（open／resolved）、`summary`、`report_url`、`started_at`、`updated_at`、包含哪些異常 |
| **Grafana** | ES data source 讀 `aiops-alerts`；alert rule：`status: open` 的每一件一則（labels：`service`、`dept`、`severity`）；notification policy 依 `dept` 送到各部門的 contact point；mute timing 管夜間假日；resolved 時發恢復 |
| **管 Grafana 的人** | 設定 contact point（Teams、Email、LINE…）、哪個 dept 送哪裡、夜間規則 |

要先驗證的：Grafana 的 ES data source 支不支援我們的 ES 9.5（Grafana 版本要夠新）；alert rule 用 ES 查詢做「每一件一則」的寫法。

### 摘要長什麼樣（Grafana 通知範本帶 `summary`、`report_url`）

```
🔴 [critical] mes-api：API 錯誤率超過 5%（13%，門檻 5%）等 4 項異常
從 10:05（台北）開始，持續 15 分鐘
異常：錯誤率 13%（平常 0.4%）、p95 3.8 秒（平常 0.12 秒）、Pod mes-api-7f9 錯誤率比其他 Pod 高
根因分析：慢在 mes-api → Oracle「SELECT lots」（平常 40ms → 2.8 秒）；錯從 Oracle 那段開始
報告：<report_url>　　回饋：<回饋網址>?alert=a-20261025-mes-api-1005（[老師傅回饋入口](2026-10-expert-feedback.md)）
```

### 前置（已做）：異常總表帶 dept、group

告警要依部門分送，但 `aiops-anomalies` 原本沒記部門，只有服務、namespace。已補上（[anomaly-owner](2026-10-anomaly-owner.md)）：每筆異常有 `dept`、`group`，可以直接 `terms group` 分。

### 要先確認的

| 項目 | 為什麼 |
|---|---|
| 公司有沒有 Grafana、什麼版本、誰管 | 沒有的話才回頭自己做最小的通知出口 |
| Grafana 能不能連到中央 ES | 要開一把唯讀 key（只讀 `aiops-alerts`）給它 |

送到哪、每個 dept 送給誰、夜間發不發：**在 Grafana 設，不在這個 repo**。

### 驗收

1. 同一個服務 10 分鐘內 20 筆異常 → `aiops-alerts` 1 件。
2. 兩個不相關的服務（就算同一個 dept）同時出事 → 2 件。
3. warning 升 critical → 那一件的 `severity` 更新；30 分鐘沒新的 → `status: resolved`。
4. 資料品質的件 `dept` 空、`group: unassigned`（Grafana 依此送平台）。
5. Grafana（測試用 docker）讀 `aiops-alerts`，一件一則、resolved 時恢復；依 `dept` 分到兩個不同的 contact point。
6. 跑一天，能回答「每天會有幾件、哪幾個服務最多」。

## Do：改動哪些 code、改了什麼、為何要改

（還沒動工）

## Check：測試的目的與結果

（還沒動工）

## Act：Check 後沒改善的、還沒解決的

（還沒動工）
