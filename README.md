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

## 已經在跑的 vmstack 加上分流

適用：cluster 已經用 Helm 裝好 victoria-metrics-k8s-stack（例如 AP1105 的 release `vmstack`），vmagent 目前只有一條 remoteWrite。要改成依 namespace 的 `group` 分三條，送到同一個 vmauth URL、各帶不同 token。

**用哪個檔**：`platform/vmagent-routing-overlay.yaml`，疊在該環境**自己現有的** values 上。

```
-f current-values.yaml                    ← helm get values 抓下來的，該環境原本的設定全部保留
-f platform/vmagent-routing-overlay.yaml  ← 只加 scrapeClasses、把 remoteWrite 換成三條
```

- ⚠ **不要套整份 `vmstack-values.yaml`**。它是另一套環境的完整 values，會關掉 vmcluster（刪掉 VMCluster）、把 image registry 換成 nexus、把 `externalLabels` 清成 `{}`（`cluster` 標籤消失）。
- ⚠ **platform 那把 token 要對到該環境現在寫入的 tenant**（AP1105 現在直接寫 `insert/0`）。沒貼 group 的 namespace（node-exporter、kube-state-metrics…）都走這條，對錯的話整個 cluster 的資料會換 tenant，看 tenant 0 的 Grafana / 告警會變空。
- overlay 的內容跟 `vmstack-values.yaml` 的那兩段一模一樣，只有 `url` 可以依環境改。`tests/validate.sh` 會檢查，也會擋下 overlay 裡出現其他設定。
- 本機用 chart 0.91.0 驗過：套上 overlay 之後，VMAgent 只有 `remoteWrite`、`scrapeClasses` 兩個欄位改變，remoteWrite 剛好三條（chart 不會再自動補一條到 `insert/0`），`externalLabels`、`extraArgs` 保留。

**先確認**（缺一個就不要往下）：

- vmauth 的 URL：`kubectl get vmauth,vmuser -A`、`kubectl get svc,ingress -A | grep -i vmauth`。跟 overlay 裡的不一樣就改 overlay 的三個 `url`。
- 三把 token 各對到哪個 tenant：問 vmauth 的管理者，不動對方設定。
- chart 版本和來源：`helm list -n vm-monitoring` 看現在的版本，`helm search repo victoria-metrics-k8s-stack --versions` 要找得到**同一個版本**。
- `helm get values vmstack -n vm-monitoring` 裡如果有 `vmagent.additionalRemoteWrites`，那些會變成第四條以後，要先處理。
- **VMAgent CRD 認不認得這兩個欄位**：`kubectl explain` 說不存在**不能當依據**。用 server-side dry-run 確認 `scrapeClasses`、`inlineUrlRelabelConfig` 不會被裁掉。CRD 是沒有這兩個欄位的舊版時**不能套**：過濾會被默默裁掉，所有資料外洩到每一個 tenant。判斷方法和指令見 [`tests/ap1105-sim`](tests/ap1105-sim/README.md) 發現 1。
- **最新一版是 failed（例如 post-upgrade hook 失敗）**：overlay 照樣會生效，只是 release 還是 failed。要修 hook 的話**不要直接關 syncJob**，它建的告警規則和 dashboard 會被一起刪掉，見 [`tests/ap1105-sim`](tests/ap1105-sim/README.md) 發現 2。

**步驟**（以 AP1105 為例，在 repo 根目錄）：

```bash
# 1. 備份，記下 REVISION
helm get values vmstack -n vm-monitoring -o yaml > current-values.yaml
kubectl get vmagent -n vm-monitoring vmstack-victoria-metrics-k8s-stack -o yaml > vmagent-before.yaml
helm history vmstack -n vm-monitoring

# 2. 先建 Secret，再改 vmagent（順序反了 vmagent 找不到 key，整個 cluster 的 metrics 會停）
#    read -s：token 不顯示、不進歷史，也不會多一個換行（多換行會一直 401）
read -s -p 'IT token: ' T_IT; echo; read -s -p 'IMC token: ' T_IMC; echo; read -s -p 'platform token: ' T_PF; echo
#    401 = vmauth 不認這把 token；其他數字 = 認得
for t in "$T_IT" "$T_IMC" "$T_PF"; do curl -sk -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $t" https://vmauth.aillm.spil.com.tw/prometheus/api/v1/write; done
kubectl create secret generic vmauth-token -n vm-monitoring --from-literal=it="$T_IT" --from-literal=imc="$T_IMC" --from-literal=platform="$T_PF"; unset T_IT T_IMC T_PF
kubectl describe secret vmauth-token -n vm-monitoring      # 三個 key，只顯示 byte 數

# 3. dry-run，確認只有 VMAgent 的 remoteWrite / scrapeClasses 在變（Helm 3.13 以前用 --dry-run）
helm get manifest vmstack -n vm-monitoring > manifest-before.yaml
helm upgrade vmstack <repo>/victoria-metrics-k8s-stack --version 0.91.0 -n vm-monitoring \
  -f current-values.yaml -f platform/vmagent-routing-overlay.yaml --dry-run=server > dry-run.txt
diff manifest-before.yaml <(sed -n '/^MANIFEST:/,/^NOTES:/p' dry-run.txt | sed '1d;$d')

# 4. 套用（vmagent 會重啟一次，幾秒沒抓）
helm upgrade vmstack <repo>/victoria-metrics-k8s-stack --version 0.91.0 -n vm-monitoring \
  -f current-values.yaml -f platform/vmagent-routing-overlay.yaml
```

