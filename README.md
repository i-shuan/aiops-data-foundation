# aiops-data-foundation

替未來的 AIOps 打資料地基：**讓每筆資料一出生就帶身分證，再照身分證送進對的地方。**

不做的話，等要上 AIOps 時第一件事是花好幾個月清資料，而且歷史資料的欄位救不回來，AI 沒有基準線可以學。

## 這套系統回答什麼、不回答什麼

先對齊期待，這是最容易誤會的地方。

| 回答 | 不回答 |
|---|---|
| **資料有沒有漏**：某台機台 20 分鐘沒回報、今天該收 500 批只收到 480 批 | **良率為什麼掉** |
| **系統哪裡出問題**：MES 交易變慢、記憶體一直漲、queue 在堆積 | **製程參數為什麼漂移** |
| **為什麼**：昨晚換版 → 記憶體洩漏 → 連線逾時 → 報工卡住 | **設備本身的異常** |

右邊那一欄是 SPC、FDC、YMS 的範圍，這套系統不會、也不該去取代它們。

### AI 的比重其實很小

「AIOps」這個名字容易讓人以為重點在 AI，實際上重點在資料有沒有收對：

| 做的事 | 貢獻 | 是 AI 嗎 |
|---|---|---|
| 欄位統一、資料收齊 | 最大 | 不是，是工程 |
| 規則告警 | 大 | 不是 |
| 統計基線取代固定閾值 | 中 | 算是，但只是統計 |
| 跨來源關聯找根因 | 中 | 算是 |
| 用 LLM 解釋錯誤訊息 | 小 | 是 |

**前兩項就吃掉八成效益，而且都不需要 AI。**

### 跟良率分析的界線

log 記錄的是「程式做了什麼」，良率是「產品量出來多少」，兩者用的工具完全不同。**把量測值塞進 log 或 metrics 去分析良率，會因為資料量和高基數直接壓垮這套系統。**

正確的接點是：**讓 YMS 這類系統吐出彙總後的業務指標**（每小時良率、各 bin 比例、資料完整度），變成 metrics。這樣才能偵測「良率突然掉」並關聯到「同時間某台機台資料異常」或「昨晚換了版本」，而原始量測資料仍然留在專門的分析工具裡。

### 一句話定位

**從「有人打電話來說產線怪怪的」，變成「系統先叫，而且直接指出是哪台機台、從幾點開始、那時候剛好發生了什麼」。**

它不會自己修，也不會取代現有的良率分析工具。

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
- **metrics 與 log 兩條路都已在本機 k3d 實測跑通**，八個關聯欄位對得起來，group 之間的隔離擋得住。細節與踩到的坑見 [vmagent.md](docs/vmagent.md)、[efk.md](docs/efk.md)。

## 還沒定案的

1. **歸屬放哪裡**：namespace 的 label，還是一份「app → group」的中央清單。兩者的差異見對話紀錄，取決於下面第 2、3 點。
2. **正式環境的 operator 與 Fluentd 版本**。本機在 operator v0.74.1、Fluentd v1.17 實測都可用，你們那邊要確認版本不會差太多。注意 chart 裝的 CRD 沒帶 schema，欄位打錯不會報錯，只會靜默失效。
3. **app 名稱是否全廠唯一**（用中央清單的話，這是前提）。
4. **token 怎麼自動進新廠的 cluster**：現在靠 Rancher 手動建 Secret，擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
5. **產線資料能不能出廠、MES 是不是自己開發的**：影響路線圖第三階段的選擇。

## 目錄

```
apps/        應用端：共用 chart、範例、Java log 設定、CI 檢查
platform/    平台端：目前只剩 kube-state-metrics 的 label 設定與 ES 權限腳本
docs/        設計文件與架構圖
```
