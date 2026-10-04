# aiops-data-foundation

每個 App 在自己的 `applications/<app>/helm/values.yaml` 宣告 observability identity。

部署時，這些資訊會成為 Kubernetes Namespace labels。

vmagent 只根據 Namespace 的 `tenant` label，把 metrics 寫進對應租戶的 AccountID。

```
values.yaml
    ↓  platform/applicationset.yaml（ArgoCD），或 chart 自己的 namespace.yaml（手動 Helm）
Namespace metadata.labels
    ↓  platform/vmstack-values.yaml
vmagent：抓取時貼上 tenant，送出前換成 vm_account_id
    ↓  一條 remoteWrite，帶這個 cluster 自己的 token
vmauth（驗 token）→ VictoriaMetrics（依 vm_account_id 分租戶）
```

用途：讓每筆 metrics 和 log 一出生就帶著「這是誰的、要給誰看」，為之後的 AIOps 分析打資料地基。

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
  tenant: icsd                 # 必填：tsid / bsid / icsd / imc / platform
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
    tenant: icsd
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

這張對照只有一份，在 `platform/vmstack-values.yaml` 的 `vmagent.spec.inlineRelabelConfig`。一個 namespace 只能寫一個租戶。

vmagent 不知道 `values.yaml`、app 名稱、namespace 名稱，只看 namespace 的 `tenant`。送出去只有一條 `remoteWrite`，租戶靠每筆樣本上的 `vm_account_id` 分。

⚠ **租戶隔離靠的是 vmagent 這一側的設定。** 中央的 vmauth 只驗 token（每個 cluster 一把），不檢查 `vm_account_id`。所以 vmagent 會先清掉 app 自己吐的 `vm_account_id`，沒貼 `tenant` 的 namespace 也在抓取時就補成 `platform`；少了任何一條，任何 pod 都能把資料寫進別人的租戶，而且不會報錯。`tests/validate.sh` 會擋下這些規則被拿掉或順序寫錯。

## 新增一個 App

1. `services/<app>/` 放程式碼和 Dockerfile
2. 複製一份 `applications/<app>/helm/`，改 `values.yaml` 的 `argocd`、`observability` 和一般 values
3. `./tests/validate.sh` 過了再發 PR

`services/<app>` 和 `applications/<app>` 要同名。ApplicationSet 看到新的 `helm/values.yaml` 就會自己長出一個 Application，不用改任何平台設定。

不走 ArgoCD、人下 `helm` 部署的 app：`values.yaml` 不寫 `argocd`，改寫 `namespace`，chart 帶一個 `templates/namespace.yaml` 自己建 namespace 並貼 label，範本是 [`applications/eqp-collector`](applications/eqp-collector/README.md)。`argocd` 區塊和 `templates/namespace.yaml` 要剛好有一個，兩個都有或都沒有 `tests/validate.sh` 會擋下。

## 已經在跑的 vmstack 加上分流

適用：cluster 已經用 Helm 裝好 victoria-metrics-k8s-stack（例如 AP1105 的 release `vmstack`）。

**用哪個檔**：`platform/vmagent-routing-overlay.yaml`，疊在該環境**自己現有的** values 上。

```
-f current-values.yaml                    ← helm get values 抓下來的，該環境原本的設定全部保留
-f platform/vmagent-routing-overlay.yaml  ← 只帶 scrapeClasses、inlineRelabelConfig、remoteWrite 三段
```

⚠ **新的 overlay 還沒有在 AP1105 的模擬環境走過完整流程。** [`tests/ap1105-sim`](tests/ap1105-sim/README.md) 驗的是改版前的 overlay（三把 token 的分流），它的兩個發現仍然適用，套之前要先確認：

- **VMAgent CRD 認不認得這三段的欄位**：`kubectl explain` 說不存在不能當依據。用 `kubectl apply --dry-run=server` 比對送進去和回來的 spec。CRD 太舊時不能套：規則會被默默裁掉，所有資料進同一個租戶。本機在 operator v0.74.0 上確認過三段都不會被裁。
- **最新一版是 failed（例如 post-upgrade hook 失敗）**：要修 hook 的話不要直接關 syncJob，它建的告警規則和 dashboard 會被一起刪掉。

其他要先確認的：

- ⚠ **不要套整份 `vmstack-values.yaml`**。它是另一套環境的完整 values，會關掉 vmcluster、把 image registry 換成 nexus、把 `externalLabels` 清成 `{}`。
- ⚠ **沒貼 `tenant` 的 namespace 會換租戶**。AP1105 現在全部寫 `insert/0`；套上去之後 node-exporter、kube-state-metrics 這些會改寫進 1000，看租戶 0 的 Grafana / 告警會變空。舊資料留在租戶 0 不搬。
- vmauth 的 URL 與路徑：overlay 裡的 `/insert/multitenant/prometheus/api/v1/write` 是佔位，待 Infra 確認。
- Secret `vmauth-token` 要先建好，key 是 `token`，放這個 cluster 自己的那一把。順序反了 vmagent 找不到 key，整個 cluster 的 metrics 會停。
- overlay 的三段跟 `vmstack-values.yaml` 一模一樣，只有 `url` 可以依環境改。`tests/validate.sh` 會檢查，也會擋下 overlay 裡出現其他設定。

