# aiops-data-foundation

每個 App 在自己的 `app/<app>/k8s/values.yaml` 宣告 observability identity。

部署時，這些資訊會成為 Kubernetes Namespace labels。

vmagent 只根據 Namespace 的 `tenant` label，把 metrics 寫進對應租戶的 AccountID。

```
values.yaml
    ↓  platform/argocd/applicationset.yaml（ArgoCD），或 chart 自己的 namespace.yaml（手動 Helm）
Namespace metadata.labels
    ↓  platform/victoriametrics/vmstack-values.yaml
vmagent：抓取時貼上 tenant，送出前換成 vm_account_id，補上 site
    ↓  兩條 remoteWrite：中科站、二林站各一條，同一份資料兩座都寫
兩座的 vmauth（各驗各的 token）→ 各自的 VictoriaMetrics（依 vm_account_id 分租戶）
```

用途：讓每筆 metrics 和 log 一出生就帶著「這是誰的、要給誰看」，為之後的 AIOps 分析打資料地基。

## 目錄

| 資料夾 | 放什麼 | 什麼時候會動到 |
|---|---|---|
| `app/<app>/` | 一個 app 一個資料夾。程式碼和 `Dockerfile` 直接放在裡面 | 改 app 的程式 |
| `app/<app>/k8s/` | 這個 app 怎麼部署到 K8s（Helm chart）。`values.yaml` 裡的 `observability` 決定它的資料算哪個部門 | 新增 app、改部門歸屬 |
| `platform/` | 平台這一側的設定，所有 app 共用。分流、雙寫、暫存都在這裡 | 加部門、改寫入目的地 |
| `tests/` | 檢查上面那些設定有沒有寫錯 | 改了 `app/` 或 `platform/` 之後跑 |
| `requirements/` | 每個需求的文件：為什麼做、做成什麼樣、怎麼做、怎麼驗收。給主管看的架構圖也在這 | 開新需求 |
| `tools/` | 檢查文件格式與規則同步的小工具，跟監控資料無關 | 改了 `.md` 或 `AGENTS.md` 之後跑 |
| `.agents/`、`.claude/`、`.github/` | 給 AI 助手讀的規則與防護 | 改 AI 的工作規則 |

平台團隊要部署 app、套監控設定、查分流問題，看 [`platform/README.md`](platform/README.md)。

`platform/` 底下依用途分三個資料夾：`victoriametrics/` 是 metrics 的分流與雙寫（這次做的都在這），`argocd/` 和 `logging/` 這次沒動。

| 檔案 | 用途 |
|---|---|
| `README.md` | 給平台團隊的說明：部署 app、分流、套設定、查問題 |
| `victoriametrics/vmstack-values.yaml` | 主要的設定。租戶對照、防冒充、兩條 `remoteWrite`、暫存 |
| `victoriametrics/site-ctsp.yaml`、`site-erlin.yaml` | 廠區標記，每個 cluster 部署時疊一個 |
| `victoriametrics/vmagent-routing-overlay.yaml` | 給已經在跑的環境（AP1105）用的，只帶要改的那幾段 |
| `argocd/applicationset.yaml` | 有 ArgoCD 的環境才用。目前先做沒有 ArgoCD 的路 |
| `logging/fluent.conf` | log 的分流，跟 metrics 無關，這次沒動 |

跟 VictoriaMetrics 有關的檔都在 `platform/victoriametrics/`。另外每個 app 的 `k8s/templates/vmservicescrape.yaml` 也屬於這一側（告訴 vmagent 這個 app 要被抓），檔案開頭有標明。

`tests/` 裡的每一支：

| 測試 | 狀態 | 用途 |
|---|---|---|
| `validate.sh` | 可用 | 不需要 cluster，幾秒跑完。設定寫錯在這裡擋 |
| `tenant-routing/` | 可用 | 本機 k3d 實測：分流、雙寫、停機補送 |
| `routing/` | ⚠ 過期 | ArgoCD 那條路的實測，測的是改版前的分流，現在跑不過 |
| `ap1105-sim/` | ⚠ 過期 | 模擬 AP1105 套舊 overlay，現在跑不過。等新的驗證做完才刪 |

