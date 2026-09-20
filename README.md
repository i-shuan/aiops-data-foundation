# aiops-data-foundation

替未來的 AIOps 打資料地基：**讓每筆資料一出生就帶身分證，再照身分證送進對的地方。**

不做的話，等要上 AIOps 時第一件事是花好幾個月清資料，而且歷史資料的欄位救不回來，AI 沒有基準線可以學。

## 目前的文件

- [docs/roadmap.md](docs/roadmap.md)：三階段路線圖。metrics 分流、log 接 EFK、產線與 MES 異常分析。
- [docs/efk.md](docs/efk.md)：log 怎麼分流。Fluentd 依 namespace 的 dept label 改 tag，每個 group 一個 output、各帶只能寫自己 index 的 API key。已在本機實測通過。
- [docs/vmagent.md](docs/vmagent.md)：metrics 怎麼分流。現況是一個 cluster 一台 vmagent、一把 token、資料全進同一個 tenant；分流做法是把 `remoteWrite` 從一條拆成每個 group 一條。
- [docs/architecture/](docs/architecture)：架構圖。

## 已經定案的

- **tenant 設計不碰。** 中央那邊依 bearer token 分 tenant，要新增由他們自己加。我們只負責在 K8s 這側決定「哪一批資料用哪一把 token」。
- **應用團隊不需要知道 tenant、group、token。** 規格統一由共用 chart 產生，見 [apps/](apps)。
- **分流點在每個 cluster 唯一那台 vmagent**，不另外長新的收集器。
- **namespace 名稱不含組織資訊**，格式是 `{cluster}-{app}-{env}` 或 `{app}-{env}`，所以歸屬要另外標。
- **log 收集器是 Fluentd，Elasticsearch 是 Basic 免費版。** 權限只能切到 index 層級，沒有 Elastic ML。

## 還沒定案的

1. **歸屬放哪裡**：namespace 的 label，還是一份「app → group」的中央清單。兩者的差異見對話紀錄，取決於下面第 2、3 點。
2. **`scrapeClasses` 你們的 operator 版本支不支援**（決定能不能在抓取時讀 namespace label）：
   ```bash
   kubectl explain vmagent.spec.scrapeClasses.attachMetadata
   ```
3. **Fluentd 的 `kubernetes_metadata_filter` 帶出來的 namespace label 實際 key 長什麼樣**，要送一筆 log 出來看（log 端同一個問題）。
4. **app 名稱是否全廠唯一**（用中央清單的話，這是前提）。
5. **token 怎麼自動進新廠的 cluster**：現在靠 Rancher 手動建 Secret，擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
6. **產線資料能不能出廠、MES 是不是自己開發的**：影響路線圖第三階段的選擇。

## 目錄

```
apps/        應用端：共用 chart、範例、Java log 設定、CI 檢查
platform/    平台端：目前只剩 kube-state-metrics 的 label 設定與 ES 權限腳本
docs/        設計文件與架構圖
```
