# AP1105 模擬

在本機 k3d 重現 AP1105 的 VictoriaMetrics 現況，照根目錄 README「已經在跑的 vmstack 加上分流」的步驟實際走一遍，確認在 AP1105 上照做會發生什麼事。

```bash
./tests/ap1105-sim/run.sh              # 從頭跑（會刪掉重建 k3d cluster ap1105），約 25 分鐘
ONLY=1 ./tests/ap1105-sim/run.sh routed # 只跑某一步
```

只用 `k3d-ap1105` 這個 context，不會切換你目前的 kubectl context。

## 重現了 AP1105 的哪些東西

| AP1105 | 模擬 |
|---|---|
| Helm release `vmstack`，rev 19 chart 0.86.2 deployed、rev 20 chart 0.91.0 **post-upgrade hooks failed** | rev 1 chart 0.86.2 deployed、rev 2 chart 0.91.0 post-upgrade hooks failed |
| hook 失敗的原因（推測）：sync-job 的 image 在 `ghcr.io`，`global.image.registry` 不會套用到它 | ghcr.io 故意擋掉（`registries.yaml` + `--disable-default-registry-endpoint`），rev 1 用的 v0.0.11 事先放在 node 上 |
| VMCluster 跟 vmagent 在同一個 cluster，vmagent 只有 chart 自動產生的一條 remoteWrite → `insert/0` | 一樣 |
| vmagent `externalLabels.cluster: ap1105`、`extraArgs.promscrape.maxScrapeSize: 64MB` | 一樣（[`vmstack-values.yaml`](vmstack-values.yaml)） |
| `kubectl explain vmagent.spec.scrapeClasses` → `field "scrapeClasses" does not exist` | 一樣 |
| eqp-collector：image 用 `ctr -n k8s.io images import` 匯入，`pullPolicy: Never`，Namespace 由 Helm 貼 label | 一樣，用 repo 的 `applications/eqp-collector/helm` |
| target `health=up`，但 `dept/group/system` 都是 None | 一樣 |
| 中央 vmauth：同一個 URL，依 bearer token 分 tenant | [`central-vmauth.yaml`](central-vmauth.yaml)：假 token，IT=2、IMC=1、platform=0，後端就是這套 VMCluster |

**不一樣的地方**（不影響分流的結論）：

- Kubernetes v1.30（本機是 cgroup v1，新版 kubelet 跑不起來），AP1105 是 v1.34.1。
- 沒有 Grafana、alertmanager、vmalert；VMCluster 每個元件 1 份。
- 沒有 `global.image.registry`，直接從各 registry 拉。
- Helm v4（AP1105 用的版本請 `helm version` 確認）。
- 只有一個 operator。AP1105 上如果真的有兩個 operator deployment，要另外確認（見下方指令）。

## 五個 eqp-collector

同一個 chart，只換 namespace 和 group：

| release | namespace | group | 預期 |
|---|---|---|---|
| eqp-collector | eqp-collector-dev | `imc` | 只進 IMC |
| eqp-it | eqp-sim-it | `it` | 只進 IT |
| eqp-shared | eqp-sim-shared | `it.imc` | IT + IMC，兩邊的原始樣本一模一樣 |
| eqp-none | eqp-sim-none | 沒有 | 只進 platform |
| eqp-typo | eqp-sim-typo | `it.imcc`（打錯） | 現在的 regex 以「點切開的段」比對，`it` 這段對到 → 只進 IT |

## 步驟

| 步驟 | 做什麼 |
|---|---|
| `cluster` | 建 cluster、擋 ghcr.io、匯入 image |
| `ap1105` | rev 1 → rev 2（hook failed），重現 AP1105 現況 |
| `diagnose` | `kubectl explain` 的結果、CRD schema、API server 會不會保留欄位、operator 版本 |
| `apps` | 中央 vmauth、5 個 eqp-collector；確認改之前 target 沒有 group |
| `routed` | **hook 還壞著**的狀態下照 README 套 overlay，驗證分流 |
| `rollback` | `helm rollback` 回套 overlay 前一版，確認回到單一 `insert/0` |
| `hookfix` | 先 orphan 掉 sync-job 的 ServiceAccount 再關 syncJob，確認 release 回到 deployed、告警規則和 dashboard 都留著 |
| `final` | hook 修好後再套一次 overlay，release 要是 deployed，分流要正確 |
| `oldcrd` | 模擬「CRD 是沒有這兩個欄位的舊版」：看 API server 和 vmagent 怎麼反應，再還原 |

## 結果（2026-09-22 本機實跑，全部 PASS）

**一句話**：分流設定在 AP1105 這種狀態（最新一版 failed、hook 壞著）下可以套、會生效，5 個情境全對。
`kubectl explain` 說欄位不存在**不能拿來判斷**。真正會出事的是兩件事：**舊版 CRD**、**直接關掉 syncJob**。

