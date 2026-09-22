# Repo 版面：為什麼這樣切

```
services/<app>/              程式碼、Dockerfile                 ← app 團隊
applications/<app>/          跟 devops repo 同格式，可以整個資料夾搬過去
  app.yaml                   ArgoCD 設定 + 這個 app 的歸屬（observability）
  helm/                      這個 app 的 chart，只填 values
  manifests/                 （選用）chart 以外的 YAML 或 Kustomize
charts/app-common/           共用 library chart                 ← 平台
platform/                    平台自己的設定與部署物件            ← 平台，走 cluster-addons
ci/                          CI 驗證
docs/                        設計文件與架構圖
```

切法的原則只有一條：**按「誰改、怎麼生效」切，不要按「是什麼技術」切。**

## applications/ 跟 devops repo 一模一樣

`applications/<app>/{app.yaml, helm/, manifests/}` 就是你們 devops repo 的固定結構，這樣這邊寫好、驗過的 app 可以**整個資料夾直接搬過去**，不用再轉一次格式。

`app.yaml` 的欄位照你們的規則：`project`、`namespace`、`environment`、`owner`、`criticality`、`sync` 必填，production 不可自動同步，`helm.local: true` 時 chart 在 `helm/`、values 路徑相對於 `helm/`。另外多一個 `observability` 區塊寫歸屬，見 [../platform/argocd/](../platform/argocd)。

`ci/validate_app_yaml.py` 是**照你們 README 寫的規則重寫的簡版**，用來確認這邊的 app.yaml 搬過去過得了 CI。正式以你們 devops repo 的 `scripts/validate_application.py` 為準。

## 為什麼程式碼和部署設定分開

`services/<app>/` 放程式碼，`applications/<app>/` 放部署設定。兩種都有人用，差別在**模板改版誰負責**：

| | 程式碼和 chart 放同一個資料夾 | 分開（這裡採用） |
|---|---|---|
| 模板改版 | 平台要發 PR 進每一個服務的資料夾 | 平台只改 `charts/app-common` |
| 部署設定能不能直接搬去 devops repo | 要從程式碼裡挑出來 | `applications/<app>/` 整個搬 |
| CI 觸發 | 改 code 和改部署方式混在一起 | 可以分開觸發，改 values 不用重 build 映像 |
| 服務要搬去別的 repo | 整包帶走比較乾淨 | 要記得兩邊都搬 |

最後一列是分開的代價。但**新增一個服務一年只會發生幾次，平台改標準是持續的**，所以優化後者。

## 兩邊資料夾必須同名

`services/yms-sim` ↔ `applications/yms-sim`。CI 和 ArgoCD 都靠這個名字接起來。

名字對不上會發生的事是**靜默的**：CI build 了 `yms-sim` 的映像，ArgoCD 部署的是另一個名字的 app，指到舊 tag，於是新版永遠上不去，但兩邊的 pipeline 都是綠的。

這個名字同時也是 `helm/values.yaml` 的 `app.name`、pod 的 `app.kubernetes.io/name`、metrics 和 log 上的 `app` 欄位。上線後不要改。

## ArgoCD 怎麼找到 app

你們的 `applicationsets/devops/applicationset.yaml` 用 git files generator 掃 `applications/**/app.yaml`，**一個 app.yaml 長出一個 Application**，要用 helm 還是 manifests 由 app.yaml 自己決定。

所以：

- **新增一個 app = 新增一個有 app.yaml 的資料夾**，ApplicationSet 不用改
- **沒有 app.yaml 的資料夾不會被掃到**，所以 `charts/app-common` 放在 `applications/` 外面不會被誤部署

這個 repo 之前寫的「用 git generator 掃 `charts/*`」是在知道你們的結構之前的建議，跟你們實際的做法不符，已經改掉。

## `app-common` 為什麼不在 applications/ 底下

它是 library chart（`type: library`），**不能單獨部署**，也不是一個 app。放進 `applications/` 會讓人以為它是一個服務。

每個 app 的 `helm/Chart.yaml` 用相對路徑引用它：

```yaml
dependencies:
  - name: app-common
    version: 0.1.0
    repository: file://../../../charts/app-common
```

**這個相對路徑只在這個 repo 裡有效。** 搬到 devops repo 之後，`app-common` 要發佈到內部的 Helm repo，`repository` 改成那個位址。跨 repo 的 `file://` 不會動。

## 映像 tag 怎麼從 code 流到 chart

三種做法：

| 做法 | 怎麼運作 | 問題 |
|---|---|---|
| **CI 改 values 再 commit** | build 完把 tag 寫回 `applications/<app>/helm/values.yaml` | 會產生機器 commit，而且容易跟人的修改撞在一起 |
| **ArgoCD Image Updater** | 它自己盯 registry，發現新 tag 就更新 | 多一個元件要維運 |
| **tag 用 git commit sha** | `values.yaml` 不寫死 tag，部署時用參數帶入 | 要 CI 有辦法帶參數給 ArgoCD，GitOps 純度降低 |

**先用第一種**，它最直接，出事也看得懂。等服務數量多到機器 commit 變雜訊，再換第二種。

不管選哪種，`tag` **都不能用 `latest`**——`app-common` 的模板會直接讓它 build 失敗，因為 metrics 和 log 上的 `version` 欄位要靠它，寫 `latest` 等於所有版本長得一樣，換版造成的問題就查不出來了。

## 新增一個服務

1. `services/<app>/` 放程式碼和 Dockerfile
2. 複製一份現成的 `applications/<app>/`，改 `app.yaml`（namespace、owner、**observability**）和 `helm/values.yaml`
3. 跑 `ci/validate.sh`，三道檢查都過才發 PR

**歸屬寫在 app.yaml 裡，跟 app 在同一個 PR。** 沒寫 `observability` 過不了 CI，所以不會有「app 先上線、歸屬後補，中間的資料進兜底救不回來」的情況。

## 平台的東西為什麼不放進 applications/

`applications/` 是給 devops 那個 ApplicationSet 掃的，每個 app.yaml 部署成一個 Application。**平台元件要進每一個場區叢集**，走的是 cluster-addons 那條（matrix generator × 場區叢集清單）。

`platform/` 底下的東西性質都不一樣：

| | 本質 | 怎麼生效 |
|---|---|---|
| `cluster-addons/vmstack-values.yaml` | **別人 chart 的參數** | 由 cluster-addons 的 ApplicationSet 帶進去 |
| `cluster-addons/ns-labeler/` | 我們自己的 K8s 物件 | 是一個 chart，同樣掛在 cluster-addons 底下 |
| `fluentd/fluent.conf` | 設定檔內容 | 渲染進 ConfigMap 或 Secret |
| `argocd/` | 要併進 devops repo 的 template 和 CI 腳本 | 不部署，是給 devops repo 用的 |

放進 `applications/` 的話，會被錯的 ApplicationSet 部署、也會被 app.yaml 的規則（environment、sync）檢查，兩者都不對。

## 本機驗證

```bash
./ci/validate.sh          # app.yaml、observability、chart 渲染三道檢查
./tests/routing/run.sh    # 分流的端到端測試，用 applications/metrics-sim/helm 部署模擬器
```