根目錄的四份文件：`README.md` 給人看的總說明；`AGENTS.md`、`CLAUDE.md` 給 AI 助手的規則；`PLAYBOOK.md` 給人看的「怎麼用 AI 改這個專案」。

## values.yaml

```yaml
argocd:                        # ApplicationSet 讀的，chart 不會用到
  namespace: mes-dispatch-prod
  project: ai-platform
  environment: production      # production 不可自動同步
  owner: mes-team
  criticality: high
  autoSync: false

observability:                 # 唯一的 identity 來源
  dept: D3120                  # 必填
  tenant: tsid                 # 必填：tsid / bsid / icsd / imc / platform
  system: mes                  # 選填

image:                         # 以下是一般的 Helm values
  repository: nexus.aillm.spil.com.tw/docker/mes/mes-dispatch
  tag: "2.3.1"
```

## Namespace 最後的 labels

照原值抄過去，不查表、不推導：

```yaml
metadata:
  labels:
    dept: D3120
    tenant: tsid
    system: mes
```

沒寫 `system` 就不會有 `system` 這個 label，不會變成 `system=""`。

## tenant 怎麼分流

| `tenant` | AccountID |
|---|---|
| `tsid` | 1001 |
| `bsid` | 1002 |
| `icsd` | 1003 |
| `imc` | 1004 |
| `platform`、沒有、不合法 | 1000 |

這張對照只有一份，在 `platform/victoriametrics/vmstack-values.yaml` 的 `vmagent.spec.inlineRelabelConfig`。一個 namespace 只能寫一個租戶。

vmagent 不知道 `values.yaml`、app 名稱、namespace 名稱，只看 namespace 的 `tenant`。租戶靠每筆樣本上的 `vm_account_id` 分，`remoteWrite` 的條數跟租戶數無關。

⚠ **租戶隔離靠的是 vmagent 這一側的設定。** 中央的 vmauth 只驗 token（每個 cluster、每一座各一把），不檢查 `vm_account_id`。所以 vmagent 會先清掉 app 自己吐的 `vm_account_id`，沒貼 `tenant` 的 namespace 也在抓取時就補成 `platform`；少了任何一條，任何 pod 都能把資料寫進別人的租戶，而且不會報錯。`tests/validate.sh` 會擋下這些規則被拿掉或順序寫錯。

## 兩廠雙寫

每個 cluster 的 vmagent 把同一份資料同時寫到中科站和二林站。任一座停機或跨廠網路斷掉時，另一座照常收；送不到的那一條先排在 vmagent 的 PVC 裡，恢復後自動補送。

| 設定 | 在哪 | 說明 |
|---|---|---|
| 兩條 `remoteWrite` | `platform/victoriametrics/vmstack-values.yaml` | 兩座各一條，各帶自己的 token（Secret `vmauth-token` 的 `ctsp`、`erlin`） |
| 排隊的資料放 PVC | 同上的 `statefulMode`、`statefulStorage` | vmagent 是 StatefulSet。不開的話 vmagent 一重啟排隊的資料就沒了 |
| 暫存上限 | 同上的 `remoteWriteSettings.maxDiskUsagePerURL` | 每個目的地 20 GiB，PVC 50 GiB。是起始值，算法寫在檔案裡 |
| 廠區 | `platform/victoriametrics/site-ctsp.yaml`、`platform/victoriametrics/site-erlin.yaml` | 每個 cluster 不同，部署時多疊一個 `-f`。樣本上是 `site` |

```bash
helm upgrade --install vmstack <repo>/victoria-metrics-k8s-stack --version 0.91.0 -n vm-monitoring \
  -f platform/victoriametrics/vmstack-values.yaml -f platform/victoriametrics/site-ctsp.yaml
```

