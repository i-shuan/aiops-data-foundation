# 尚未決定的設計問題（2026-10）

**狀態**：未立案，討論中　　**日期**：2026-10-31　　**相關**：已合入 master（分支已刪）
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

### 1. trace 合併

**狀態：已合入 master**（PR #36、#37，2026-11-01；設計與實測見 [PDCA](../phase1-collect/2026-10-trace-merge.md)，ES 兩個資料節點加只投票節點見 [es-two-pods](../phase1-collect/2026-11-es-two-pods.md)）。[trace/README.md](../../trace/README.md)、[retention.md](../../retention.md) 已改成 100% 收、一個請求合成一筆。

目的：製造業事故檢討不能少資料，不在收值端取樣；一個請求原本是 5～8 筆 span，希望查一個請求只讀一筆。

討論時的選項與決定：

| 問題 | 決定 |
|---|---|
| 在哪裡合 | 不在收值端、不用 ES transform（`scripted_metric` 組 `nested` 難寫難測；continuous transform 寫不進 data stream）；由 engine 的排程器每分鐘跑 `detector trace-merge`，以 `event.ingested` 找這一分鐘有新 span 的 trace，讀回全部的段，合成一筆 upsert（`_id` = `trace_id`） |
| 取代 span 還是多存一份 | 都留：raw（OTel 原生、100% 收）只留 2 天，merged（`traces-merged-*`，一請求一筆、每段放 `nested`）留 7 天；根因分析讀 merged，trace 每一段的 baseline 仍讀 raw |
| 隨機 5% 要不要取消 | 取消尾端取樣，100% 收；拆掉 trace-sampler 與 `traces-problem` |
| 晚到的 span | 每分鐘重讀有新 span 的 trace 全部的段，晚到的會補進去；停很久會分段補（最多 2 天） |
| 摘要延遲 | 晚 1～2 分鐘：排程器每分鐘跑，根因分析看到的 merged 是 1～2 分鐘前的；事故當下要看即時的查 raw（2 天內） |
| 工廠端是什麼意思 | 未回答 |

合入時還沒解決的（來源：[PDCA](../phase1-collect/2026-10-trace-merge.md) 的 Act）：

