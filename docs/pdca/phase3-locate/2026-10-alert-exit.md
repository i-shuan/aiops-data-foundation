# 事件整合，通知交給 Grafana（2026-10）

**狀態**：已完成（k3s e2e、正式 Grafana 待接）　　**日期**：2026-10-25　　**相關**：Plan 在分支 `claude/alert-exit`；實作在 `claude/alerts`
**模組**：⑥ 收斂（最小版）；⑧ 出口交給 Grafana

## Plan：這一次要達到什麼

- **需求**：④ 判斷的結果（baseline、規則、Pod 互比、trace 每一段、資料品質）只寫進 `aiops-anomalies`；值班不會去查 ES，判斷沒有人看得到。
- 每筆異常直接送出會洗版：一次 DB 變慢，30 分鐘裡可能上百筆（[architecture.md「收斂」](../../architecture-reference.md#8-收斂很多筆怎麼變成一件)），值班會直接關掉通知。
- **決定（2026-10-25）**：通知不自己做，交給 Grafana（公司有 Grafana 的話）。
- 送到哪、每個 dept 送給誰、夜間、靜音、重複抑制：用 Grafana Alerting 的 contact point、notification policy、mute timing，由管 Grafana 的人設定。
- AIOps 只做：把異常整合成「一件事」、附上根因分析，寫成 Grafana 讀得到的格式。
- **目的**：
  1. **一件事一筆**（`aiops-alerts`）：同一個服務、時間接在一起的異常合成一件；Grafana 依這個 index 發通知，**一件只響一次**。
  2. 摘要欄位是**看得懂的一段話**；Grafana 的通知範本直接帶這個欄位。
  3. **先只寫 `aiops-alerts`、Grafana 不開通知**跑 2～4 週：記下每天幾件、哪些是誤報（[feedback/README.md](../../feedback/README.md) 要的「上線前先記下現況」），門檻調過才打開。
- **範圍**：
  - 做：收斂最小版的 ② **同一個服務合成一件**（[architecture.md「收斂」](../../architecture-reference.md#8-收斂很多筆怎麼變成一件) 的 ① 時間合併已有 `status`）。
  - 做：一件存一筆 `aiops-alerts`；一件跑一次根因分析，報告連結和摘要寫進那一筆。
  - 做：Grafana 的設定範例（ES data source、alert rule、依 `dept` 分送的 notification policy）。
  - 不做：通知管道、分送、夜間、靜音（Grafana 做）。
  - 不做：③ 沿呼叫關係合併上下游（要 trace 拓撲，下一輪）；LLM 摘要；排程部署（先手動或 cron 跑指令）。
  - 不做：通知裡的回饋按鈕（另一份：[老師傅回饋入口](../phase5-learning-loop/2026-10-expert-feedback.md)）；這份先在通知裡放 alert id，用現有的 `feedback add`。

### 哪些異常會變成告警

| 來源 | 進告警 | 為什麼 |
|---|---|---|
| 規則（`rules.yaml`）`severity: critical` | ✅ 馬上 | 一定是壞了（容器重啟、副本不夠、錯誤率 ≥ 5% 保底） |
| 規則 `severity: warning` | ✅ 併進開著的那件；沒有開著的，第 2 次才開 | 單一 warning 常是一下子的 |
| baseline `status: anomaly`（連續 2 個時間窗） | ✅ | 已經過「連續才算」 |
| baseline `status: warning`（只跳一次） | ❌ 只記在那件事裡當參考 | 零星跳動 |
| Pod 同儕橫向比對 (Peer Comparison)（peer） | ✅ 併進同一個服務 | 「哪一個 Pod」是根因線索 |
| 資料品質（`quality.*`） | ✅ **送平台，不送應用團隊** | 收值的問題（資料停了、被拒收、路由爆炸），應用團隊處理不了 |

### 「合成一件」看服務，「送給誰」看 dept

| 問題 | 依什麼 | 為什麼 |
|---|---|---|
| 合成一件（⑥ 收斂） | **服務** | 同一個服務的 p95、錯誤率、Pod 互比是同一件事 |
| 同一個 dept 的兩個不相關服務 | **兩件** | 不因為同部門就併 |
| 送給誰（⑧ 出口） | **dept** → group → `unassigned`（平台） | 值班依部門分 |
| 一件事的 dept 從哪來 | 它的異常 | [anomaly-owner](../phase2-anomaly/2026-10-anomaly-owner.md) 讓每筆異常帶 `dept`、`group` |
| 同一個 dept 的多件 | 各自一則，送到同一個地方 | 每件的根因分析、回饋各自獨立 |

- 同一個 dept 可再加一則「這個部門目前 N 件進行中」的彙總（之後）。
- 一件事跨兩個 dept（例：IT 的 mes-api 慢，是因為 IMC 的 wip-worker 慢）：第一版各自一件、各送各的。
- 沿呼叫關係合併上下游是下一輪（③ 拓撲合併），合併後送給兩邊。

### 怎麼合成一件（⑥ 最小版）

- **鑰匙**：服務（`resource.attributes.service.name`；trace 序列的 `entry.service`；規則的 context）。沒有服務的（節點、PV）用 namespace，再沒有用節點名。
- **開一件**：這個服務沒有開著的件，來了一筆會進告警的異常。
- **併進去**：開著的件，最後一筆異常在 30 分鐘內（`close_after`）。
- 只用 `close_after` 一個參數，不另設 `merge_gap`：兩個參數時，中間那段要併還是開新的說不清。
- **結束**：30 分鐘（`close_after`）沒有新的異常 → `resolved`，Grafana 發恢復通知。
- **嚴重度**：件裡最高的那筆；升級（warning → critical）時更新通知，降級不發。

### 交給 Grafana 的分工

| 誰做 | 做什麼 |
|---|---|
| **AIOps**（我們） | 異常 → 合成一件 → 寫 `aiops-alerts`（欄位見下） |
| **Grafana** | ES data source 讀 `aiops-alerts` |
| **Grafana** | alert rule：`status: open` 每件一則（labels：`service`、`dept`、`severity`） |
| **Grafana** | notification policy 依 `dept` 送到各部門的 contact point |
| **Grafana** | mute timing 管夜間假日；resolved 時發恢復 |
| **管 Grafana 的人** | contact point（Teams、Email、LINE…）、哪個 dept 送哪裡、夜間規則 |

- `aiops-alerts` 的欄位：`service`、`dept`、`group`、`severity`、`status`（open／resolved）、`summary`、`report_url`、`started_at`、`updated_at`、包含哪些異常。
- Grafana 12.2.0 的 ES data source 讀得到、能做「每一件一則」（見 Check）。

### 摘要長什麼樣（Grafana 通知範本帶 `summary`、`report_url`）

```
🔴 [critical] mes-api：API 錯誤率超過 5%（13%，門檻 5%）等 4 項異常
從 10:05（台北）開始，持續 15 分鐘
異常：錯誤率 13%（平常 0.4%）、p95 3.8 秒（平常 0.12 秒）、Pod mes-api-7f9 錯誤率比其他 Pod 高
根因分析：慢在 mes-api → Oracle「SELECT lots」（平常 40ms → 2.8 秒）；錯從 Oracle 那段開始
報告：<report_url>　　回饋：<回饋網址>?alert=a-20261025-mes-api-1005（[老師傅回饋入口](../phase5-learning-loop/2026-10-expert-feedback.md)）
```

### 前置（已做）：異常總表帶 dept、group

- 每筆異常有 `dept`、`group`，可以直接 `terms group` 分（[anomaly-owner](../phase2-anomaly/2026-10-anomaly-owner.md)）。

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

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/investigator/alerts.py`（新） | `entity_of`：服務 → namespace → 節點；資料品質另一類 | 合成一件的鑰匙 |
| 同上 | `kind_of`：每筆異常分 open／weak／attach | 對應「哪些異常會變成告警」 |
| 同上 | `merge`：併進開著的、開新的、30 分鐘沒新的就結束 | ⑥ 最小版 |
| 同上 | `merge` 的 `done_ids` | 重跑時已結束的件不會被重開 |
| 同上 | `merge`：早於開始時間 `close_after` 以上的異常不併 | 不然開始時間被拉早、筆數變多 |
| 同上 | `owner`：dept、group 取異常裡最多的；資料品質一律 unassigned | 送給誰 |
| 同上 | `headline`／`summary`：一行標題＋開始時間、持續多久、哪些指標、根因結論 | 通知範本帶 `summary` |
| `engine/investigator/cmd_alert.py`（新） | `alert` 指令：讀開著的件、最近結束的件、最近 2 小時的異常 → `merge` | — |
| 同上 | 新的那件跑一次 `investigate` → 寫 `aiops-alerts` | 一件一份根因報告 |
| 同上 | 開著的件每次都更新 `@timestamp`（心跳） | Grafana 查「最近 5 分鐘有心跳、status open」就知道哪些件還在 |
| `engine/investigator/__main__.py` | 加 `alert` 子指令 | — |
| `engine/configs/rca/rca.yaml` | `alerts:`：`close_after`、`lookback`、`rca`、`report_url` | `report_url` 等回饋網頁做好再填 |
| `engine/configs/grafana/datasource.yaml`（新） | ES，只讀 `aiops-alerts` | — |
| `engine/configs/grafana/alert-rules.yaml`（新） | 每一件一則；labels 帶 `alert_id`、`service`、`dept`、`group`、`severity`、`summary` | — |
| `engine/configs/grafana/contact-points.yaml`（新） | 範例 webhook | 管 Grafana 的人照著改 |
| `engine/configs/grafana/policies.yaml`（新） | 依 dept 分送；沒有 dept 送平台；`group_by: [alert_id]` | Grafana 不再合併 |
| `engine/configs/grafana/README.md`（新） | 怎麼用 | 管 Grafana 的人照著改 contact point 和 dept 清單 |
| `telemetry/scripts/elasticsearch-setup.sh` | 多發一把「Grafana 唯讀 key」：只讀 `aiops-alerts`＋ cluster `monitor` | Grafana 的 ES data source 要先打 `GET /` 看版本 |
| `engine/investigator/es.py`、`analyze.py` | 拿掉沒在用的 `get_field` import；`noqa` 註解格式 | ruff |
| `engine/tests/test_alerts.py`（新） | 16 個測試 | 見 Check |

- `alerts.py` 放在 investigator：一件事要跑根因分析，也只有它要讀 `aiops-anomalies` 全部；不讓 detector 去 import investigator。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 同服務合一件、不同服務分開 | 單元測試 | 同服務 10 分鐘 20 筆 → 1 件；同 dept 兩個服務 → 2 件 |
| 哪些異常會開件 | 單元測試 | 只跳一次的 warning 不開、併得進開著的；weak 一筆不開、兩筆開 |
| critical 與升級 | 單元測試 | critical 規則馬上開；warning 升 critical 有標記 |
| 重跑與結束 | 單元測試 | 重跑不重複；30 分鐘沒新的結束；結束後再出事是新的一件 |
| 資料品質 | 單元測試 | 另一件、送平台 |
| 在 ES 上實跑：開件 | es-t 跑 `investigator alert`（資料見下） | mes-api、wip-worker 各一件（`D3120／it`、`D1110／imc`） |
| 舊異常不重開 | 同上 | 之前留下的舊異常記成「已經過去的」，不新開 |
| 重跑不重複 | 同上，再跑一次 | 新開 0 |
| 根因分析接得上 | 同上，看 `report` | 有跑 `investigate`、報告存進去 |
| 沒有 trace 時 | 同上 | 報告寫「沒有 trace，只能從 log 找」；摘要只有標題兩行 |
| 有結論時摘要加第三行 | 單元測試 | 有 |
| Grafana 依 dept 分送 | docker Grafana（設定見下） | mes-api → `/it`、wip-worker → `/imc` |
| 資料品質送平台 | 同上 | 被拒收那件 → `/platform`（`dept: none`） |
| 通知帶的內容 | 同上 | labels 帶 `alert_id`、`service`、`dept`、`severity`；annotation 帶摘要 |
| 件結束時 Grafana 發恢復 | `close_after` 改短，停掉假資料 | `/it`、`/imc`、`/platform` 都收到 `resolved` |
| Grafana 唯讀 key 的權限 | 用 key 打 ES | 讀 `aiops-alerts` 200、讀 `aiops-anomalies` 403 |

- es-t 的資料：Collector 送兩組假 Java（mes-api／D3120、wip-worker／D1110），跑保底規則。
- docker 的 Grafana：12.2.0，掛 `engine/configs/grafana/` 的四個 provisioning 檔；contact point 指到一個記錄收到什麼的 webhook。
- 見 troubleshooting：Grafana 規則 Error、ES 回 400（terms 要寫 `orderBy: _count`）；規則 Error 時 Grafana 會發一則「規則錯誤」到預設的平台頻道。
- 見 troubleshooting：data source 測試回 403（key 要有 cluster `monitor`）。

**沒測到**：k3s e2e；有 trace 時摘要帶根因分析結論（只有單元測試）；正式的 Grafana（版本、權限、contact point）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `investigator alert` 停了，Grafana 會以為所有件都恢復了 | 設計時：心跳停了查不到 | 值班收到一堆「恢復」 | 加「`aiops-alerts` 10 分鐘沒寫入」的告警送平台，隨排程上線 |
| 跨 dept 的上下游還是各自一件 | 範圍 | IT、IMC 各收一則，看不出是同一件事 | 下一輪：沿呼叫關係合併（③） |
| 摘要放在 label 裡 | Grafana 只能從 terms 分組拿值、用 labels 帶文字 | 摘要很長時 label 也很長 | 夠用；`summary` 欄位 `ignore_above: 4096` |
| 嚴重度只有 warning／critical | 實作時：baseline 的異常都算 warning | baseline 抓到再嚴重也只是 warning | 試跑 2～4 週看分佈，再決定要不要依 z 分級 |
| 每件只在新開時跑一次根因分析 | 實作時 | 後面才出現的線索不會進報告 | 之後可在嚴重度升級時重跑 |
| 資料品質的異常時間是 10 分鐘時段的開頭 | 測試把 `close_after` 改成 3 分鐘時，那件沒開 | `close_after` 太短時一進來就算「已經過去」 | 正式的 30 分鐘沒有這個問題 |

- **決定**：合成一件看服務、送給誰看 dept；通知完全交給 Grafana，`group_by: [alert_id]`，Grafana 不再合併。
- **下一輪**：排程上線（CronJob：rules、detect、quality、alert）＋「alert 停了」的告警；老師傅回饋入口（通知裡的連結）。
