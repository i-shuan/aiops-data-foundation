# 歸屬：資料怎麼知道自己是哪一組的

一句話：**中央維護一份對照表，由 CronJob 自動把 `dept` / `group` 標到 namespace 上，收集端照舊只讀 label。**

設定在 [../platform/cluster-addons/ns-labeler/](../platform/cluster-addons/ns-labeler)，已經在本機 k3d 實測通過。

## 為什麼需要這一層

vmagent 過濾時只看得到 label，Fluentd 改 tag 時只看得到欄位。但 namespace 名稱是 `{cluster}-{app}-{env}` 或 `{app}-{env}`，**看不出是哪個組織的**。所以歸屬必須額外生出來。

兩種生法各有致命傷：

| 做法 | 致命傷 |
|---|---|
| 每個 namespace 自己標 label | 別人有權限就能改掉，改組要去改幾十個 namespace |
| 平台維護一份中央清單，收集端查表 | 每上一個新 app 就要改設定、重啟收集器，跟「擴廠不能全人工」衝突 |

所以兩個都用：**清單當來源，label 當機制。**

![對照表放哪、labeler 跑在哪](architecture/attribution-where-things-run.svg)

**對照表一份在 git，labeler 每個 cluster 各跑一份。** 這點是整張圖的重點：namespace label 是 cluster 內的物件，central 改不到 zk 或 el 的 namespace，所以不可能有一台中央的機器統一去標。central 自己也要跑一份，它沒有特權。

擴廠時要做的只有兩件事：在新 cluster 部署一份 labeler、在它的環境變數加上新廠代號。三個 tsv 的內容不用動。

好處是收集端設定不動、人只改一個地方、別人手動改 label 會被對帳蓋回去。代價是多一個 CronJob，而且它要有改 namespace label 的權限。

## 三個對照表

| `values.yaml` 裡的 | 內容 | 什麼時候改 |
|---|---|---|
| `registry.apps` | app → 部門、系統 | 新 app 上線 |
| `registry.namespaces` | namespace → 部門、系統（例外、平台自己的 namespace） | namespace 名稱不照命名規則時 |
| `registry.groups` | 部門 → group | **改組**。大多數情況只改這一份 |

`registry.apps` 的 key 是**「namespace 名稱去掉 cluster 前綴和 env 後綴之後剩下的那一段」，不是 Deployment 的名字**：

```
zk-mes-prod    → 去掉 zk- 和 -prod → mes
wip-staging    → 去掉 -staging     → wip
```

cluster 代號和 env 的清單寫在 CronJob 的環境變數，擴廠時在那裡加新廠代號。

`registry.namespaces` 的優先順序高於 `registry.apps`，所以名稱亂掉的 namespace 可以直接指定，不用為了它去改命名規則。

## 這跟 app 的 CI/CD 是兩條，不要混在一起

![兩條 pipeline](architecture/two-pipelines.svg)

app 的 pipeline 是 build image → deploy。**平台這條沒有 build image 這一段**：要進 cluster 的只有三個文字檔和一份 YAML，CronJob 用的還是現成的公開映像，沒有任何東西要打包。所以它只有一個 stage——把 ConfigMap apply 到每一個 cluster。

兩條 pipeline 唯一的交會點是同一個 namespace 物件：一條把它建出來，另一條在上面標歸屬。**兩邊不需要互相知道對方存在**，這也是 app 團隊不必知道 tenant / group 的原因。

### 順序：平台那條要先跑完

新 app 上線的正確順序是：**先在 `registry.apps` 加一行合進去 → 等對帳跑過 → app 再部署**。

反過來的話，app 先上線、tsv 後補，中間那段時間的 metrics 和 log 會進 unassigned。**補登記之後不會搬回來**——資料早就寫進別的 tenant 和 index 了，那段歷史就是錯的。之後 AIOps 學基準線會學到錯的資料。

所以 app 的 pipeline 要多一個部署前檢查，把這個會被忽略的靜默錯誤變成擋得住的部署失敗：

