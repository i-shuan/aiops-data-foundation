# aiops-data-foundation

替未來的 AIOps 打資料地基：**讓每筆資料一出生就帶身分證，再照身分證送進對的地方。**

不做的話，等要上 AIOps 時第一件事是花好幾個月清資料，而且歷史資料的欄位救不回來，AI 沒有基準線可以學。

> [!IMPORTANT]
> **重點：log 格式不一致的處理**
>
> 全廠的 log 不可能統一成一種格式：Java 的 JSON、Go 的 key=value、設備通訊的 SECS、各服務各寫各的 REST API log。AIOps 能不能用 log，關鍵在這裡。
>
> - **已完成：** 依服務宣告的格式（chart 的 `logFormat`）分派到不同的解析規則。沒有規則的格式也照收、原文完整保留。見 [docs/elasticsearch.md「pipeline」](docs/elasticsearch.md#pipeline)。
> - **待辦：SECS、REST API 的解析規則還沒寫，需要真實 log 樣本**（SECS 要含一般事件、告警、逾時、多行 body；REST API 要兩三個服務的範例）。SECS 一則跨多行，還要在 Collector 收的時候加多行合併。

## 目前的文件

- [docs/elasticsearch.md](docs/elasticsearch.md)：log 怎麼進 ES。OpenTelemetry Collector 依 namespace 的 dept 分 group，進 `k8s-{group}-{cluster}`，欄位由中央 pipeline 統一整理成 AIOps 要的格式；log 格式不一致時，依服務宣告的格式分派到不同的解析規則。已在 k3s 上端到端驗證。
- [docs/elasticsearch-testing.md](docs/elasticsearch-testing.md)：Fluent Bit 時期的測資、53 項檢查結果與 23 個 level 案例、key 失效的故障測試，以及測試中抓到的問題。
- [docs/metrics.md](docs/metrics.md)：metrics 怎麼進 ES。OpenTelemetry Collector 收 kubelet、K8s 物件狀態、應用的 Prometheus metrics，依每筆資料自己的 namespace 讀 dept 分 group，寫進 ES 的 time series data stream。分部門的規則與 key 跟 log 共用。已在 k3s 上端到端驗證。
- [docs/traces.md](docs/traces.md)：trace 怎麼進 ES。應用送到同節點的 OpenTelemetry Collector，依連線來源的 Pod 讀 namespace 的 dept 分 group，擋冒充；log 的 `trace.id` 對得到 trace。已在 k3s 上端到端驗證。
- [apps/standards/tracing.md](apps/standards/tracing.md)：給應用團隊的 trace 埋點指引：哪些自動有、什麼時候要自己加、怎麼加、命名規則、取樣。
- [docs/aiops-rca.md](docs/aiops-rca.md)：**AIOps 根因分析怎麼做**：三種資料各回答什麼、六個步驟裡哪幾步需要 AI，以及最關鍵的「怎麼知道平常的樣子」（看哪些指標、週期、方法、需要的條件）。
- [docs/aiops-cases.md](docs/aiops-cases.md)：**15 個真實故障案例要哪種「平常」才抓得到**：N+1、錯誤被吞、連線池洩漏、重試風暴、單一 Pod 壞掉…逐一對到訊號、baseline 種類、資料與程式現況，排出接下來要做的順序。
- [docs/retention.md](docs/retention.md)：**保留天數與容量（方案，還沒套用）**：各種資料留多久、舊 metrics 降解析度、log 和 trace 改留統計值，以及容量估算。
- [aiops/baseline/](aiops/baseline/README.md)：**AIOps 第一步：學「平常的樣子」、抓異常**。每個服務 × API × 指標，取過去 4 週同時段的中位數與範圍；模擬資料測試 4 個異常全部抓到、2,592 個正常點 0 誤報。
- [aiops/rca/](aiops/rca/README.md)：**AIOps 第二步：根因分析**。baseline 發現異常後，照 Metrics → Trace → Log 自動查：慢在哪一段、錯誤從哪裡開始、各服務的 log、新出現的錯誤樣式、呼叫關係圖，產出報告。k3s 實測：製造 Oracle 變慢，正確指到那一段和那行 log。
- [docs/otel-logs-compare.md](docs/otel-logs-compare.md)：log 從 Fluent Bit 改用 OpenTelemetry Collector 之前的比較紀錄與決定。

## 已經定案的

- **log、metrics、trace 都進 Elasticsearch（9.x）。** 不再使用 VictoriaMetrics。
- **收集器：log、metrics、trace 都用 OpenTelemetry Collector**，每個節點一個，分部門的規則和 key 共用。log 原本用 Fluent Bit，比較後改用 OTel：欄位做得到完全一樣，重啟不會掉 log（Fluent Bit 每次重啟掉幾筆到 2 秒），而且只要維護一套。代價是 key 失效或 ES 拒收時資料會直接丟（metrics 本來就有這個限制），見 [docs/otel-logs-compare.md](docs/otel-logs-compare.md)。中央 pipeline 仍接受 Fluent Bit 的資料，邊緣 VM 可以用。
- **每個 group 一把寫入 key，log 與 metrics 共用。** 某把 key 失效只影響那個 group；寫錯 group 會被 ES 擋下。
- **換 key 不掉資料、不重啟：** Collector 從檔案讀 key，用 `platform/central/rotate-writer-key.sh` 換（新舊 key 重疊一段時間），見 [docs/elasticsearch.md「換 key」](docs/elasticsearch.md#換-key)。
- **trace 的業務欄位名稱**：`lot.id`（批號）、`equipment.id`（機台）、`work_order.id`（工單）、`recipe.id`（配方），全廠統一，見 [apps/standards/tracing.md](apps/standards/tracing.md)。新增要先跟平台確認。
- **應用團隊不需要知道 group、key。** 規格統一由共用 chart 產生，見 [apps/](apps)。
- **namespace 名稱不含組織資訊**，格式是 `{cluster}-{app}-{env}` 或 `{app}-{env}`，所以歸屬要另外標。

## 還沒定案的

1. **歸屬放哪裡**：目前 log 和 metrics 都實作成讀 namespace 的 `dept` label，部門 → group 對照在 `platform/groups.yaml`。要改成一份「app → group」的中央清單的話，兩邊要一起改。
2. **app 名稱是否全廠唯一**（用中央清單的話，這是前提）。
3. **寫入 key 怎麼自動進新廠的 cluster**：現在靠 Rancher 手動建 Secret（`otel-collector` namespace），擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
4. **K8s events 怎麼歸屬**：log 與 metrics 都還沒收 events。event 帶 namespace 名稱，照現在讀 namespace label 的做法應該能分，要實測。

## AIOps 怎麼用這些資料

根因分析分六步：**發現異常 → 合併告警 → 定位到哪一段 → 找出可能的原因 → 說明與建議 → 人確認**。

分工：**metrics 決定什麼時候告警，trace 和 log 說明原因。** baseline 的流量、錯誤率、延遲從應用的 metrics 算（每個請求都算到、可以留很久）；trace 有取樣、只留 7 天，log 不記正常的請求，都只在告警後查那段時間。架構圖見 [docs/aiops-rca.md「分工」](docs/aiops-rca.md#分工metrics-決定什麼時候告警trace-和-log-說明原因)。

- **需要 AI 的**：發現異常（每個服務學出「平常的樣子」，偏離就標出來），以及從大量 log 裡找出新出現或突然變多的錯誤樣式。
- **規則、統計就做得到的**：用 trace 的呼叫關係合併告警、比對 trace 找出慢在哪一段、對照換版時間。
- **大型語言模型**：最後把證據整理成人看得懂的結論。

最關鍵的是**怎麼知道「平常的樣子」**：依服務 × API × 指標，比對過去幾週同一時段的表現，而不是固定門檻。建議先用最簡單、好解釋的方法（過去 4 週同時段的中位數與範圍）上線。前提是歷史資料夠長（至少 4 週）、不缺、不重複、欄位一致。訓練用的是統計過的時間序列，**量只有 GB 等級**；真正的門檻是時間夠長、資料品質，以及**事故紀錄**（要從現在開始記）。詳見 [docs/aiops-rca.md](docs/aiops-rca.md)。

## 待辦

1. **評估 Elastic 內建的 AIOps 功能（要付費授權）。** 免費的 Basic 授權實測全部不能用；異常偵測等機器學習功能要 Platinum 以上，AI 助理可能要 Enterprise（Platinum 自架版可能只賣既有客戶，要跟 Elastic 確認）。可以先開 30 天試用評估，試用只能開一次，等資料累積夠再開：

   | 功能 | 做什麼 | Basic 實測 |
   |---|---|---|
   | 異常偵測（Anomaly detection） | 學每個服務的正常樣子，偏離時告警 | ❌ `ml` |
   | log 樣式分群（Log pattern analysis） | 把大量 log 自動歸成幾種樣式 | ❌ `categorize_text` |
   | log 量突變分析（Log rate analysis） | log 量突然變多時，找出是哪個服務、哪種訊息造成的 | ❌ `frequent_item_sets` |
   | 變化點偵測（Change point detection） | 找出 metrics 開始變化的時間點 | ❌ `change_point` |
   | AI 助理（AI Assistant） | 用自然語言查資料、解釋告警 | ❌ `inference` |

   不買的話，AIOps 要自己做（例如 Python 從 ES 讀資料跑模型），資料地基不用改。
2. **保留天數要定案並套用。** ⚠️ metrics、trace 現在用 ES 預設的 ILM，**只有 rollover、不會刪除**，硬碟遲早滿。方案見 [docs/retention.md](docs/retention.md)：metrics 原始 14 天、降成 5 分鐘留到 90 天、降成 1 小時留到 400 天；log 原文 30 天＋統計值 400 天；trace 原始 7 天＋從 trace 算的指標 400 天。要決定天數、副本，並用正式環境的量重算容量。
3. **AIOps 分兩階段推進：先一般服務，再 EAP 與機台。** 階段 1 在 Web API、worker 上把三種資料串接、baseline、事故紀錄、合併告警跑通；還沒做的是從 metrics 跳到慢 trace、連續異常才報、事故紀錄、合併告警，以及用 4 週真實資料驗證 baseline。階段 2 再接 EAP：SECS 解析規則、載具／腔體／交易 ID 的欄位名稱、機台交握耗時與告警的 baseline。底層共用，不用重寫。清單見 [docs/aiops-rca.md「推進的兩個階段」](docs/aiops-rca.md#推進的兩個階段先一般服務再-eap-與機台)。

## 目錄

```
aiops/       AIOps：baseline（學平常的樣子、抓異常）、rca（根因分析：Metrics → Trace → Log）
apps/        應用端：共用 chart、範例、Java log 設定、trace 埋點指引、CI 檢查
platform/    平台端：groups.yaml（部門 → group）、中央 ES 設定、各 cluster 的 OpenTelemetry Collector（log 與 metrics）
docs/        設計文件與測試紀錄
test/        本機 k3s 端到端測試
```