⚠ **套之前先 `kubectl get storageclass`。** 沒有可用的 StorageClass 時 PVC 卡在 Pending，vmagent 起不來，整個 cluster 的 metrics 會停。這在本機 k3d 測不到。

⚠ **兩個寫入網址都是佔位**（`.invalid` 結尾），待 Infra 確認，確認前不能上線。`tests/validate.sh` 會印出提醒。

⚠ **沒疊廠區的檔，樣本上不會有 `site`，不報錯。** 部署指令 `tests/validate.sh` 檢查不到。

⚠ **暫存超過上限時最舊的資料會被丟掉，不報錯。** 要看 `vmagent_remotewrite_pending_data_bytes`。

## 新增一個 App

1. 開一個 `app/<app>/`，放程式碼和 Dockerfile
2. 從別的 app 複製一份 `k8s/` 到 `app/<app>/k8s/`，改 `values.yaml` 的 `argocd`、`observability` 和一般 values
3. `./tests/validate.sh` 過了再發 PR

資料夾的名字就是 app 的名字。ApplicationSet 看到新的 `app/<app>/k8s/values.yaml` 就會自己長出一個 Application，不用改任何平台設定。

不走 ArgoCD、人下 `helm` 部署的 app：`values.yaml` 不寫 `argocd`，改寫 `namespace`，chart 帶一個 `templates/namespace.yaml` 自己建 namespace 並貼 label，範本是 [`app/eqp-collector`](app/eqp-collector/README.md)。`argocd` 區塊和 `templates/namespace.yaml` 要剛好有一個，兩個都有或都沒有 `tests/validate.sh` 會擋下。

## 已經在跑的 vmstack 加上分流

適用：cluster 已經用 Helm 裝好 victoria-metrics-k8s-stack（例如 AP1105 的 release `vmstack`）。

**用哪個檔**：`platform/victoriametrics/vmagent-routing-overlay.yaml`，疊在該環境**自己現有的** values 上。

```
-f current-values.yaml                    ← helm get values 抓下來的，該環境原本的設定全部保留
-f platform/victoriametrics/vmagent-routing-overlay.yaml  ← 只帶分流、雙寫、暫存那幾段
-f platform/victoriametrics/site-<廠區>.yaml               ← 這個 cluster 的廠區
```

⚠ **新的 overlay 還沒有在 AP1105 的模擬環境走過完整流程。** [`tests/ap1105-sim`](tests/ap1105-sim/README.md) 驗的是改版前的 overlay（三把 token 的分流），它的兩個發現仍然適用，套之前要先確認：

- **VMAgent CRD 認不認得這幾段的欄位**：`kubectl explain` 說不存在不能當依據。用 `kubectl apply --dry-run=server` 比對送進去和回來的 spec。CRD 太舊時不能套：規則會被默默裁掉，所有資料進同一個租戶。本機在 operator v0.74.0 上確認過都不會被裁。
- **最新一版是 failed（例如 post-upgrade hook 失敗）**：要修 hook 的話不要直接關 syncJob，它建的告警規則和 dashboard 會被一起刪掉。

其他要先確認的：

- ⚠ **不要套整份 `vmstack-values.yaml`**。它是另一套環境的完整 values，會關掉 vmcluster、把 image registry 換成 nexus、把 `externalLabels` 清成 `{}`。
- ⚠ **沒貼 `tenant` 的 namespace 會換租戶**。AP1105 現在全部寫 `insert/0`；套上去之後 node-exporter、kube-state-metrics 這些會改寫進 1000，看租戶 0 的 Grafana / 告警會變空。舊資料留在租戶 0 不搬。
- ⚠ **vmagent 會從 Deployment 變成 StatefulSet**，要一個 50 GiB 的 PVC，套的時候會重啟一次。先確認有可用的 StorageClass。
- vmauth 的 URL 與路徑：overlay 裡兩個網址都是佔位，待 Infra 確認。
- Secret `vmauth-token` 要先建好，兩個 key：`ctsp`、`erlin`，放兩座各發給這個 cluster 的那一把。順序反了 vmagent 找不到 key，整個 cluster 的 metrics 會停。
- overlay 的那幾段跟 `vmstack-values.yaml` 一模一樣，只有 `url` 可以依環境改。`tests/validate.sh` 會檢查，也會擋下 overlay 裡出現其他設定。