```bash
# 放在 deploy 之前。app 團隊不用知道 group 是什麼，只要知道「還沒登記就先去找平台」。
dept=$(kubectl get ns "$TARGET_NS" -o jsonpath='{.metadata.labels.dept}' 2>/dev/null || true)
if [ -z "$dept" ]; then
  echo "namespace $TARGET_NS 還沒有歸屬，資料會進 unassigned 而且事後救不回來。"
  echo "請平台團隊在 ns-labeler 的 values.yaml（registry.apps）補一行後再部署。"
  exit 1
fi
```

## 部署：掛在 cluster-addons 底下，不要另外做一條管道

ns-labeler 要進每一個 cluster，而 **cluster-addons 的 ApplicationSet 已經是現成的分發管道**（list generator 跑 central / zk / el）。把它當成 cluster-addons 的一個元件，不用另外做。

這樣一來，新廠開起來時 list 加一行 `- cluster: <新廠>`，那個 cluster 同時拿到 vmagent 分流設定、ns-labeler、Fluentd、KSM allowlist，**一行全部到位**。

CronJob 的 `CLUSTERS` 環境變數也用同樣的 `helm.parameters` 注入，跟 `externalLabels.cluster` 一模一樣的機制，不用手動維護每廠一份。

### 它已經是一個 chart

對照表放在 **`values.yaml`**，由模板渲染成 ConfigMap 裡的三個 tsv 檔。

```
platform/cluster-addons/ns-labeler/
  Chart.yaml
  values.yaml                     ← 對照表在這裡，人改這裡
  scripts/label-namespaces.sh
  templates/{configmaps,rbac,cronjob,vmrule}.yaml
```

**為什麼對照表放 values 而不是包進 chart**：你們 `cluster-addons` 的驗證規則要求 `helm.repoURL` / `chart` / `version` / `valueFiles` 齊全，**不支援 git 內的本地 chart**，所以 chart 必須發佈到內部 Helm repo 並釘選版本。對照表若包在 chart 裡，改一個部門歸屬就要發一版新 chart；放 values 的話它留在 devops repo 的 git 裡走 PR，chart 版本不用動。

本機驗證用：

```bash
helm upgrade --install ns-labeler platform/cluster-addons/ns-labeler   -n vm-monitoring --set image.registry=docker.io
```

正式環境不要手動裝，掛進 cluster-addons 讓 ApplicationSet 去部。

**第一次上線先把 `dryRun` 設成 `true`**，跑幾輪看它想動什麼，確認都是預期內的再改 `false`。

不用等排程可以手動跑一次：

```bash
kubectl create job --from=cronjob/ns-labeler ns-labeler-now -n vm-monitoring
kubectl -n vm-monitoring logs job/ns-labeler-now
```

### 放進 devops repo 的樣子

chart 發佈到內部 Helm repo 之後，devops repo 裡只要兩個檔案：

```
cluster-addons/ns-labeler/
  app.yaml      ← ArgoCD 的設定，範例見 chart 目錄下的 app.yaml
  values.yaml   ← 對照表在這裡
```

`app.yaml` 的欄位照 `scripts/validate_application.py cluster-addons` 的規則：必填 `namespace` / `owner` / `criticality` / `helm`，`helm` 的四個子欄位要齊全、`version` 要釘選。

**不需要 `values-<cluster>.yaml`**：對照表和 `clusters` 清單全廠共用，沒有 per-cluster 差異。

⚠ **要先確認內部 Helm repo 放不放得了自建 chart。** 文件裡那個位址名字是 `artifacts-proxy`，proxy（read-through）型的倉庫只能代理外部 chart，放不了自己的。若是這樣就要另外開一個 hosted 倉庫，這會擋住 ns-labeler 落地。

### values 裡幾個要注意的

| 項目 | 說明 |
|---|---|
| `image` | **必須是含 shell 的映像**，而且要鏡像進 Nexus。預設寫的是 `alpine/k8s`，你們環境拉不到 |
| `clusters` | 是**全部 cluster 的代號清單**，不是這一台所在的 cluster。所以不需要像 `externalLabels.cluster` 那樣由 ApplicationSet 注入，全廠共用一份就好 |
| `vmrule.enabled` | **預設 false。** 各 cluster 的 vmalert 是關的，在那邊建了也沒人評估，告警永遠不會響。這條規則要部在中央有 vmalert 的那一側 |

## 本機實測結果

