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

## Branch：要看哪一個

| branch | 是什麼 | 狀態 |
|---|---|---|
| **`feature/efk-log-collection`** | **目前的設計，全部內容都在這裡**，而且每一塊都在本機 k3d 實測過 | ← **看這個** |
| `master` | 只有 metrics 分流的舊快照 | 過時，落後十幾個 commit |

**branch 名稱會誤導。** 它叫 efk，但現在裡面不只 log：metrics 分流、log 分流、歸屬設計、四張架構圖全在這一個 branch 上。`master` 停在還沒做 log 的階段，而且連 metrics 那份文件都是實測前的版本（沒有 `scrapeClasses` 的驗證結果，也沒有 VMServiceScrape 靠 Service label 選取那個坑）。

合回 master 之後這張表就可以刪掉。

### 已經刪掉的 branch

| branch | 核心做法 | 為什麼否掉 |
|---|---|---|
| `feature/org-yaml-tenant-mapping`（`3f920c2`） | 手寫一份 `org.yaml`，用它渲染出每個 namespace 的 manifest，再產生 vmagent 的分流規則 | 部門一多就爆炸（兩個組織 50 個單位要維護 50 份渲染結果），而且擴廠要人工重跑渲染。改成用 namespace label 當依據 |
| `feature/aiops-telemetry-foundation`（`a8724d2`） | 最初的架構文件 | 內容已經被 master 吸收，沒有獨有的東西 |

SHA 留著是為了萬一要回去翻，`git checkout <sha>` 還撈得到（垃圾回收前）。

## 目前的文件

- [docs/deployment-layers.md](docs/deployment-layers.md)：**東西該掛在哪一層。** 三個問題分出業務層 / 平台層 / 中央層，以及分錯會怎樣——四種替代做法的後果都寫了。
- [docs/repo-layout.md](docs/repo-layout.md)：`applications/` 為什麼跟 devops repo 同格式、程式碼和部署設定為什麼分開、映像 tag 怎麼從 code 流到 values。
- [platform/argocd/](platform/argocd)：**歸屬寫在 app.yaml。** 一隻 app 一個 namespace，歸屬跟 owner / criticality 放在一起，由 ArgoCD 貼到 namespace 上。要改 devops repo 的哪三個地方、CI 擋什麼、沒驗證的部分都在這裡。
- [docs/attribution.md](docs/attribution.md)：**歸屬怎麼決定。** app.yaml 為主、ns-labeler 補漏（平台的 namespace、沒進 ArgoCD 的舊 app），兩條路怎麼不互相覆蓋。已在本機實測通過。
- [docs/roadmap.md](docs/roadmap.md)：三階段路線圖。metrics 分流、log 接 EFK、產線與 MES 異常分析。
- [docs/efk.md](docs/efk.md)：log 怎麼分流。Fluentd 依 namespace 的 dept label 改 tag，每個 group 一個 output、各帶只能寫自己 index 的 API key。已在本機實測通過。
- [docs/vmagent.md](docs/vmagent.md)：metrics 怎麼分流。現況是一個 cluster 一台 vmagent、一把 token、資料全進同一個 tenant；分流做法是把 `remoteWrite` 從一條拆成每個 group 一條。
- [docs/architecture/](docs/architecture)：四張架構圖，每張回答一個不同的問題
  - [before-after-tenant-split.svg](docs/architecture/before-after-tenant-split.svg)：**給人看的 Before / After。** tenant 從「依廠分」改成「依組織分」，重點是線會交叉。
  - [where-does-it-go.svg](docs/architecture/where-does-it-go.svg)：**決策圖。** 一個東西要部署，三個問題決定它掛哪一層。
  - [two-pipelines.svg](docs/architecture/two-pipelines.svg)：app 的 CI/CD 和平台的 CI/CD 是兩條，交會點只有 namespace 這個物件。
  - [attribution-where-things-run.svg](docs/architecture/attribution-where-things-run.svg)：對照表一份在 git，labeler 每個 cluster 各跑一份。

## 已經定案的