**回滾**：`helm rollback vmstack <套之前記下的 REVISION> -n vm-monitoring`。

## Tests

```bash
./tests/validate.sh              # 不需要 cluster
./tests/tenant-routing/run.sh           # 本機 k3d E2E，不走 ArgoCD。會建一個獨立的 k3d cluster tenant-test
./tests/tenant-routing/run.sh outage    # 停機與補送，多花約 5 分鐘
```

**validate.sh**：每個 values.yaml 的必填欄位、`tenant` 是否合法（允許的值從 `vmstack-values.yaml` 的租戶對照讀出，不另外維護清單）、租戶對照本身（先清掉進來的 `vm_account_id`、AccountID 不重複、兜底在最後）、雙寫與暫存（`remoteWrite` 剛好兩條、網址和 token 不一樣、開 `statefulMode`、有暫存上限、PVC 裝得下）、`platform/victoriametrics/site-*.yaml` 只能有 `site`、`vmagent-routing-overlay.yaml` 的分流設定跟 `vmstack-values.yaml` 一致（url 除外）且不帶其他設定、namespace 不能被兩個 app 共用、chart 渲染得出來、namespace label 渲染結果跟 values.yaml 一致。內建的錯誤寫法（`imcc`、`it.imc`、還在寫 `group`、欄位打錯、拿掉防偽造的規則…）一定要被擋下，否則腳本本身失敗。手動 Helm 的 app（沒有 `argocd` 區塊、有 `templates/namespace.yaml`）不查 `argocd` 欄位，`observability` 照查，namespace label 改成渲染 chart、取出它自己建的 Namespace 來比。

**tenant-routing/run.sh**：用手動 Helm 把 `eqp-collector` 的 chart 部署七個情境（四個租戶、沒貼、打錯，加一個自帶假 `tenant` 和 `vm_account_id` 想冒充別人的 pod），確認每個 namespace 的資料在兩座都只出現在該去的 AccountID，而且帶著 `site`。vmagent 的設定取自 `platform/victoriametrics/vmstack-values.yaml` 加 `platform/victoriametrics/site-ctsp.yaml`，只換位址；中央是本機起的兩座，各是一座 VictoriaMetrics cluster 加一個只驗自己那把 token 的 vmauth。也會比對 VMAgent 送進去和存下來的 spec，確認欄位沒有被 CRD 裁掉。`VMSTACK_VALUES=<改壞的複本>` 可以確認測試抓得到錯：拿掉防偽造那條、把 `icsd` 對到別的 AccountID 都確認過會 exit 1。`./tests/tenant-routing/run.sh down` 刪掉 cluster。

**tenant-routing/run.sh outage**：關掉站 1 的入口，確認站 2 照常；中間刪掉 vmagent 的 pod，確認佇列還在；再恢復，比兩座在停機期間的筆數（站 1 要有站 2 的 95% 以上）。結束時不管成功失敗都會把入口開回來。確認過正常時補得回來、沒開 `statefulMode` 的複本會 exit 1。⚠ 本機只起一套 vmagent（扮演中科的 cluster），不是兩個廠區各一套；「跨廠斷線」用「那一座送不到」代替。

**routing/run.sh**：⚠ 測的是改版前的分流（`group`、三把 token），目前跑不過，等 ArgoCD 那條路補做時一起改。以下是它原本的設計：

