# aiops-data-foundation

替未來的 AIOps 打資料地基：**讓每筆資料一出生就帶身分證，再照身分證送進對的地方。**

不做的話，等要上 AIOps 時第一件事是花好幾個月清資料，而且歷史資料的欄位救不回來，AI 沒有基準線可以學。

> [!IMPORTANT]
> **重點：log 格式不一致的處理**
>
> 全廠的 log 不可能統一成一種格式：Java 的 JSON、Go 的 key=value、設備通訊的 SECS、各服務各寫各的 REST API log。AIOps 能不能用 log，關鍵在這裡。
>
> - **已完成：** 依服務宣告的格式（chart 的 `logFormat`）分派到不同的解析規則。沒有規則的格式也照收、原文完整保留。見 [docs/elasticsearch.md「pipeline」](docs/elasticsearch.md#pipeline)。
> - **待辦：SECS、REST API 的解析規則還沒寫，需要真實 log 樣本**（SECS 要含一般事件、告警、逾時、多行 body；REST API 要兩三個服務的範例）。SECS 一則跨多行，還要在 Fluent Bit 加多行合併。

## 目前的文件

- [docs/elasticsearch.md](docs/elasticsearch.md)：log 怎麼進 ES。Fluent Bit 依 namespace 的 dept 分流到 `k8s-{group}-{cluster}`，欄位由中央 pipeline 統一整理成 AIOps 要的格式；log 格式不一致時，依服務宣告的格式分派到不同的解析規則。已在 k3s 上端到端驗證。
- [docs/elasticsearch-testing.md](docs/elasticsearch-testing.md)：上面那套的測資、53 項檢查結果與 23 個 level 案例、key 失效的故障測試，以及測試中抓到的問題。
- [docs/metrics.md](docs/metrics.md)：metrics 怎麼進 ES。OpenTelemetry Collector 收 kubelet、K8s 物件狀態、應用的 Prometheus metrics，依每筆資料自己的 namespace 讀 dept 分 group，寫進 ES 的 time series data stream。分部門的規則與 key 跟 log 共用。已在 k3s 上端到端驗證。

## 已經定案的

- **log 和 metrics 都進 Elasticsearch（9.x）。** 不再使用 VictoriaMetrics。log 用 Fluent Bit 收，metrics 用 OpenTelemetry Collector 收。
- **每個 group 一把寫入 key，log 與 metrics 共用。** 某把 key 失效只影響那個 group；寫錯 group 會被 ES 擋下。
- **應用團隊不需要知道 group、key。** 規格統一由共用 chart 產生，見 [apps/](apps)。
- **namespace 名稱不含組織資訊**，格式是 `{cluster}-{app}-{env}` 或 `{app}-{env}`，所以歸屬要另外標。

## 還沒定案的

1. **歸屬放哪裡**：目前 log 和 metrics 都實作成讀 namespace 的 `dept` label，部門 → group 對照在 `platform/groups.yaml`。要改成一份「app → group」的中央清單的話，兩邊要一起改。
2. **你們的 Fluent Bit 是哪一版**。`namespace_labels` 3.x 就有，但用 API key 寫 ES 要 4.1 以上，見 [docs/elasticsearch.md](docs/elasticsearch.md#坑)。
3. **app 名稱是否全廠唯一**（用中央清單的話，這是前提）。
4. **寫入 key 怎麼自動進新廠的 cluster**：現在靠 Rancher 手動建 Secret（`logging`、`otel-metrics` 兩個 namespace 各一份），擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
5. **K8s events 怎麼歸屬**：log 與 metrics 都還沒收 events。event 帶 namespace 名稱，照現在讀 namespace label 的做法應該能分，要實測。

## 目錄

```
apps/        應用端：共用 chart、範例、Java log 設定、CI 檢查
platform/    平台端：groups.yaml（部門 → group）、中央 ES 設定、各 cluster 的 Fluent Bit（log）與 OpenTelemetry Collector（metrics）
docs/        設計文件與測試紀錄
test/        本機 k3s 端到端測試
```