dry-run 的 diff 看到 vmcluster、image、externalLabels 有變，或有資源被刪，就停下來。Grafana 密碼、webhook 憑證的差異是 chart 每次 upgrade 都會有的，不是這次造成的。

**驗證**（等 1～2 分鐘，vmagent 重新載入設定會延遲）：

```bash
kubectl port-forward -n vm-monitoring svc/vmagent-vmstack-victoria-metrics-k8s-stack 8429:8429   # 另一個分頁

curl -s localhost:8429/remotewrite-url-relabel-config                  # 三段：keep it / keep imc / drop (it|imc)
curl -s localhost:8429/metrics | grep -E '^vmagent_remotewrite_requests_total'   # 1: 2: 3: 都要有 2xx，不能有 401
```

接著用有貼 group 的 app 確認進對 tenant，例如 [`applications/eqp-collector`](applications/eqp-collector/README.md) 的 Step 6-8、6-9（`VMA_NS=vm-monitoring`、`VMA=vmstack-victoria-metrics-k8s-stack`）。也要確認 tenant 0 還有沒貼 group 的資料，`count(last_over_time(up[2m]))` 跟改之前差不多。

**回滾**：`helm rollback vmstack <步驟 1 記下的 REVISION> -n vm-monitoring`，回到單一條 `insert/0`。Secret 留著沒關係。

**之後**：分流只存在這個 release 的 values 裡。之後用 `--reuse-values` 或 `helm get values` + `-f` 升級都會保留；但如果有人只拿**當初那份** values 檔升級，分流會悄悄消失，全部回到單一 tenant。這份 overlay 要交給管 vmstack 的人。

## Tests

```bash
./tests/validate.sh       # 不需要 cluster
./tests/routing/run.sh    # 本機 k3d E2E，真的經過 ArgoCD
```

**validate.sh**：每個 values.yaml 的必填欄位、group 是否合法（允許的值從 `vmstack-values.yaml` 的 remoteWrite 推出，不另外維護清單）、`vmagent-routing-overlay.yaml` 的分流設定跟 `vmstack-values.yaml` 一致（url 除外）且不帶其他設定、namespace 不能被兩個 app 共用、chart 渲染得出來、namespace label 渲染結果跟 values.yaml 一致。內建的錯誤寫法（`imcc`、`it.imcc`、`it.platform`、欄位打錯…）一定要被擋下，否則腳本本身失敗。

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
- **搬到 devops repo 時**：discovery 改成找 `applications/*/helm/values.yaml`，**只用 manifests、沒有 `helm/` 的 app 不會被發現**。其他 app 的 `app.yaml` 內容也要搬進各自的 `values.yaml`。
- **app 的 JSON log 有 `time` 欄位，時間會變成 1970。** `platform/fluent.conf` 的 json parser 把字串 `"2026-09-22T..."` 當 epoch 秒數，只讀到 `2026` → `1970-01-01T00:33:46`。本機 argocd 的 log 全部在 `k8s-unassigned-1970.01.01`，照日期看 Kibana 看不到。app 的 log 先不要用 `time` 當欄位名。
- **`log` 欄位型別衝突會整筆拒收。** 不是 JSON 的行（Python traceback、coredns 警告）`log` 是字串；有 app 吐 ECS 格式（`log.level`）時同一天的 index 裡 `log` 變成 object，之後的純文字行全部 400 被拒，只在 Fluentd 自己的 log 看得到。本機 unassigned 已經發生（Elasticsearch 自己的 log 造成），IT / IMC 目前還沒有。
- **共用模板每個 app 各一份。** 標準 label、probe、VMServiceScrape 的模板（`helm/templates/_*.tpl`）每個 app 各複製一份。平台要改標準時每個 app 都要改，漏改一個不會報錯。
- **token 要手動建**，Vault 還沒上線。
- **本機 E2E 不是正式環境**：單一 cluster、in-cluster destination、本機 git server、不經過中央 vmauth 的真實 tenant。