```
正式    ArgoCD ← devops repo       vmagent → 中央 vmauth → IT / IMC / platform tenant
本機    ArgoCD ← 本機 git server   vmagent → 本機 vmauth → VMSingle it / imc / platform
```

ApplicationSet、vmagent、測試 app 的 values 都取自正式檔案，只換位址。有一個 namespace 會先建好再交給 ArgoCD，驗證既有的 namespace 也會被補上 label。

`validate.sh` 確認過正常時 exit 0、故意改壞時 exit 1。

E2E 的前提：

```bash
kubectl create ns argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/core-install.yaml
docker build --build-arg BASE_IMAGE=python:3.12-slim -t metrics-sim:test app/metrics-sim
k3d image import metrics-sim:test victoriametrics/vmauth:v1.148.0 -c zk
```

另外要有 VictoriaMetrics operator。

## Known limitations

- **log 還有一份 dept 對照。** `platform/logging/fluent.conf` 依 `dept` 分流，自己寫著部門清單，而且目前只有 D3120、D1110 兩條：**D3130、D1120 的 log 會進 unassigned**（`yms-sim` 就是 D1120）。log 的分流跟 metrics 的 `tenant` 無關。
- **dept 沒有清單可以比對**，打錯字 CI 抓不到。metrics 不受影響（看的是 `tenant`），但 log 會進 unassigned。
- **改組要改每個相關 app 的 values.yaml。** 沒有中央的部門對照表是刻意的，代價是部門換租戶時要一個一個改。production 還要等手動 sync 才會生效。
- **搬到 devops repo 時**：這個 repo 的路徑是 `app/<app>/k8s/`，公司 devops repo 的慣例是 `applications/<app>/helm/`，搬過去要改路徑和 `platform/argocd/applicationset.yaml` 的 discovery。discovery 找的是 `values.yaml`，**只用 manifests、沒有 Helm chart 的 app 不會被發現**。其他 app 的 `app.yaml` 內容也要搬進各自的 `values.yaml`。
- **app 的 JSON log 有 `time` 欄位，時間會變成 1970。** `platform/logging/fluent.conf` 的 json parser 把字串 `"2026-09-22T..."` 當 epoch 秒數，只讀到 `2026` → `1970-01-01T00:33:46`。本機 argocd 的 log 全部在 `k8s-unassigned-1970.01.01`，照日期看 Kibana 看不到。app 的 log 先不要用 `time` 當欄位名。
- **`log` 欄位型別衝突會整筆拒收。** 不是 JSON 的行（Python traceback、coredns 警告）`log` 是字串；有 app 吐 ECS 格式（`log.level`）時同一天的 index 裡 `log` 變成 object，之後的純文字行全部 400 被拒，只在 Fluentd 自己的 log 看得到。本機 unassigned 已經發生（Elasticsearch 自己的 log 造成），IT / IMC 目前還沒有。
- **共用模板每個 app 各一份。** 標準 label、probe、VMServiceScrape 的模板（`k8s/templates/_*.tpl`）每個 app 各複製一份。平台要改標準時每個 app 都要改，漏改一個不會報錯。
- **token 要手動建**，Vault 還沒上線。
- **查詢端沒有處理補送期間的缺口。** 剛恢復的那一座在補送完成前查得到的資料不完整，查詢要不要避開它不在這個 repo 的範圍。
- **暫存的大小沒有實測依據。** 20 GiB 是起始值，要在正式的 cluster 量每小時的暫存量才知道撐不撐得住 8 小時。超過上限會丟資料，目前沒有告警。
- **租戶隔離沒有中央那一層把關。** vmauth 只驗 token，不檢查 `vm_account_id`；能改 vmagent 設定的人就能寫進任何租戶。
- **ArgoCD 那條路目前沒有 E2E。** `tests/routing` 還是舊分流的版本。
- **本機 E2E 不是正式環境**：單一 cluster、in-cluster destination、本機 git server、不經過中央 vmauth 的真實 tenant。