| 項目 | 現況 |
|---|---|
| 容量 | 100% 收；文件的估算：低流量服務 7 天 1.2GB 變 12GB、高流量 39GB 變 387GB。`nested` 不會省空間，省的只有文件固定開銷；raw 多留 2 天，這 2 天是雙份。收值不省是使用者的決定，之後靠保留天數與冷儲存（MinIO，另案）處理 |
| 升級那一刻漏合 | setup 把舊 raw 資料流 rollover 之前寫的 span 沒有索引的 `event.ingested`，`trace-merge` 看不到；實測約 9 個 trace，只影響第一次升級 |
| merged 不是 OTel 原生格式 | 自訂格式；Kibana 只有 raw 那 2 天看得到原生 |
| 讀端兩套 | 根因分析讀 merged，trace baseline、資料品質讀 raw（只看最近 10 分鐘）；`aiops-trace-segments` 要改讀 merged 得把 composite 聚合改成 nested，這次不碰 |
| 實測環境 | Rancher Desktop 單節點、ES 8.19.23：`run-trace-e2e.sh` 7b 的 raw 21 個 trace 對 merged 21 筆、重跑不重複、權限正確；`run-rca-e2e.sh` 11 項過；單元測試 377 個過。排程器（含 `trace-merge` 每分鐘）24 項、`loop_e2e` 43 項、⑧⑨ 實跑 11 項、metrics-trace 15 項、keys 84 項、trace-id 15 項、kafka 9 項、peer-edge 6 項都過。正式環境沒跑過 |
| ES 全部資料節點停機會掉資料（**已知風險，決定不處理**） | 測試 ES 改成兩個資料節點加一個只投票節點後（[PDCA](../phase1-collect/2026-11-es-two-pods.md)）：卡住任何一個資料節點，log、trace 都 0 遺失；兩個資料節點都停 180 秒仍掉（log 7,580／71,988 = 10.5%、trace 993／7,199 = 13.8%、metrics 空 209 秒），遺失集中在停機後 0～30 秒與 50～92 秒，其餘時間沒掉。單 Pod 時 trace 掉 35%（master 是 0，因為 trace 以前經 sampler 無限重試）。原因：Collector 的 ES 元件遇到連線逾時不重試；一直重試的 `retry`、`timeout: 0`、磁碟佇列早就開了，缺的只有這一種錯誤。log 遺失 master 也有（9,649 對 7,715）。**決定：不處理，記成已知風險——掛任何一個資料節點都安全，全停才會掉。**沒查全停時掉資料的具體錯誤種類；正式環境若 ES 前面有 LB 加健康檢查，連線黑洞會變成 503、Collector 會重試，風險可能更小（沒實測）；前案 [collector-es-exporter](2026-10-collector-es-exporter.md) 記過同一個限制 |
| `trace_quality` 測試 6 項失敗 | 已解決：3 項在測已廢止的隨機取 5%，已刪；抽樣太少的 2 項在每點抽 100 個後都過，B6a 由 `missing_if_equals` 解掉。整支 38 項全過（[PDCA](../phase2-anomaly/2026-11-trace-sample-100.md)） |
| B8（unassigned 的 log 比例）單獨跑沒過 | 已重跑：資料品質那段單獨跑 21 項全過，B8 兩項都報了。之前失敗時測試 ES 剛跑完 durability、留著大量其他 log；現在過了，與「分母被墊大」的推測一致，但沒有直接證明（[PDCA](../phase2-anomaly/2026-11-trace-sample-100.md)） |

### 2. 偵測的時間

**狀態：已測**（事故到 Grafana 通知的延遲、p95 暴衝都已量，見下表與第 3 節）。

| 問題 | 現況 | 傾向 |
|---|---|---|
| `detect` 每分鐘跑、窗口 10 分鐘 | 停機超過 10 分鐘的那段不補判，Collector 補送的舊資料也不判，只進 `train` | 頻率與窗口不改；限制已寫進 [aiops-engine/README.md](../../../aiops-engine/README.md) 排程表（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） |
| peer、trace 每 5 分鐘 | trace 異常約 5～10 分鐘才出現（推算），metrics 約 1～3 分鐘（推算）。trace 的統計本來就以 5 分鐘為單位。100% 收已拆掉尾端取樣的 30 秒等待、加了每分鐘的 `trace-merge`，但 trace 的偵測仍是 5 分鐘一點，這個推算合入後沒重新量過 | 可接受；只有 trace 看得到的緩慢變差會晚發現 |
| 實際延遲沒量過 | 上面的時間大多是推算；p95 暴衝到開事故已實測約 70 秒（見第 3 節）。⑧⑨ 實跑（Collector → trace-merge → rules → alert → Grafana → notify-sink，每分鐘一輪 tick 模擬排程器，誤差約 ±1 分鐘）：事故開始後 159 秒有第一筆異常、162 秒開件、**186 秒 Grafana 通知到**。測試環境，不是正式環境 | 上線前在正式環境量一次 |
| Grafana 評估間隔 | `alert-rules.yaml` 的 `interval: 1m`，再加進 1～3 分鐘（推算）；已寫進 [grafana/README.md](../../../grafana/README.md) | 已確認 |
| 30 秒收一點 | 10 秒的短暫尖峰會被平均掉 | 目前不處理 |

### 3. 一次就很嚴重的

**狀態：已測**。p95 暴衝的實測已做（已合入 master，PDCA 見 [detect-latency](../phase3-locate/2026-10-detect-latency.md)）；事故到 Grafana 通知的延遲已用 ⑧⑨ 實跑腳本量過：事故開始後 162 秒開件、186 秒通知到（測試環境）。

