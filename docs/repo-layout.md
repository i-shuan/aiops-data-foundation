# Repo 版面：為什麼這樣切

```
services/<app>/        程式碼、Dockerfile          ← app 團隊
charts/<app>/          Chart.yaml、values          ← app 團隊填 values，模板由平台維護
charts/app-common/     共用 library chart          ← 平台
platform/              平台自己的設定與部署物件      ← 平台
ci/                    chart 驗證腳本
docs/                  設計文件與架構圖
```

切法的原則只有一條：**按「誰改、怎麼生效」切，不要按「是什麼技術」切。**

## 為什麼 services 和 charts 分開，而不是 chart 放進每個服務裡

兩種都有人用，差別在**模板改版誰負責**。

| | `services/<app>/chart/`（放在一起） | `services/` 和 `charts/` 分開（這裡採用） |
|---|---|---|
| 模板改版 | 平台要發 PR 進每一個服務資料夾 | 平台只改 `charts/app-common` |
| 誰看得到全部 chart | 要翻遍整棵樹 | `ls charts/` 就看完 |
| CI 觸發 | 改 code 和改部署方式混在一起 | 可以分開觸發，改 values 不用重 build 映像 |
| 服務要搬去別的 repo | 整包帶走比較乾淨 | 要記得兩邊都搬 |

最後一列是分開的代價。但**新增一個服務一年只會發生幾次，平台改標準是持續的**，所以優化後者。

## 兩邊資料夾必須同名

`services/yms-sim` ↔ `charts/yms-sim`。CI 和 ArgoCD 都靠這個名字接起來。

名字對不上會發生的事是**靜默的**：CI build 了 `yms-sim` 的映像，ArgoCD 部署的是 `yms-simulator` 的 chart，指到舊 tag，於是新版永遠上不去，但兩邊的 pipeline 都是綠的。

## ArgoCD：用 git generator，不要用 list generator

`cluster-addons` 那邊用 list generator 是對的，因為**清單是 cluster，數量少、變動少**。

應用這邊相反：**清單是 app，會一直長**。用 list generator 的話每加一個服務就要改 ApplicationSet，那就是「新增 app 要人工改平台設定」，跟擴廠不能全人工是同一個問題。

改用 git generator 掃資料夾：

```yaml
generators:
  - matrix:
      generators:
        # 有哪些 app：掃 charts/ 底下的資料夾，新增資料夾自己會長出 Application
        - git:
            repoURL: <這個 repo>
            revision: main
            directories:
              - path: charts/*
              - path: charts/app-common
                exclude: true      # library chart 不能單獨部署
        # 部到哪些 cluster
        - list:
            elements:
              - cluster: central
              - cluster: zk
              - cluster: el
```

`matrix` 把兩邊相乘，**新增一個服務 = 新增一個資料夾，新增一個廠 = list 加一行**，ApplicationSet 本身不用動。

`app-common` 一定要排除，它是 `type: library`，單獨部署會失敗。

## 映像 tag 怎麼從 code 流到 chart

這是 monorepo 最容易做歪的地方。三種做法：

| 做法 | 怎麼運作 | 問題 |
|---|---|---|
| **CI 改 values 再 commit** | build 完把 tag 寫回 `charts/<app>/values.yaml` | 會產生機器 commit，而且容易跟人的修改撞在一起 |
| **ArgoCD Image Updater** | 它自己盯 registry，發現新 tag 就更新 | 多一個元件要維運 |
| **tag 用 git commit sha** | `values.yaml` 不寫死 tag，部署時用參數帶入 | 要 CI 有辦法帶參數給 ArgoCD，GitOps 純度降低 |

**先用第一種**，它最直接，出事也看得懂。等服務數量多到機器 commit 變雜訊，再換第二種。

不管選哪種，`tag` **都不能用 `latest`**——`app-common` 的模板會直接讓它 build 失敗，因為 metrics 和 log 上的 `version` 欄位要靠它，寫 `latest` 等於所有版本長得一樣，換版造成的問題就查不出來了。

## 新增一個服務的完整流程

1. **先在 [apps.tsv](../platform/attribution/registry/apps.tsv) 加一行並合進去。** 這步要最先做，晚了那段時間的資料會進 unassigned 而且救不回來，見 [attribution.md](attribution.md)
2. `services/<app>/` 放程式碼和 Dockerfile
3. `charts/<app>/` 複製一份現成 chart，改 `values.yaml`
4. 推上去，ArgoCD 自己長出 Application

**第 1 步和第 4 步之間如果隔太久，中間的資料就是錯的。** 所以 app 的 pipeline 要有部署前檢查，見 [attribution.md](attribution.md) 的那段 shell。

## 平台的東西為什麼不放進 charts/

`platform/` 底下有三種性質完全不同的東西：

| | 本質 | 怎麼生效 |
|---|---|---|
| `cluster-addons/vmstack-values.yaml` | **別人 chart 的參數** | 由 cluster-addons 的 ApplicationSet 帶進去 |
| `fluentd/fluent.conf` | 設定檔內容 | 渲染進 ConfigMap 或 Secret |
| `attribution/` | 我們自己的 K8s 物件 + 資料檔 | 目前是 kubectl，**應該包成 chart** |

第一種永遠不可能變成 chart，chart 是別人的。第三種包成 chart 明顯比較好，見 [attribution.md](attribution.md) 的待確認。

把這三種混進 `charts/` 會讓人以為都能 `helm install`，第一種會直接炸。