**回滾**：`helm rollback vmstack <套之前記下的 REVISION> -n vm-monitoring`。

## Tests

```bash
./tests/validate.sh              # 不需要 cluster
./tests/tenant-routing/run.sh    # 本機 k3d E2E，不走 ArgoCD。會建一個獨立的 k3d cluster tenant-test
```

**validate.sh**：每個 values.yaml 的必填欄位、`tenant` 是否合法（允許的值從 `vmstack-values.yaml` 的租戶對照讀出，不另外維護清單）、租戶對照本身（先清掉進來的 `vm_account_id`、AccountID 不重複、兜底在最後、`remoteWrite` 一條）、`vmagent-routing-overlay.yaml` 的分流設定跟 `vmstack-values.yaml` 一致（url 除外）且不帶其他設定、namespace 不能被兩個 app 共用、chart 渲染得出來、namespace label 渲染結果跟 values.yaml 一致。內建的錯誤寫法（`imcc`、`it.imc`、還在寫 `group`、欄位打錯、拿掉防偽造的規則…）一定要被擋下，否則腳本本身失敗。手動 Helm 的 app（沒有 `argocd` 區塊、有 `templates/namespace.yaml`）不查 `argocd` 欄位，`observability` 照查，namespace label 改成渲染 chart、取出它自己建的 Namespace 來比。

**tenant-routing/run.sh**：用手動 Helm 把 `eqp-collector` 的 chart 部署七個情境（四個租戶、沒貼、打錯，加一個自帶假 `tenant` 和 `vm_account_id` 想冒充別人的 pod），確認每個 namespace 的資料只出現在該去的 AccountID。vmagent 的分流三段取自 `platform/vmstack-values.yaml`，只換位址；中央是本機起的一座 VictoriaMetrics cluster 加一個只驗 token 的 vmauth。也會比對 VMAgent 送進去和存下來的 spec，確認欄位沒有被 CRD 裁掉。`VMSTACK_VALUES=<改壞的複本>` 可以確認測試抓得到錯：拿掉防偽造那條、把 `icsd` 對到別的 AccountID 都確認過會 exit 1。`./tests/tenant-routing/run.sh down` 刪掉 cluster。

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
docker build --build-arg BASE_IMAGE=python:3.12-slim -t metrics-sim:test services/metrics-sim
k3d image import metrics-sim:test victoriametrics/vmauth:v1.148.0 -c zk
```

另外要有 VictoriaMetrics operator。

## Known limitations

- **log 還有一份 dept 對照。** `platform/fluent.conf` 依 `dept` 分流，自己寫著部門清單，而且目前只有 D3120、D1110 兩條：**D3130、D1120 的 log 會進 unassigned**（`yms-sim` 就是 D1120）。log 的分流跟 metrics 的 `tenant` 無關。
- **dept 沒有清單可以比對**，打錯字 CI 抓不到。metrics 不受影響（看的是 `tenant`），但 log 會進 unassigned。
- **改組要改每個相關 app 的 values.yaml。** 沒有中央的部門對照表是刻意的，代價是部門換租戶時要一個一個改。production 還要等手動 sync 才會生效。
- **搬到 devops repo 時**：discovery 改成找 `applications/*/helm/values.yaml`，**只用 manifests、沒有 `helm/` 的 app 不會被發現**。其他 app 的 `app.yaml` 內容也要搬進各自的 `values.yaml`。
- **app 的 JSON log 有 `time` 欄位，時間會變成 1970。** `platform/fluent.conf` 的 json parser 把字串 `"2026-09-22T..."` 當 epoch 秒數，只讀到 `2026` → `1970-01-01T00:33:46`。本機 argocd 的 log 全部在 `k8s-unassigned-1970.01.01`，照日期看 Kibana 看不到。app 的 log 先不要用 `time` 當欄位名。
- **`log` 欄位型別衝突會整筆拒收。** 不是 JSON 的行（Python traceback、coredns 警告）`log` 是字串；有 app 吐 ECS 格式（`log.level`）時同一天的 index 裡 `log` 變成 object，之後的純文字行全部 400 被拒，只在 Fluentd 自己的 log 看得到。本機 unassigned 已經發生（Elasticsearch 自己的 log 造成），IT / IMC 目前還沒有。
- **共用模板每個 app 各一份。** 標準 label、probe、VMServiceScrape 的模板（`helm/templates/_*.tpl`）每個 app 各複製一份。平台要改標準時每個 app 都要改，漏改一個不會報錯。
- **token 要手動建**，Vault 還沒上線。
- **租戶隔離沒有中央那一層把關。** vmauth 只驗 token，不檢查 `vm_account_id`；能改 vmagent 設定的人就能寫進任何租戶。
- **ArgoCD 那條路目前沒有 E2E。** `tests/routing` 還是舊分流的版本。
- **本機 E2E 不是正式環境**：單一 cluster、in-cluster destination、本機 git server、不經過中央 vmauth 的真實 tenant。
