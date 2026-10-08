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

- [docs/vmagent.md](docs/vmagent.md)：metrics 怎麼分流。現況是一個 cluster 一台 vmagent、一把 token、資料全進同一個 tenant；分流做法是把 `remoteWrite` 從一條拆成每個 group 一條。
- [docs/elasticsearch.md](docs/elasticsearch.md)：log 怎麼進 ES。Fluent Bit 依 namespace 的 dept 分流到 `k8s-{group}-{cluster}`，欄位由中央 pipeline 統一整理成 AIOps 要的格式；log 格式不一致時，依服務宣告的格式分派到不同的解析規則。已在 k3s 上端到端驗證。
- [docs/elasticsearch-testing.md](docs/elasticsearch-testing.md)：上面那套的測資、53 項檢查結果與 23 個 level 案例、key 失效的故障測試，以及測試中抓到的問題。
- [docs/architecture/](docs/architecture)：架構圖。

## 已經定案的

- **tenant 設計不碰。** 中央那邊依 bearer token 分 tenant，要新增由他們自己加。我們只負責在 K8s 這側決定「哪一批資料用哪一把 token」。
- **應用團隊不需要知道 tenant、group、token。** 規格統一由共用 chart 產生，見 [apps/](apps)。
- **分流點在每個 cluster 唯一那台 vmagent**，不另外長新的收集器。
- **namespace 名稱不含組織資訊**，格式是 `{cluster}-{app}-{env}` 或 `{app}-{env}`，所以歸屬要另外標。

## 還沒定案的

1. **歸屬放哪裡**：namespace 的 label，還是一份「app → group」的中央清單。兩者的差異見對話紀錄，取決於下面第 2、3 點。
2. **`scrapeClasses` 你們的 operator 版本支不支援**（決定能不能在抓取時讀 namespace label）：
   ```bash
   kubectl explain vmagent.spec.scrapeClasses.attachMetadata
   ```
3. **你們的 Fluent Bit 是哪一版**。`namespace_labels` 3.x 就有，但用 API key 寫 ES 要 4.1 以上，見 [docs/elasticsearch.md](docs/elasticsearch.md#坑)。
4. **app 名稱是否全廠唯一**（用中央清單的話，這是前提）。
5. **token 怎麼自動進新廠的 cluster**：現在靠 Rancher 手動建 Secret，擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
6. **K8s events 怎麼歸屬**：event 只帶 namespace 名稱，要分流得先決定第 1 點。container log 的部分已經完成，見 [docs/elasticsearch.md](docs/elasticsearch.md)。

## 目錄

```
apps/        應用端：共用 chart、範例、Java log 設定、CI 檢查
platform/    平台端：groups.yaml（部門 → group）、中央 ES 設定、各 cluster 的 Fluent Bit 與 vmstack 設定
docs/        設計文件與架構圖
test/        本機 k3s 端到端測試
```
