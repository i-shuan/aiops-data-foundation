# aiops-data-foundation

每個 App 在自己的 `applications/<app>/helm/values.yaml` 宣告 observability identity。

部署時，這些資訊會成為 Kubernetes Namespace labels。

vmagent 只根據 Namespace 的 `group` label，把 metrics 送到對應 tenant。

```
values.yaml
    ↓  platform/applicationset.yaml（ArgoCD）
Namespace metadata.labels
    ↓  platform/vmstack-values.yaml
vmagent
    ↓  同一個 URL，不同 bearer token
vmauth
    ↓
IT / IMC / platform
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
  group: it                    # 必填：it / imc / it.imc / platform
  system: mes                  # 選填

image:                         # 以下是一般的 Helm values
  repository: nexus.aillm.spil.com.tw/docker/mes/mes-dispatch
  tag: "2.3.1"
```

## Namespace 最後的 labels

ApplicationSet 照原值抄過去，不查表、不推導：

```yaml
metadata:
  labels:
    dept: D3120
    group: it
    system: mes
```

沒寫 `system` 就不會有 `system` 這個 label，不會變成 `system=""`。

## group 怎麼分流

| group | IT | IMC | platform |
|---|---|---|---|
| `it` | ✓ | | |
| `imc` | | ✓ | |
| `it.imc` | ✓ | ✓ | |
| 沒有 / 不合法 | | | ✓ |

`it.imc` 是**抓一次、送兩份**：兩個 tenant 裡每一筆原始樣本的 timestamp 精確到毫秒都一樣。

vmagent 不知道 values.yaml、app 名稱、namespace 名稱，也沒有部門對照表，只看 namespace 的 `group`。tenant 由中央 vmauth 依 token 決定，這邊不碰。

## 新增一個 App

1. `services/<app>/` 放程式碼和 Dockerfile
2. 複製一份 `applications/<app>/helm/`，改 `values.yaml` 的 `argocd`、`observability` 和一般 values
3. `./tests/validate.sh` 過了再發 PR

`services/<app>` 和 `applications/<app>` 要同名。ApplicationSet 看到新的 `helm/values.yaml` 就會自己長出一個 Application，不用改任何平台設定。

## Tests

```bash
./tests/validate.sh       # 不需要 cluster
./tests/routing/run.sh    # 本機 k3d E2E，真的經過 ArgoCD
```

**validate.sh**：每個 values.yaml 的必填欄位、group 是否合法（允許的值從 `vmstack-values.yaml` 的 remoteWrite 推出，不另外維護清單）、namespace 不能被兩個 app 共用、chart 渲染得出來、namespace label 渲染結果跟 values.yaml 一致。內建的錯誤寫法（`imcc`、`it.imcc`、`it.platform`、欄位打錯…）一定要被擋下，否則腳本本身失敗。

**routing/run.sh**：五個情境（it、imc、it.imc、沒有 observability、不合法的 group）走完整條路。

```
正式    ArgoCD ← devops repo       vmagent → 中央 vmauth → IT / IMC / platform tenant
本機    ArgoCD ← 本機 git server   vmagent → 本機 vmauth → VMSingle it / imc / platform
```

ApplicationSet、vmagent、測試 app 的 values 都取自正式檔案，只換位址。有一個 namespace 會先建好再交給 ArgoCD，驗證既有的 namespace 也會被補上 label。

兩支都確認過正常時 exit 0、故意改壞時 exit 1。

E2E 的前提：

```bash
kubectl create ns argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/core-install.yaml
docker build --build-arg BASE_IMAGE=python:3.12-slim -t metrics-sim:test services/metrics-sim
k3d image import metrics-sim:test victoriametrics/vmauth:v1.148.0 -c zk
```

另外要有 VictoriaMetrics operator。

## Known limitations

- **log 還有一份 dept 對照。** `platform/fluent.conf` 依 `dept` 分流，自己寫著部門清單，而且目前只有 D3120、D1110 兩條：**D3130、D1120 的 log 會進 unassigned**（`yms-sim` 就是 D1120）。log 也不支援 `it.imc`。
- **dept 沒有清單可以比對**，打錯字 CI 抓不到。metrics 不受影響（看的是 group），但 log 會進 unassigned。
- **改組要改每個相關 app 的 values.yaml。** 沒有中央的部門對照表是刻意的，代價是部門換 group 時要一個一個改。production 還要等手動 sync 才會生效。
- **搬到 devops repo 時**：discovery 改成找 `applications/*/helm/values.yaml`，**只用 manifests、沒有 `helm/` 的 app 不會被發現**。其他 app 的 `app.yaml` 內容也要搬進各自的 `values.yaml`。`charts/app-common` 要發佈到內部 Helm repo，因為各 chart 的 `file://../../../charts/app-common` 只在這個 repo 裡有效。
- **token 要手動建**，Vault 還沒上線。
- **本機 E2E 不是正式環境**：單一 cluster、in-cluster destination、本機 git server、不經過中央 vmauth 的真實 tenant。

`charts/app-common` 不在上面的主線裡，但三個 app 的 chart 都靠 `file://` 引用它，刪掉就渲染不出來，所以保留。