| 步驟 | 結果 |
|---|---|
| `ap1105` | rev 1 deployed → rev 2 `post-upgrade hooks failed: … sync-job not ready`（sync-job `ImagePullBackOff`），VMAgent 只有 `insert/0` 一條、沒有 scrapeClasses，跟 AP1105 一樣 |
| `diagnose` | 這份 CRD（chart 0.86.2 帶的）有完整 schema，118 個欄位，兩個欄位都有；operator v0.74.0 |
| `apps` | 改之前 5 個 target 都 `up`，dept / group / system 都是 None（跟 AP1105 一樣） |
| `routed` | dry-run：41 個資源只有 VMAgent 的 `remoteWrite`、`scrapeClasses` 變。套用後 rev 3 **還是 failed（hook）**，但 VMAgent 已經改好、規則載入、分流全對 |
| `rollback` | `helm rollback vmstack 2` 成功，回到單一 `insert/0`、沒有規則 |
| `hookfix` | 先 orphan sync-job 的 ServiceAccount 再關 syncJob：release 回到 deployed，VMRule 37 個、dashboard ConfigMap 41 個都留著 |
| `final` | 再套一次 overlay：rev 6 **deployed**，分流全對 |
| `oldcrd` | 舊 CRD 下，過濾被裁掉，**每一筆資料都進三個 tenant**，vmagent OOMKilled；換回 CRD 之後欄位不會自己回來，要再 `helm upgrade` 一次 |

分流結果（`routed` 和 `final` 都一樣）：

| namespace | group | IT | IMC | platform |
|---|---|:---:|:---:|:---:|
| eqp-collector-dev | `imc` | | ✓ | |
| eqp-sim-it | `it` | ✓ | | |
| eqp-sim-shared | `it.imc` | ✓ | ✓ | |
| eqp-sim-none | 沒有 | | | ✓ |
| eqp-sim-typo | `it.imcc` | ✓ | | |

- `it.imc`：IT、IMC 兩邊同一段時間的原始樣本，timestamp（毫秒）和值完全一樣。
- 沒貼 group 的 namespace（kube-system、vm-monitoring、default、node 層級）只在 tenant 0。

## 發現 1：`kubectl explain` 說不存在，不代表不能用

VMAgent 的 CRD 有三種狀況，其中 A、C 的 `kubectl explain` 都會說 `field "scrapeClasses" does not exist`：

| CRD | 怎麼認 | `explain` | 套 overlay 會怎樣 |
|---|---|---|---|
| **A. 不宣告欄位、全部照收**（`preserve-unknown-fields: true`） | 下面指令 1 印出 `true` | 永遠說不存在（假警報） | 欄位照收，operator v0.74.0 會用。本機 zk cluster 就是這種，分流實測正常 |
| **B. 有完整 schema，而且有這兩個欄位** | 指令 1 空白、指令 2 印出 `probe` | 找得到 | 正常（這次模擬就是這種） |
| **C. 有完整 schema，但是舊版，沒有這兩個欄位** | 指令 1 空白、指令 2 印出 `Warning: unknown field "spec.scrapeClasses"`、值是空的 | 說不存在（是真的） | **❌ 不能套**：過濾被 API server 默默裁掉，三條 remoteWrite 都不過濾，**所有資料外洩到 IT、IMC、platform 每一個 tenant**，vmagent 記憶體變三倍被 OOMKilled |

在 AP1105 上照順序跑（都是唯讀，第 2 條是 server-side dry-run，不會真的改）：

```bash
# 1. CRD 是不是「全部照收」
kubectl get crd vmagents.operator.victoriametrics.com -o jsonpath='{.spec.versions[?(@.name=="v1beta1")].schema.openAPIV3Schema.properties.spec.x-kubernetes-preserve-unknown-fields}{"\n"}'

# 2. API server 會不會保留欄位
kubectl -n vm-monitoring patch vmagent vmstack-victoria-metrics-k8s-stack --type=merge --dry-run=server \
  -p '{"spec":{"scrapeClasses":[{"name":"probe"}]}}' -o jsonpath='{.spec.scrapeClasses[*].name}{"\n"}'
kubectl -n vm-monitoring patch vmagent vmstack-victoria-metrics-k8s-stack --type=merge --dry-run=server \
  -p '{"spec":{"remoteWrite":[{"url":"http://probe","inlineUrlRelabelConfig":[{"action":"keep","source_labels":["group"],"regex":"probe"}]}]}}' \
  -o jsonpath='{.spec.remoteWrite[0].inlineUrlRelabelConfig[0].regex}{"\n"}'

# 3. 有幾個 operator、各是什麼版本（你說有兩個 operator deployment，要一起看）
kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}  {.spec.template.spec.containers[*].image}{"\n"}{end}' | grep -i 'victoriametrics/operator'
```

