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
- [docs/elasticsearch-testing.md](docs/elasticsearch-testing.md)：上面那套的測資、53 項檢查結果與 23 個 level 案例、key 失效的故障測試，以及測試中抓到的問題。
- [docs/metrics.md](docs/metrics.md)：metrics 怎麼進 ES。OpenTelemetry Collector 收 kubelet、K8s 物件狀態、應用的 Prometheus metrics，依每筆資料自己的 namespace 讀 dept 分 group，寫進 ES 的 time series data stream。分部門的規則與 key 跟 log 共用。已在 k3s 上端到端驗證。

## 已經定案的

- **log、metrics、trace 都進 Elasticsearch（9.x）。** 不再使用 VictoriaMetrics。
- **收集器：log、metrics、trace 都用 OpenTelemetry Collector**，每個節點一個，分部門的規則和 key 共用。log 原本用 Fluent Bit，比較後改用 OTel：欄位做得到完全一樣，重啟不會掉 log（Fluent Bit 每次重啟掉幾筆到 2 秒），而且只要維護一套。代價是 key 失效或 ES 拒收時資料會直接丟（metrics 本來就有這個限制），見 [docs/otel-logs-compare.md](docs/otel-logs-compare.md)。中央 pipeline 仍接受 Fluent Bit 的資料，邊緣 VM 可以用。
- **每個 group 一把寫入 key，log 與 metrics 共用。** 某把 key 失效只影響那個 group；寫錯 group 會被 ES 擋下。
- **換 key 不掉資料、不重啟：** Collector 從檔案讀 key，用 `platform/central/rotate-writer-key.sh` 換（新舊 key 重疊一段時間），見 [docs/elasticsearch.md「換 key」](docs/elasticsearch.md#換-key)。
- **應用團隊不需要知道 group、key。** 規格統一由共用 chart 產生，見 [apps/](apps)。
- **namespace 名稱不含組織資訊**，格式是 `{cluster}-{app}-{env}` 或 `{app}-{env}`，所以歸屬要另外標。

## 還沒定案的

1. **歸屬放哪裡**：目前 log 和 metrics 都實作成讀 namespace 的 `dept` label，部門 → group 對照在 `platform/groups.yaml`。要改成一份「app → group」的中央清單的話，兩邊要一起改。
2. **app 名稱是否全廠唯一**（用中央清單的話，這是前提）。
3. **寫入 key 怎麼自動進新廠的 cluster**：現在靠 Rancher 手動建 Secret（`otel-collector` namespace），擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
4. **K8s events 怎麼歸屬**：log 與 metrics 都還沒收 events。event 帶 namespace 名稱，照現在讀 namespace label 的做法應該能分，要實測。

## 待辦

1. **trace 還沒收。** 已定用 OpenTelemetry Collector，進 ES 9 的 OTLP 入口；要跟 log 的 `trace.id`、metrics 的 `service.name` 串得起來。
2. **評估 Elastic 內建的 AIOps 功能（要付費授權）。** 免費的 Basic 授權實測全部不能用；異常偵測等機器學習功能要 Platinum 以上，AI 助理可能要 Enterprise（Platinum 自架版可能只賣既有客戶，要跟 Elastic 確認）。可以先開 30 天試用評估，試用只能開一次，等資料累積夠再開：

   | 功能 | 做什麼 | Basic 實測 |
   |---|---|---|
   | 異常偵測（Anomaly detection） | 學每個服務的正常樣子，偏離時告警 | ❌ `ml` |
   | log 樣式分群（Log pattern analysis） | 把大量 log 自動歸成幾種樣式 | ❌ `categorize_text` |
   | log 量突變分析（Log rate analysis） | log 量突然變多時，找出是哪個服務、哪種訊息造成的 | ❌ `frequent_item_sets` |
   | 變化點偵測（Change point detection） | 找出 metrics 開始變化的時間點 | ❌ `change_point` |
   | AI 助理（AI Assistant） | 用自然語言查資料、解釋告警 | ❌ `inference` |

   不買的話，AIOps 要自己做（例如 Python 從 ES 讀資料跑模型），資料地基不用改。

## 目錄

```
apps/        應用端：共用 chart、範例、Java log 設定、CI 檢查
platform/    平台端：groups.yaml（部門 → group）、中央 ES 設定、各 cluster 的 OpenTelemetry Collector（log 與 metrics）
docs/        設計文件與測試紀錄
test/        本機 k3s 端到端測試
```