| 問題 | 現況 |
|---|---|
| p95 沒有單點即判定 | `instant_z: null`（p95 取自直方圖桶中點，跨一格 z 約 23，開了每個超標點都會直接判 anomaly）。延遲暴衝只靠靜態規則的「p95 超過 2 秒」 實測（真的 ES、`detector rules` 每分鐘一輪）：一個 30 秒點多 4 個 3 秒的請求（5 分鐘窗內約 9% 超過 2 秒），第一輪規則就命中、連續 5 輪命中，**第 2 輪（暴衝後約 70 秒）開出事故**——規則看最近 5 分鐘（`lookback: 5m`），一個暴衝點被連續多輪看到，湊滿 weak 要的 2 筆；只多 1 個慢請求（約 2.4%）9 輪都不命中，不開件。先前「單點暴衝可能不開事故」的推論是錯的，頻率與窗口不用改。仍成立的限制：5 分鐘內請求不到 20 個的 API 規則不判（`min_events: 20`）、慢請求不到 5% 不報。實際延遲還要再加排程器與 `alert` 各一輪，多 0～2 分鐘 |
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
| 看不到最終生效值 | 同時有多筆符合時（後面蓋前面），沒有指令查「這個服務現在用的 z 是多少、來自哪一筆」 | 已做：`tune --show --series … --match service=…` 印每個參數的生效值與來源（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） |
| `match` 是正規表示式 | 寫太寬會誤傷其他服務；套用順序是自動選 → `overrides` → `adjustments.yaml` → `aiops-tuning`（依 `at` 由舊到新），不看 `match` 多細，較晚寫的寬鬆調整會蓋掉較早的精細調整（[reference.md](../../../aiops-engine/code/configs/baseline/reference.md#使用者feedback-調整)） | 調整前用 `--list`、`backtest`；是否改成「精細的優先」未決定 |

### 6. 根因分析的限制

| 限制 | 說明 |
|---|---|
| 多個服務共用同一個資料庫 | 每件事故各算各的，看不出同源；跨服務合併未做 |
| metrics → trace 是統計對應 | metrics 沒有 `trace_id`，樣本不保證是造成異常的那個請求 |
| 每段取平均 | 少數極慢請求會拉高排名 |
| 比 1 秒快的變慢 | 以前 200ms 變 800ms 只剩隨機 5%（實測 1,586 個請求剩 88 個）。2026-11-01 起 100% 收、不取樣，根因分析讀 `traces-merged-*`，不再有這個限制；trace 每一段的偵測已改成每點抽 100 個請求（`sample_per_bucket`），這個案例沒重測，見 9.2 的抽樣一列 |
| 路由沒參數化 | 撈到 0 筆 |
| 沒做 | 趨勢（慢慢惡化）、指標之間的關係、慢請求的共同點（Pod、版本、批號、SQL）、事故紀錄格式、log 樣式字典、處理手冊 |
| 沒在正式環境跑過 | 驗證都在測試環境 |

問題 1 已合入 master，根因分析改讀 `traces-merged-*`。對照後：「取樣」那條已改（上表）；「每段取平均」「metrics → trace 是統計對應」「路由沒參數化」不受影響，仍成立；merged 晚 1～2 分鐘，事故當下要看即時的查 raw（2 天內）。

### 7. 基準線與調整索引掉了

| 問題 | 現況 | 傾向 |
|---|---|---|
| `aiops-baseline` 掉了 | `detect` 讀到空的，印一行「沒有 baseline，先跑 train」就跳過那條序列，不報錯。動態基準線這一路靜默停擺；規則、Pod 同儕橫向比對、資料品質不受影響 | 手動跑一次 `train`（[aiops-engine/README.md](../../../aiops-engine/README.md)）；不手動要等隔天 02:00 |
| 沒有「基準線不見」的告警 | Grafana 的 `aiops-heartbeat` 只看 `investigator alert` 有沒有寫入，這種情況不會響 | 已做：`detect` 每輪寫 `detect-status`（`missing_baseline: N`）進 `aiops-alerts`，Grafana 規則 `aiops-baseline-missing` N > 0 持續 5 分鐘送平台；Grafana 上沒實跑（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） |
| 沒有 snapshot 備份 | `elasticsearch/code` 找不到 snapshot 或 SLM 設定；索引 `auto_expand_replicas: 0-1`，單節點沒有副本 | 人工判斷的三個索引優先加：`aiops-incidents`、`aiops-feedback`、`aiops-tuning`，掉了不能重算；`aiops-baseline` 是每天重寫的衍生資料，重要性較低 |
| 回饋存在哪 | 和 log、metrics、trace 在同一個中央 ES，是 `aiops-*` 一般索引；AIOps 的 key 只能寫 `aiops-*`；回饋網頁本身不存資料。正式環境的磁碟大小由管 ES 的人決定，repo 內附的 chart 是測試用（`storage: 10Gi`） | 待和管 ES 的人確認 |
| 測試與正式環境的回饋不互通 | 兩邊是不同叢集，測試時累積的回饋、事故紀錄不會自動帶到正式環境 | 上線前決定要不要匯入 |
| 重建 ES 的流程 | 沒有把 `train` 列為標準步驟 | 已寫進 [aiops-engine/README.md](../../../aiops-engine/README.md)「重建 ES、換 ES 之後」（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） |
| `aiops-baseline` 實際大小 | 沒查到數字（文件只有訓練時 1 萬條序列、2～3GB 記憶體） | 上線前量 |

### 8. 相似事故檢索往哪裡走（指紋、向量、graph RAG）

| 階段 | 解決什麼 | 現況 | 什麼時候評估 |
|---|---|---|---|
| 指紋＋欄位比對（已經是 RAG 的檢索，只是不用向量） | 找同一段變慢、同一個錯誤源頭的過去事故，看得出為什麼像 | ✅ 模擬資料驗過；權重與門檻是估的 | — |
| 向量檢索 | 欄位對不上但意思一樣；檢索手冊、舊事故單、老師傅的自由文字（`root_cause` 程式現在不比對文字） | ❌ 沒做 | 事故累積到數百件、同分的變多；或開始匯入手冊、舊事故單（匯入格式還沒寫）；公司確定能用 embedding 模型（模型與能否連外未定） |
| graph RAG | 多個服務共用同一個依賴（例如同一個 Oracle）時看出同源 | ⚪ 構想（[rca/README.md](../../rca/README.md)） | 先加第二個呼叫同一 Oracle 的服務，現行報告若已指出同源就不做 |

- 三者解決不同問題，不是必須依序往上：向量不是 graph RAG 的前提，圖也不取代指紋。
- 觸發的觀察指標（真實資料跑 2～4 週才看得到）：`similar-eval` 的 recall@3；老師傅回報「附的相似事故不像」的比例。模擬資料現在是 3/3，真實資料沒測過。
- 「什麼時候會用到向量 RAG」沒有數據能算機率，這裡只記觸發條件，不預設要做。

### 9. 散在其他文件的未解項目

上面 1～8 是討論中累積的；這一節是其他文件已經記了、沒有收進來的。來源是關鍵字掃出來的，不保證完整，也沒逐項核對現況（有些可能已被後來的改動解決）。

#### 9.1 會影響上線

| 項目 | 現況 | 來源 |
|---|---|---|
| ES 停 180 秒、中途重啟兩個 Collector，metrics 整段 0 筆 | 佇列裡的資料在 Collector 重啟時遺失；根因沒查 | [e2e-es8-rerun](2026-10-e2e-es8-rerun.md) |
| 部署重整後五支 e2e 腳本沒在 cluster 上跑 | 它們的 helm 指令沒驗過 | [restructure-components](2026-10-restructure-components.md) |
| `run-metrics-scenarios.sh label` 修好後沒重跑；測試之間互相干擾（`rules` 抓到 rca 留下的服務） | 單獨跑會過，連著跑會誤判 | [e2e-es8-rerun](2026-10-e2e-es8-rerun.md) |
| ES chart 沒帶 setup Job；設定靠 `--set-file`，用 CD 工具要另外處理 | setup 要讀 `code/storage/` 四十幾個檔，Helm 讀不到 chart 目錄外的檔 | [restructure-components](2026-10-restructure-components.md) |
| 服務重名跨部門 | 只靠服務名的異常歸給其中一個部門，告警可能送錯 | [anomaly-owner](../phase2-anomaly/2026-10-anomaly-owner.md) |
| LLM 逾時 60 秒、alert 時限 600 秒是估的 | 一輪超過 10 件新的、模型又慢，仍會被砍 | [fix-critical](../phase5-learning-loop/2026-10-fix-critical.md) |

#### 9.2 偵測品質

| 項目 | 現況 | 來源 |
|---|---|---|
| Pod 同儕比對在 3 個 Pod、年紀不同時抓不到記憶體洩漏（2026-10-31 測試觀察，還沒有 PDCA） | `run-peer-e2e` 第 5 項失敗：洩漏的 `pk7zn` 10 分鐘內最高 420 MB，另外兩隻 251 MB、163 MB（中位數 207 MB）。多 213 MB（門檻 ≥ 100 MB）、2.03 倍（門檻 ≥ 1.5 倍）兩道都過，但 z = (420 − 207) ÷ 65 = 3.3，沒到門檻 5；手動重播三次都是「3 個 Pod 比過，觸發 0 個」。原因：z 的分母是其他 Pod 之間本來的差距，3 個 Pod 只剩 2 個值，而案例之間刪掉壞 Pod 補上的新 Pod 記憶體本來就比老 Pod 低約 90 MB，把離散度撐大。之前驗過是過的，那時 3 隻年紀一樣。正式環境副本只有 3 個、滾動更新後年紀不同時也會遇到。待決定：A. 記憶體、CPU 兩項的 `z` 調低（例如 3），靠 `min_abs`、`min_ratio` 擋誤報（要確認其他案例不會多出誤報）；B. 只改測試，等新 Pod 長到跟舊的差不多再比（正式環境的弱點還在）。傾向 A，還沒動。<br>討論後的判斷（讀 [peer.py](../../../aiops-engine/code/detector/peer.py) 的推論，沒跑過）：A 改成 A2，`z` 依「其他 Pod 數」（程式已有 `peers` 欄位）分級，例如其他 2 個用 3、3～4 個用 4、5 個以上維持 5，記憶體與 CPU 都套用；只降到 3 仍脆弱（新 Pod 若是 130 MB，z 約 2.6 一樣抓不到），其他只剩 2 個值時 z 沒有統計意義，主要靠 `min_abs`、`min_ratio`；A2 要改 `compare()`（目前只吃固定的 `z`）。<br>新舊 Pod 的問題兩個方向都有：HPA 擴容後老 Pod 400 MB、兩個新 Pod 約 150 MB 時，`rel_floor` 讓分母只有約 15，老 Pod 的 z 約 16、兩道硬門檻也過，現在 z = 5 就會把正常的老 Pod 誤報成洩漏（推論，待用 `run-peer-edge-e2e.sh` 驗證，未確認它有沒有涵蓋擴容情境）；`warmup` 只有 5 分鐘，蓋不住 JVM heap 慢慢長大的時間。<br>斜率（趨勢）方向對但要另做成新規則，不放進同儕比對：不受新舊 Pod 高低差影響、單 Pod 或 2 個 Pod 的服務也能用，規格本來就列為「還沒加」的趨勢；要處理 GC 鋸齒（用回歸或看 GC 後低點是否墊高）與新 Pod 啟動期暖機，實用形式是「照這個速度幾小時後撞到 limit」，可和 `container.memory_near_limit` 接在一起；要有自己的 PDCA 與 e2e 案例（洩漏、GC 鋸齒、新 Pod 暖機） | [run-peer-e2e.sh](../../../e2e/k8s/run-peer-e2e.sh)、[peer.yaml](../../../aiops-engine/code/configs/baseline/peer.yaml) |
| p95 誤報超過目標 | anomaly 0.53%（目標 0.1%）、warning 1.97%（目標 1%） | [baseline-threshold-tune](../phase2-anomaly/2026-10-baseline-threshold-tune.md) |
| 錯誤率 1 個 5xx 就到門檻 | 一週 205 個誤報（0.34%）；後續是「至少 N 個 5xx 才判斷」 | 同上 |
| 只有一部分請求變慢時 trace 段沒報 | 每點改抽 100 個後，合成資料上 10% 請求多 2 秒報得出來；慢的比例更低時仍報不出來，看 p90 或慢請求比例還沒做（[PDCA](../phase2-anomaly/2026-11-trace-sample-100.md)） | [troubleshooting.md](../../troubleshooting.md) |
| `trace.yaml` 的 `sample_per_bucket` 20 要不要調到 100 | 已改成 100。耗時：測試資料上一輪 0.3～0.5 秒（20 時 0.1 秒，排程時限 240 秒）；偵測：20 時 trace 那段 14 過 3 失敗，50 與 100 都 17 項全過，選 100 餘裕較大。100 是 ES `top_hits` 的預設上限。正式環境 API 多時的耗時沒量（[PDCA](../phase2-anomaly/2026-11-trace-sample-100.md)） | 同上 |
| 三成 Pod 沒有服務 label 沒報（B6a） | pipeline 沒 label 時 `service.name` 退回 container 名稱 `app`，欄位不算缺，檢查看不到；master 也一樣，與 trace 合併無關。已做：`missing` 檢查加 `missing_if_equals`（等於 `k8s.container.name` 也算缺），只有單元測試、Painless 沒對真 ES 跑、e2e B6a 等叢集空了再跑（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） | 同上 |
| 每筆異常沒記是哪一版產生的 | 已做：每筆異常帶 `engine_version`（chart 的 `image.tag`，本機是 `dev`）（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） | [baseline/reference.md](../../../aiops-engine/code/configs/baseline/reference.md) |
| `suggest` 只調 `z` | 已做：誤報累積時依時間與偏差選週期放寬（`recurring_override`）、`min_abs` 或 z；不提粒度（改粒度要重新 train，回測比不了）（[PDCA](../phase5-learning-loop/2026-11-suggest-and-rule-backtest.md)） | 同上 |
| 規則核准前沒有回測 | 已做：`backtest` 能跑 `rule_suggestion`，`adjust approve` 對規則也要求回測（[PDCA](../phase5-learning-loop/2026-11-suggest-and-rule-backtest.md)） | 同上 |

#### 9.3 回饋與相似事故

| 項目 | 現況 | 來源 |
|---|---|---|
| 回饋沒有身分 | 表單、網頁沒登入；回饋 3 次可以是同一個人 | [architecture.md 第 10 節](../../architecture.md#10-還沒做還不足的) |
| Grafana 回饋表單是陽春版（**決定：先不改**） | 後端與欄位對得上、單元測試與 `loop_e2e` 過；沒有在真的 Grafana 畫面上驗過（測試 Grafana 沒裝 Business Forms 外掛）。儀表板 JSON 的四個網址寫死 `http://localhost:18080`，表單是在值班人員的瀏覽器裡送出，要改成回饋網頁對外的位址；回饋網頁沒有跨網站（CORS）設定，瀏覽器直接呼叫可能被擋。漏報面板的 `datetime` 元素型別是照外掛文件寫的。上線前要處理 | [grafana/README.md](../../../grafana/README.md)、[aiops-feedback.json](../../../grafana/code/dashboards/aiops-feedback.json) |
| 漏報沒有網頁入口 | 已做：Grafana 回饋儀表板加第二個表單面板，送 `feedback_web` 的 `/api/missed`，文件格式同指令；不另做網頁（使用者決定）；Grafana 上沒實載（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） | 同上 |
| 沒有評測集 | 相似事故在沒有 trace 時，只靠「同服務＋同指標」剛好壓在門檻上 | 同上 |
| 舊事故單匯入 `aiops-incidents` 的格式沒寫 | 已做：`investigator import-incidents`（CSV／JSON，格式見 [configs/rca/README.md](../../../aiops-engine/code/configs/rca/README.md)）；合成 20 筆 recall@3 50%，等真實事故單（[PDCA](../phase4-rca-llm/2026-10-incident-import.md)） | 同上 |
| code-review 剩三項沒動 | 已做：metrics 的回饋改依這段告警的 `alert_id` 載入（漏報仍依時間）、回測用快取代理只讀一次 ES、相似事故候選 20 → 200（已合入 master；[PDCA](../phase5-learning-loop/2026-11-open-issues-batch.md)） | 同上 |

#### 9.4 資料與 log

| 項目 | 現況 | 來源 |
|---|---|---|
| 設備通訊（SECS）、REST API 的 log 還沒有規則 | 缺一般事件、告警、逾時、多行的真實樣本 | [log/README.md](../../log/README.md) |
| 業務 ID（交易 ID）名稱未定 | log、metrics、trace 三份鑰匙圈都是 ❌ | [log](../../log/README.md)、[metrics](../../metrics/README.md)、[trace](../../trace/README.md) |
| metrics 沒有 `trace_id` | exemplar 寫進 ES 時被丟（ES 9.5.5、8.19.23 實測） | [metrics/README.md](../../metrics/README.md) |
| 開 otlp 的服務 log 量兩倍 | 存量兩倍，統計推論也算兩次 | [log-trace-bind](../phase1-collect/2026-10-log-trace-bind.md) |
| Collector 接回 16KB 長行、file＋OTLP 兩份未驗證 | 只用 `_simulate` 驗了 pipeline | 同上 |

#### 9.5 模組缺口與過度設計

| 項目 | 現況 | 來源 |
|---|---|---|
| ① .NET SQL Server、Serilog／NLog 驗證 | 沒做 | [architecture.md 第 10 節](../../architecture.md#10-還沒做還不足的) |
| ③ 副本、正式環境容量 | 沒做 | 同上 |
| ④ p99、慢請求比例、往外呼叫耗時、`groups` 依公司命名、變化點偵測 | 沒做 | 同上 |
| ⑥ 沿呼叫關係合併、排優先、一段記成一筆、靜音、限頻 | 沿呼叫關係合併：一層已做（下游有件開著、直接呼叫它的上游併進來；[PDCA](../phase3-locate/2026-10-cross-service-aggregation.md)）；多層、排優先、靜音、限頻 📝 | 同上 |
| ⑦ 單一 Pod 異常拿它的 trace 逐段比；撈 log 帶 `span.id` | 沒做 | 同上 |
| ⑨ 版本、評測集、第二人確認、排除時段的建議 | 沒做 | 同上 |
| 過度設計：三個自動機制（自選粒度、卜瓦松、借同組）、trace 每一段 baseline、回饋表單選項是估的 | 建議留著不再擴，等真實資料再決定砍哪個 | [architecture.md 第 11 節](../../architecture.md#11-哪裡過度設計) |

### 10. 文件

| 項目 | 現況 |
|---|---|
| retention 內容搬進 log、metrics、trace | 數字照原文搬，沒有重新對程式核對 |
| 保留天數寫兩處 | 各 README 的「多久壓縮」與 [retention-reference.md](../../retention-reference.md) 同一批天數；天數的來源是 `retention.conf` |