- 指令 1 是 `true`（情況 A）時，指令 2 **連打錯字的欄位都會保留**，只能證明「沒被裁掉」，不能證明 operator 看得懂。這時要看指令 3 的 operator 版本（v0.74.x 可以），套完再用 `/remotewrite-url-relabel-config` 和 target 有沒有 `group` 確認。
- 情況 C 要先升級 CRD 才能套。另外：**舊 CRD 期間只要 VMAgent 被寫過一次，欄位就永久消失**，CRD 換回來也不會自己長回來，要再 `helm upgrade` 一次（`oldcrd` 步驟實測）。
- 兩個 operator 同時管同一隻 VMAgent 的話，版本舊的那個可能看不懂新欄位、把設定改回去。要先確認只有一個在管。
- `kubectl explain` 有快取（大約 10 分鐘），剛換過 CRD 時結果也不準。

## 發現 2：hook 失敗跟分流是兩件事，但不要直接關 syncJob

- rev 20 失敗的原因很可能是 sync-job：image 在 `ghcr.io`（`global.image.registry` 不會套用到它），而且它執行時還要上網抓 dashboard / rule。在 AP1105 確認：

  ```bash
  kubectl get jobs,pods -n vm-monitoring | grep sync-job
  kubectl describe pod -n vm-monitoring -l job-name=vmstack-victoria-metrics-k8s-stack-sync-job | grep -iE 'image|failed|back-off' | head
  ```

- **hook 壞著也能套 overlay**：資源在 post-upgrade hook 之前就套上去了，分流會生效，只是 release 又是 failed（`routed` 步驟實測）。
- **不要直接 `syncJob.enabled=false`**。sync-job 建的 VMRule 和 dashboard ConfigMap 的 owner 是它的 ServiceAccount，關掉時 Helm 刪掉 ServiceAccount，garbage collector 會把它們**全部刪掉**。第一次模擬實測：VMRule 37 → 0。AP1105 有在跑 vmalert 和 Grafana，等於告警規則和 dashboard 一起消失，而且沒有任何錯誤訊息。
- 要關的話，先 orphan 掉 owner（`hookfix` 步驟實測，VMRule 37、ConfigMap 41 都留著）：

  ```bash
  kubectl -n vm-monitoring delete serviceaccount vmstack-victoria-metrics-k8s-stack-sync-job --cascade=orphan
  helm upgrade vmstack <repo>/victoria-metrics-k8s-stack --version 0.91.0 -n vm-monitoring \
    -f current-values.yaml --set syncJob.enabled=false
  # hook 的 Job 不算 release 的資源，舊的那個會一直重試拉 image，手動刪掉
  kubectl -n vm-monitoring delete job vmstack-victoria-metrics-k8s-stack-sync-job
  ```

  代價：之後 dashboard 和預設規則不會再跟著 chart 升級更新。另一條路是把 sync-job 的 image 放進 artifacts proxy、設 `syncJob.image.repository`，但它執行時還要能上網。

## 發現 3：其他要知道的

- **最新一版是 failed 時**，`helm get values`（不加 `--revision`）拿的是最新那版（rev 20）的 values，跟現在跑著的東西一致；`--reuse-values` 拿的是**最後一次 deployed**（rev 19）的。README 的步驟用前者，不要換成 `--reuse-values`。
- **分流看的是「被抓的 target 在哪個 namespace」**，不是 metrics 講的是誰。kube-state-metrics 在 vm-monitoring（沒貼 group），所以 `kube_pod_info{namespace="eqp-collector-dev"}` 這類 Pod 狀態在 platform，**IMC tenant 看不到**，IMC 只看得到 app 自己吐的 metrics。
- **tenant 0 既有的資料會換一批 series**：platform 那條會加上 `group="platform"`，改完之後同一個 target 在 tenant 0 變成新的 series，舊的那條停在切換時間點。實測 kube-system 的 core-dns 在 tenant 0 有兩條 `up`：一條沒有 group（切換前）、一條 `group="platform"`（切換後）。圖表上會看到線斷一下再接上；用 `absent()` 或依 series 身分的告警要注意。
- **打錯字的 group**：現在的 regex 以「點切開的段」比對，`it.imcc` 會進 IT（`it` 那段對到），不會進 platform。要打錯就一律進 platform，得改成完整值比對（例如 IT 用 `^(it|it\.imc)$`）；代價是每多一個 tenant，所有組合都要列進三條 regex。`tests/validate.sh` 已經會擋下 `it.imcc`，但 AP1105 手動 helm 不會經過它。
- **只有模擬才有的問題**：本機是 cgroup v1、20 顆 CPU，VMCluster 不設 CPU limit 會照 20 顆開平行度，256Mi 就被 OOMKilled；rollback 之後三條 queue 一起補送，vminsert 要 512Mi。這些設定只在 `vmstack-values.yaml`（模擬用），跟 AP1105 無關。