- **tenant 設計不碰。** 中央那邊依 bearer token 分 tenant，要新增由他們自己加。我們只負責在 K8s 這側決定「哪一批資料用哪一把 token」。
- **應用團隊不需要知道 tenant、group、token。** 規格統一由共用 chart 產生，見 [charts/app-common](charts/app-common)。
- **分流點在每個 cluster 唯一那台 vmagent**，不另外長新的收集器。
- **metrics 依 namespace 的 `group` label 分流**，不是 `dept`。一份資料可以同時給多個 tenant（`group=it.imc`），改組也不用碰 vmagent 設定。log 那邊還是依 `dept`，還不支援多個 tenant，見 [efk.md](docs/efk.md)。
- **平台要進每個 cluster 的東西，全部掛在既有的 cluster-addons ApplicationSet 底下**，不另外開分發管道。擴廠 = list 加一行。理由與替代做法的後果見 [deployment-layers.md](docs/deployment-layers.md)。
- **namespace 名稱不含組織資訊**，格式是 `{cluster}-{app}-{env}` 或 `{app}-{env}`，所以歸屬要另外標。
- **歸屬寫在 devops repo 的 `app.yaml`，由 ArgoCD 貼到 namespace 上**。不是 ArgoCD 部署的 namespace 才由 ns-labeler 標。同一個 namespace 只能由一邊管，CI 會擋。收集端只讀 namespace 的 label，不管是誰標的。見 [platform/argocd/](platform/argocd)、[attribution.md](docs/attribution.md)。
- **log 收集器是 Fluentd，Elasticsearch 是 Basic 免費版。** 權限只能切到 index 層級，沒有 Elastic ML。
- **metrics 與 log 兩條路都已在本機 k3d 實測跑通**，九個關聯欄位的名稱兩邊一致（平台 namespace 的 `group` 值例外，見 efk.md），group 之間的隔離擋得住。細節與踩到的坑見 [vmagent.md](docs/vmagent.md)、[efk.md](docs/efk.md)。

## 還沒定案的

1. **`dept` / `group` 這兩個 label 名稱在正式環境有沒有被別人用掉。** 上線前查一次 `kubectl get ns -L dept,group`。
2. **正式環境的 operator 與 Fluentd 版本**。本機在 operator v0.74.1、Fluentd v1.17 實測都可用，你們那邊要確認版本不會差太多。注意 chart 裝的 CRD 沒帶 schema，欄位打錯不會報錯，只會靜默失效。
3. **app 名稱在同一個 cluster 內是否唯一**（對照表靠它比對，撞名會標錯）。
4. **token 怎麼自動進新廠的 cluster**：現在靠 Rancher 手動建 Secret，擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
5. **產線資料能不能出廠、MES 是不是自己開發的**：影響路線圖第三階段的選擇。

## 目錄

```
services/                  應用程式碼，一個 app 一個資料夾
  yms-sim/                 參考實作：業務指標和 JSON log 長什麼樣
  metrics-sim/             持續模擬收值，給分流測試用
  _standards/              跨服務的共用設定範本（Java logback 等）

applications/              部署設定，跟 devops repo 同格式，可以整個資料夾搬過去
  <app>/app.yaml           ArgoCD 設定 + 歸屬（observability），名稱必須跟 services/ 一致
  <app>/helm/              這個 app 的 chart，只填 values
                           （yms-sim、mes-dispatch、metrics-sim）

charts/app-common/         平台維護的 library chart，標準 label、probe、VMServiceScrape 由它產生

                           以上三塊不由平台 pipeline 部署，走 app 自己的 CI/CD

platform/                  平台端，由平台 pipeline 部署
  cluster-addons/          掛在既有的 cluster-addons ApplicationSet 底下     → 每個 cluster
    vmstack-values.yaml    vmagent 三條 remoteWrite、kube-state-metrics allowlist
    ns-labeler/            chart：對照表、對帳腳本、CronJob、告警規則（給不是 ArgoCD 部署的 namespace）
  argocd/                  歸屬寫在 app.yaml：template、CI 驗證、app.yaml 範例    → 併進 devops repo
  fluentd/                 fluent.conf，三個 output 各帶自己的 API key         → 每個 cluster
  central/                 Elasticsearch 的 API key、role 設定腳本             → 只有 central

tests/routing/             metrics 分流的端到端測試（只 IT / 只 IMC / 兩個都送 / 寫錯時的行為）
tests/app-yaml/            歸屬寫在 app.yaml 這條路的測試（CI 驗證 + template + 分流）
ci/                        validate.sh 一次跑完三道檢查：app.yaml、observability、chart 渲染
docs/                      設計文件與架構圖，不部署
```

規則與慣例見 [services/README.md](services/README.md)、版面配置的理由見 [docs/repo-layout.md](docs/repo-layout.md)。