**環境**：k3d 單節點、`alpine/k8s:1.30.0`、用 chart 裝在 `vm-monitoring`。

**結果**

| namespace | 來源 | 標上的 label |
|---|---|---|
| `zk-mes-prod` | `registry.apps` 的 `mes` | `dept=D3120` `group=it` `system=mes` |
| `zk-wip-prod` | `registry.apps` 的 `wip` | `dept=D1110` `group=imc` `system=wip` |
| `kube-system`、`vm-monitoring` 等 | `registry.namespaces` | `dept=PLATFORM` `group=platform` `system=platform` |
| `zk-unknownapp-prod` | 對不到 | **故意不標**，落到 unassigned 並列在報告裡 |

**確認了幾件事**

1. **名稱解析正確。** `zk-mes-prod` 去掉 cluster 前綴和 env 後綴後對到 `mes`，即使 app 名稱本身含有 `-` 也不會被切壞。
2. **重跑不會亂動。** 第二次執行報「已正確 8 個，本次調整 0 個」，只在值不同時才送 patch。
3. **改組只改一行。** 把 `registry.groups` 裡 `D1110` 從 `imc` 改成 `it`，下一次執行只動 `zk-wip-prod` 一個 namespace，其他七個不受影響。
4. **改對照表的完整迴路可用。** 加一筆對照 → `helm upgrade` → 下一次執行就把那個 namespace 標上，「對不到歸屬」的清單清空。

對照表從 tsv 檔搬進 `values.yaml`、並加上 `system` 欄之後，已在 zk 上用 `helm upgrade` 重跑過，額外確認了：

5. **`system` 跟著一起標上，而且不動到既有的兩個 label。** 第一輪 8 個 namespace 全部補上 `system`，第二輪「已正確 8 個，本次調整 0 個」。
6. **`system` 缺席時不擋分流。** 只給 `dept` 不給 `system` 的 namespace，`dept` / `group` 照標，log 裡多一行 `WARN ... 沒有 system`，不會因此被當成沒有歸屬。
7. **metrics 端**：`up` 上出現 `system`（`zk-mes-prod` → `mes`、`vm-monitoring` → `platform`）。把 namespace 的 `system` 拿掉，series 上就**沒有這個 key**，不是空字串，也不是 unknown。
8. **log 端**：ES 裡新進的文件都帶 `system`：`group=it` → `mes`、`group=imc` → `wip`、`group=unassigned` → `platform`。

**沒有單獨驗的**：kube-state-metrics 的 allowlist 加了第三個 `system`。格式跟已驗證的 `[dept,group]` 相同，而且目前沒有任何告警或查詢用到 `label_system`，所以沒有為它重裝一次 KSM。

## 對不到歸屬要有告警，不然就是靜默出錯

**這是整套設計唯一的失效模式**：新 app 沒加進 `registry.apps`，資料就安安靜靜進 unassigned，不會有任何錯誤。

CronJob 的 log 會列出對不到的 namespace，但沒人會每十分鐘去看。要變成告警，靠 kube-state-metrics 的 `kube_namespace_labels`（`vmstack-values.yaml` 已經把 `dept`、`group`、`system` 放進 allowlist）：

```yaml
- alert: NamespaceMissingAttribution
  expr: kube_namespace_labels{label_dept=""} == 1
  for: 30m
  annotations:
    summary: "{{ $labels.namespace }} 沒有歸屬，資料正在進 unassigned"
    description: "在 ns-labeler 的 values.yaml 的 registry.apps 補一行，或在 registry.namespaces 標成例外。"
```

`for: 30m` 是為了跳過新建 namespace 到下一輪對帳之間的空窗（排程是 10 分鐘）。

**已實測。** 本機裝了 kube-state-metrics 2.20.0，這條查詢精準回傳唯一那個沒有歸屬的 `zk-unknownapp-prod`，其他八個都不在結果裡。沒有歸屬的 namespace 在 `kube_namespace_labels` 上是**整個 `label_dept` 欄位不存在**（不是空字串），`label_dept=""` 在 PromQL 語意上同時涵蓋這兩種情況，所以寫法是對的。

### 但踩到一個會讓告警指錯對象的坑

第一次查出來長這樣：

