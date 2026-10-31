# 尚未決定的設計問題（2026-10）

**狀態**：未立案，討論中　　**日期**：2026-10-31　　**相關**：分支 `claude/open-issues`
**模組**：③ 存、④ 判斷、⑥ 收斂、⑦ 根因分析、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：討論偵測時機、快速通道、回饋調整、根因分析時，累積了一批沒有定論的問題，散在對話裡。
- **目的**：集中列出，每項記現況、爭議點、目前傾向。之後逐項決定，決定的再各自立案。
- **範圍**：只列問題。沒有改程式與設定，也沒有立任何案。

## Do：改動哪些 code、改了什麼、為何要改

無 code 改動。這份只整理討論。標「推算」的是讀設定與程式推出來的，沒有量測。

## Check：測試的目的與結果

無。以下數字與行為，除註明外，都來自 [schedule.yaml](../../../aiops-engine/code/configs/schedule/schedule.yaml)、[series.yaml](../../../aiops-engine/code/configs/baseline/series.yaml)、[alerts.py](../../../aiops-engine/code/investigator/alerts.py)、[tuning.py](../../../aiops-engine/code/detector/tuning.py)、[rca/README.md](../../rca/README.md)。

## Act：沒解決的，在什麼情況下發現的

### 1. trace 合併（討論 trace 量大時）

目的：一個請求產生 5～8 筆 span，筆數多、固定開銷大，想把同一個 `trace_id` 的 span 合成一筆摘要。

| 問題 | 選項 | 傾向 |
|---|---|---|
| 在哪裡合 | trace-sampler：取樣後立刻寫摘要，不增加延遲，要自己寫元件。ES transform：不用寫程式，但完整 span 要先寫進 ES，省的只有保存容量 | transform，先不動 Collector |
| 合併後取代 span 還是多存一份 | 取代：容量省最多，但根因分析現在讀 span。多存：查詢方便，容量反而多一點 | 有問題的 trace 保留完整 span，正常的只存摘要 |
| 隨機 5% 要不要取消 | 摘要很小，全部正常請求都存一筆比抽 5% 準 | 未決定 |
| 工廠端是什麼意思 | 工廠現場另一套小 ES／機台或 EAP 另一種來源／同一套設計搬到工廠環境 | 未回答，決定寫入量與保存容量哪個是瓶頸 |
| 晚到的 span | 超過取樣等待的 30 秒才到，摘要可能少一段；transform 會不會補進去 | 未驗證 |
| 摘要延遲 | transform 約 5 分鐘（`aiops-log-stats` 現況，推算）；出問題的 trace 不受影響，仍是完整 span | 可接受 |

### 2. 偵測的時間

| 問題 | 現況 | 傾向 |
|---|---|---|
| `detect` 每分鐘跑、窗口 10 分鐘 | 停機超過 10 分鐘的那段不補判，Collector 補送的舊資料也不判，只進 `train` | 頻率與窗口不改；把這個限制寫進文件 |
| peer、trace 每 5 分鐘 | trace 異常約 5～10 分鐘才出現（推算），metrics 約 1～3 分鐘（推算）。trace 的統計本來就以 5 分鐘為單位 | 可接受；只有 trace 看得到的緩慢變差會晚發現 |
| 實際延遲沒量過 | 上面的時間都是推算 | 上線前量一次 |
| Grafana 評估間隔 | `alert-rules.yaml` 的 `interval: 1m`，再加進 1～3 分鐘（推算）；已寫進 [grafana/README.md](../../../grafana/README.md) | 已確認 |
| 30 秒收一點 | 10 秒的短暫尖峰會被平均掉 | 目前不處理 |

### 3. 一次就很嚴重的

| 問題 | 現況 |
|---|---|
| p95 沒有單點即判定 | `instant_z: null`（p95 取自直方圖桶中點，跨一格 z 約 23，開了每個超標點都會直接判 anomaly）。延遲暴衝只靠靜態規則的「p95 超過 2 秒」 |
| 靜態規則有等待 | 部分規則設 `for: 1m～5m`（例如副本不足 3 分鐘、拉 image 5 分鐘），為了避免換版時誤報。「一次就嚴重」要看是哪條規則 |
| 快速通道 | 錯誤率 `confirm_points: 1`、暴衝 `instant_z: 15`、規則 critical，都直接開事故，Grafana 規則 `for: 0s` 不再等 |
| 上線初期不通知 | 前 2～4 週 Grafana 不接 contact point，快速通道的事故只出現在儀表板 |

### 4. 一直壞著的異常