```
kube_namespace_labels{namespace="vm-monitoring", exported_namespace="zk-unknownapp-prod", ...}
```

`namespace` 變成 **kube-state-metrics 自己所在的 namespace**，KSM 回報的那個被改名成 `exported_namespace`。原因是抓取時 vmagent 會蓋上 target 自己的 `namespace`，跟 KSM 指標裡本來就有的 `namespace` 撞名，撞到的那個就被加上 `exported_` 前綴。

**後果是告警訊息會指著 `vm-monitoring`**，也就是監控自己的 namespace，而不是真正出問題的那個——告警會響，但寫的是錯的，排查時會被帶去完全錯誤的方向。

解法是 KSM 的 scrape 要開 `honorLabels`，讓指標自己帶的 label 贏：

```yaml
endpoints:
  - port: http
    honorLabels: true    # 沒有這行，namespace 會被換成 KSM 自己的
```

開完重查，`namespace` 就是 `zk-unknownapp-prod` 了。**上線前先確認你們現有的 KSM scrape 物件有沒有開這個**；`victoria-metrics-k8s-stack` 裝出來的通常有，但值得查一次：

```bash
kubectl get vmservicescrape -A -o yaml | grep -B3 honorLabels
```

另外，這個指標上會同時出現 `dept` 和 `label_dept` 兩個欄位，意思完全不同：`dept` 是抓取時蓋上的「KSM 這個 pod 屬於誰」（永遠是 platform），`label_dept` 才是「這一列講的那個 namespace 屬於誰」。**告警條件一定要用 `label_dept`。**

## 坑

- **映像必須有 shell。** `rancher/kubectl` 裡面只有 `kubectl` 一個執行檔，掛腳本進去會 `StartError: exec "sh": executable file not found`，而且訊息不會告訴你原因是映像太精簡。
- **根檔案系統唯讀時要給 `HOME`。** kubectl 會想寫 `~/.kube/cache`，沒有可寫的 HOME 會出警告。manifest 裡設了 `HOME=/tmp` 並掛了一個 emptyDir。
- **`dept` 和 `group` 是很通用的 label 名稱**，別的團隊可能已經拿來用在別的地方。上線前先查一次：`kubectl get ns -L dept,group`。要保險就改成 `observability.spil.com.tw/dept` 這種有命名空間的 key，但收集端的設定要跟著改。
- **這個 CronJob 有改全 cluster namespace label 的權限。** 權限只有 `get`/`list`/`patch` namespace，碰不到 namespace 裡面的東西，但對帳邏輯寫錯就會把整廠的歸屬標錯，所以對照表要走 code review。
- **app 名稱必須在同一個 cluster 內唯一。** 跨廠不用唯一（各廠各自一份 ConfigMap），但同一廠內兩個不同組織的 app 撞名就會標錯。
- **從清單移除的 app，舊 label 不會被清掉。** 對帳只補不刪，避免誤刪。下線的 namespace 要手動處理。

## 想再加 label 之前

現在標三個：`dept`、`group`、`system`。**再加之前先過這幾關**，因為每一個 label 都是每筆 metrics、每筆 log 一輩子帶著走的。

### 技術上限不是問題

Kubernetes 對 namespace 的 label 數量沒有硬性上限（整個物件 ≤ 1.5MB），key 名稱 ≤ 63 字元、value ≤ 63 字元。**真正的上限是人維護得動幾個。**

### 加一個 label 要改四個地方，漏一個欄位就對不起來，而且不會報錯

| 改哪 | 改什麼 |
|---|---|
| `ns-labeler/values.yaml` | 對照表多一個欄位 |
| `ns-labeler/scripts/label-namespaces.sh` | 讀那一欄、加進 `kubectl label` |
| `vmstack-values.yaml` | scrapeClass 多一段 relabel；KSM 的 `namespaces=[...]` allowlist 多放一個 |
| `fluent.conf` | `record_transformer` 多一個欄位，**一定要給預設值** |

log 端的 label key **不會被改寫，點還是點**，用底線取值會得到 `null`。沒給預設值的話，沒標 label 的 namespace 那個欄位是 `null`，ES 對 `null` 的查詢行為跟空字串不同，關聯分析會漏掉。