| 問題 | 現況 |
|---|---|
| 整個服務長期不好、又沒到門檻 | 例如錯誤率一直 3%：基準線學進平常，同儕比對抓不到（整個服務一起壞），靜態規則 5% 沒到。只有 log 有明確 `error.type` 的會進慢性病清單 |
| 慢性病清單每週一次 | 用途是排修理優先順序，不是發現問題 |
| 第二階段才開 | `peer`、`chronic` 在第二階段；現在上線只有靜態規則擋這類 |
| 「靜態的異常」的定義 | 我的理解是「一直壞著不變」，還沒跟提問的人確認 |

### 5. 回饋與調整

| 問題 | 現況 | 傾向 |
|---|---|---|
| 靜態規則（5%、2 秒）回饋不能調 | 改 `rules.yaml`；回饋只能新增 `rule_suggestion` | 未決定 |
| `exclude_window` 只影響基準線 | 加 `--apply-to train,detect` 也只讓基準線這邊不報，擋不住靜態規則 | 未決定 |
| override 存放 | `tune` 或回饋網頁寫進 `aiops-tuning`（有原因、誰、何時）；改 `series.yaml` 只存檔案。`series` 本身不搬進 DB（失去 git 審查與回滾） | 結構放檔案，調整放 DB |
| 長期有效的 override 沒有「畢業」 | 不會升級成 `series.yaml` 的預設值；`aiops-tuning` 沒有備份機制，ES 重建就消失 | 半自動：生效超過 N 天，產生 PR 草稿，人審後合併再刪 DB 那筆 |
| 過期的調整不會清 | `expires_at` 過期後不再生效，但文件留著 | 未決定 |
| 看不到最終生效值 | 同時有多筆符合時（後面蓋前面），沒有指令查「這個服務現在用的 z 是多少、來自哪一筆」 | 想補 |
| `match` 是正規表示式 | 寫太寬會誤傷其他服務；套用順序是自動選 → `overrides` → `adjustments.yaml` → `aiops-tuning`（依 `at` 由舊到新），不看 `match` 多細，較晚寫的寬鬆調整會蓋掉較早的精細調整（[reference.md](../../../aiops-engine/code/configs/baseline/reference.md#使用者feedback-調整)） | 調整前用 `--list`、`backtest`；是否改成「精細的優先」未決定 |

### 6. 根因分析的限制

| 限制 | 說明 |
|---|---|
| 多個服務共用同一個資料庫 | 每件事故各算各的，看不出同源；跨服務合併未做 |
| metrics → trace 是統計對應 | metrics 沒有 `trace_id`，樣本不保證是造成異常的那個請求 |
| 每段取平均 | 少數極慢請求會拉高排名 |
| 比 1 秒快的變慢 | 200ms 變 800ms 只剩隨機 5%；實測 1,586 個請求剩 88 個 |
| 路由沒參數化 | 撈到 0 筆 |
| 沒做 | 趨勢（慢慢惡化）、指標之間的關係、慢請求的共同點（Pod、版本、批號、SQL）、事故紀錄格式、log 樣式字典、處理手冊 |
| 沒在正式環境跑過 | 驗證都在測試環境 |

問題 1 的決定會牽動這一節：根因分析現在讀 span，若合併後取代 span，這裡要一起改。

### 7. 基準線與調整索引掉了

| 問題 | 現況 | 傾向 |
|---|---|---|
| `aiops-baseline` 掉了 | `detect` 讀到空的，印一行「沒有 baseline，先跑 train」就跳過那條序列，不報錯。動態基準線這一路靜默停擺；規則、Pod 同儕橫向比對、資料品質不受影響 | 手動跑一次 `train`（[aiops-engine/README.md](../../../aiops-engine/README.md)）；不手動要等隔天 02:00 |
| 沒有「基準線不見」的告警 | Grafana 的 `aiops-heartbeat` 只看 `investigator alert` 有沒有寫入，這種情況不會響 | 加一條：`detect` 讀到空基準線的序列數大於 0 就送平台 |
| 沒有 snapshot 備份 | `elasticsearch/code` 找不到 snapshot 或 SLM 設定；索引 `auto_expand_replicas: 0-1`，單節點沒有副本 | `aiops-tuning` 優先加（老師傅的調整，掉了不能重算）；`aiops-baseline` 是每天重寫的衍生資料，重要性較低 |
| 重建 ES 的流程 | 沒有把 `train` 列為標準步驟 | 列進去 |
| `aiops-baseline` 實際大小 | 沒查到數字（文件只有訓練時 1 萬條序列、2～3GB 記憶體） | 上線前量 |

### 8. 文件

| 項目 | 現況 |
|---|---|
| retention 內容搬進 log、metrics、trace | 數字照原文搬，沒有重新對程式核對 |
| 保留天數寫兩處 | 各 README 的「多久壓縮」與 [retention-reference.md](../../retention-reference.md) 同一批天數；天數的來源是 `retention.conf` |