### 不可逆：label 是寫入當下焊進資料的

今天標 `dept=D3120`，明年改組，**去年的資料永遠是 D3120**。

| | 舊資料能不能修 |
|---|---|
| metrics（VictoriaMetrics） | **不行。** 已寫入的 series 改不了 label，而且改組後舊資料在另一個 tenant 裡，等於做不到 |
| log（Elasticsearch） | 欄位能用 `_update_by_query` 改，但文件落在哪個 index 搬不動，要 reindex；ILM 轉唯讀之後連 update 都不行 |

所以**會變的東西不要標**（負責人姓名、專案代號）。

成本也不對稱：metrics 的 label 是每條 series 一份，log 是**每一筆文件一份**。一天一億筆的話，多一個十個字元的欄位就是每天多幾 GB，log 那邊的帳要算得比 metrics 嚴。

### 哪些值得、哪些不值得

| 值得 | 為什麼 |
|---|---|
| `system` | AIOps 要問「MES 整體有沒有問題」，不用一個個 app 看。**已加** |
| `criticality` | 告警分級，tier1 半夜叫人、tier2 早上再說 |
| `oncall` | 告警直接路由到值班群組，不用查表 |

| 不值得 | 為什麼 |
|---|---|
| `env` | namespace 名稱裡已經有 |
| `app` / `version` | 那是 **pod** 層級的，標在 namespace 上會錯（一個 namespace 可能多個 app） |
| `cluster` | vmagent 的 `externalLabels` 已經有 |

**上限抓四個**：`dept`、`group`、`system`，再留一個給 `criticality`。再多就會出現「這個 label 半年沒人用，但每筆資料都在佔空間」。

### `system` 跟既有的 `part_of` 是同一件事的兩個來源

metrics 上本來就有 `part_of`（`app-common` 從 pod 的 `app.kubernetes.io/part-of` 帶出來）。兩者講同一件事，差別在來源：

| | `system` | `part_of` |
|---|---|---|
| 來源 | namespace label，由 ns-labeler 標 | pod label，由 `app-common` 產生 |
| 還在用舊 manifest 的 app | **有** | **沒有**（沒照標準寫） |
| log 上 | **有** | 沒有（`fluent.conf` 沒有攤平這個） |

所以**分析用 `system`**：舊 manifest 的 app 也涵蓋得到，log 和 metrics 都有。`part_of` 留作交叉檢查，兩者對不上時值得查一下是不是有人把 pod 放錯 namespace。

## 之後可以再簡化：讓收集端直接讀 `group`

現在 namespace 上已經有 `group` label，但收集端還是照舊用部門 regex 在算 group，等於「部門 → group」這份對照在三個地方各有一份（`registry.groups`、vmagent values、`fluent.conf`）。

改成直接讀 `group` 之後，**新增部門就完全不用碰收集端設定**：

vmagent 的 scrapeClass 多帶一個 label，remoteWrite 的條件換掉：

```yaml
relabelConfigs:
  - source_labels: [__meta_kubernetes_namespace_label_group]
    target_label: group
# remoteWrite 的過濾條件
  - action: keep
    source_labels: [group]
    regex: it          # 原本是 dept 的 "D3120|D3130"
```

Fluentd 的 `rewrite_tag_filter` 同理，`key dept` 換成 `key group`、pattern 換成 `/^it$/`。

沒有現在就做，是因為現有設定已經實測過，換掉要重測一輪。想做的時候這是兩邊各兩行的改動。

## 待確認

1. **`dept` / `group` 這兩個 label 名稱在正式環境有沒有被別人用掉。**
2. **映像要能從 Nexus 拉得到。** 現在寫的是 Docker Hub 的 `alpine/k8s:1.30.0`，你們環境拉不到，要先鏡像進 `nexus.aillm.spil.com.tw/docker`，或換成內部已有、含 shell 和 kubectl 的映像。**這一條會直接擋住落地。**
3. **`registry.apps` 由誰維護、走什麼流程。** 建議跟著 app 上線的 PR 一起改，不要另外開一個流程，否則一定會有人忘記。
4. **cluster 代號和 env 的清單寫死在 CronJob 環境變數**，擴廠時要記得加。要不要改成從對照表讀還沒決定。
