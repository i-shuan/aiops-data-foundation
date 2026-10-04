# eqp-collector：機台收值程式（手動 Helm 部署）

> **一句話**：把一支模擬的機台收值程式部署到 Kubernetes，確認它產生的數據會照 namespace 上的 `tenant` 標籤，**只**寫進 IMC 的租戶，不會跑到別的部門或平台的租戶。

這台測試機沒有 ArgoCD，所以用 Helm 手動部署。資料分流用的是**平台現有的 vmagent 和中央 vmauth**，這個資料夾不另外建任何一套分流設定。

## 怎麼讀這份文件

| 你是 | 看這幾節 |
|---|---|
| 想知道這在做什麼（主管、非技術同仁） | [1](#1-這次要證明什麼)、[2](#2-一張圖看懂) |
| 要動手測試的人 | [3](#3-名詞小辭典) → [6](#6-操作步驟)，出問題看 [8](#8-出問題時) |
| 平台 / 維運 | [9](#9-為什麼這樣設計)、[10](#10-跟其他-app-的差別) |

---

## 1. 這次要證明什麼

每個 app 產生的數據（metrics），要送到「看得到它的那個部門」的資料庫（tenant）。規則是：

- **App 不知道**自己的數據要送去哪，也不拿任何密碼（token）。
- App 只在設定檔寫一個標籤 `tenant`，部署時貼在它的 namespace 上。
- **平台的 vmagent 看標籤**，決定寫進哪個租戶。

這個資料夾用一支真的程式把整條路走一遍。改 `values.yaml` 的 `tenant`，預期結果如下：

| `tenant` 寫 | 寫進哪個租戶 | AccountID |
|---|---|---|
| `imc`（**第一輪就測這個**） | IMC | 1004 |
| `tsid` | TSID | 1001 |
| `bsid` | BSID | 1002 |
| `icsd` | ICSD | 1003 |
| `platform`、沒寫、或寫了不認得的值 | platform（兜底） | 1000 |

一個 namespace 只能寫一個租戶。以前的 `it.imc`（一份資料送兩個部門）不再支援。

---

## 2. 一張圖看懂

```
① values.yaml
     你只改這一個檔。裡面寫著 tenant: imc
        │
        │  helm upgrade --install
        ▼
② Namespace eqp-collector-dev
     門牌：dept=D1110  tenant=imc  system=eqp
        │
        ├──▶ ③ Pod eqp-collector
        │        每 10 秒模擬向 EQP001、EQP002、EQP003 收值
        │        在 :9464/metrics 吐出數字，在畫面（stdout）印出 JSON log
        │
        ├──▶ ④ Service eqp-collector
        │        給 :9464 一個固定的名字，Pod 重啟換 IP 也找得到
        │
        └──▶ ⑤ VMServiceScrape eqp-collector
                 登記一下：「這個 Service 的 /metrics 要來抓」
        │
        ▼
⑥ 既有的 vmagent（平台的，不是這次建的）
     每 30 秒來抓一次，順便把門牌上的 dept / tenant / system 貼到每筆數據上
     送出前照對照表把 tenant 換成租戶編號：imc → 1004；對不到的一律當成 platform → 1000
     同一份資料往中科站、二林站各送一份，各帶各的 token
        │
        ▼
⑦ 中央 vmauth（別人的，不動）
     看 token 確認是合法的 cluster，放行
        │
        ▼
   中央 VictoriaMetrics 看每筆數據上的租戶編號，放進對應的租戶
   IMC（1004）✓      其他部門 ✗      platform（1000）✗
```

### 用比喻說

| 真的東西 | 比喻 |
|---|---|
| Namespace | 一間辦公室 |
| Namespace 上的 label | 辦公室門口的**門牌** |
| eqp-collector（程式） | 辦公室裡的人，每 10 秒寫一張報表，貼在門口 |
| VMServiceScrape | 在收發室登記「這間有報表要收」 |
| vmagent | **收發室**：定時去每間登記過的辦公室收報表，看門牌在每張報表上蓋部門編號 |
| token | 收發室的通行證（每個 cluster 一張），證明這批報表是哪個 cluster 送來的 |
| vmauth / tenant | 中央收件處 / 各部門的保險箱 |

**重點**：辦公室裡的人（程式）完全不知道報表會送去哪。要改送去哪，只要換門牌（改 `values.yaml` 的 `tenant`），程式一行都不用動。

---

## 3. 名詞小辭典

| 名詞 | 白話 |
|---|---|
| Kubernetes（k8s） | 一群機器組成的平台，負責把程式跑起來、掛了自動重啟 |
| node | Kubernetes 裡的一台機器。程式實際跑在某一台 node 上 |
| Namespace | Kubernetes 裡的一個隔間。這個測試一個程式一個隔間 |
| label | 貼在東西上的標籤，`key=value` 的形式，例如 `tenant=imc` |
| image | 程式連同執行環境打包成的檔案。`equipment-collector:test` 是名字:版本 |
| tar | image 匯出成的單一檔案，可以複製到別台機器再匯入 |
| containerd / Docker / CRI-O | node 上真正把 image 跑起來的軟體。不同軟體匯入 image 的指令不同 |
| Pod | 跑起來的程式本體 |
| Deployment | 「要一直有 1 個 Pod 在跑」的設定，Pod 掛了會自動補 |
| Service | 給 Pod 一個固定名字和 port，別人用這個名字找它 |
| Helm / chart | Helm 是部署工具；chart 是一包部署設定（本資料夾的 `k8s/`） |
| values.yaml | chart 的設定檔，**這次唯一需要改的檔** |
| metrics | 程式定時吐出的數字，例如溫度、收值成功次數 |
| vmagent | 平台的收集程式：去各程式抓 metrics，再依 label 轉送 |
| VMServiceScrape | 告訴 vmagent「這個 Service 要抓」的登記單 |
| remoteWrite | vmagent 把資料送出去的那條線。平台設兩條：中科站、二林站各一條，送的是同一份。租戶靠每筆數據上的編號分 |
| vmauth | 中央的門口，看 token 確認是哪個 cluster 送來的 |
| tenant | 一個部門專屬的資料庫空間，彼此看不到 |
| token | 這個 cluster 寫入中央用的密碼，每個 cluster 一把。**程式和這個 chart 都不碰 token** |

---

## 4. 資料夾裡有什麼

repo 裡（`app/eqp-collector/`）：

```
app/eqp-collector/
├── README.md                     這份說明
├── main.py                       程式本體
├── Dockerfile                    怎麼包成 image
├── requirements.txt
└── k8s/                          Helm chart：一包「怎麼部署」的設定
    ├── Chart.yaml                chart 的名字和版本
    ├── values.yaml               ★ 唯一需要改的檔
    └── templates/                模板：讀 values.yaml，產生 Kubernetes 設定
        ├── namespace.yaml        建 namespace，把 observability 照抄成門牌
        ├── deployment.yaml       跑收值程式（固定 1 個）
        ├── service.yaml          給 :9464 一個固定名字
        └── vmservicescrape.yaml  登記給既有的 vmagent 來抓
```

測試機 AP1105 上（`/home/aiadm/eqp-collector-sim/`）：

```
eqp-collector-sim/
├── image/                        放 image tar（不進 git：檔案 100 多 MB）
│   └── equipment-collector-test.tar
└── k8s/                          從 repo 的 app/eqp-collector/k8s 整包複製過來
```

程式本體是同一個資料夾裡的 `main.py`。

### 程式會吐出什麼

**metrics**（`:9464/metrics`，給 vmagent 抓）

| 名稱 | 意思 |
|---|---|
| `eqp_value{machine, param}` | 收到的值，例如 EQP001 的 temperature |
| `eqp_collect_total{machine, result}` | 收值成功（ok）/ 失敗（fail）累計次數 |
| `eqp_up{machine}` | 最近一次收值成功是 1，失敗是 0 |
| `eqp_last_success_timestamp_seconds{machine}` | 最後一次成功的時間。告警用：`time() - 它 > 300` 代表 5 分鐘沒收到 |
| `eqp_collect_duration_seconds` | 收一次花多久 |

**log**（stdout，一行一筆 JSON）

```json
{"level": "INFO", "message": "收值成功", "machine": "EQP001", "readings": {"temperature": 59.49, "pressure": 51.43, "flow": 52.98}, "trace_id": "da9edd1b92d6426d"}
{"level": "WARN", "message": "收值失敗", "machine": "EQP002", "error": "EQP002 在 3 秒內沒有回應", "trace_id": "34eba20ff7ce41ef"}
```

失敗是故意模擬的（預設 3% 機率），用來確認失敗也看得到。

### image 是怎麼做出來的

在開發機（Windows）的 repo 根目錄：

```bash
docker build --build-arg BASE_IMAGE=python:3.12-slim -t equipment-collector:test app/eqp-collector
docker save -o equipment-collector-test.tar equipment-collector:test
```

再把 `equipment-collector-test.tar` 複製到 AP1105 的 `/home/aiadm/eqp-collector-sim/image/`。

---

## 5. values.yaml 逐項說明

| 欄位 | 現在的值 | 意思 | 可以改嗎 |
|---|---|---|---|
| `namespace` | `eqp-collector-dev` | 部署到哪個 namespace，chart 會自己建 | 可以。但改了再部署，**舊的 namespace 會被整個刪掉** |
| `observability.dept` | `D1110` | 資料屬於哪個部門，貼成門牌 | 可以。**不影響** metrics 分流 |
| `observability.tenant` | `imc` | 資料寫進哪個租戶。**metrics 分流只看這個** | **測其他情境就是改這個** |
| `observability.system` | `eqp` | 業務系統，分析用 | 可以。刪掉這行就不會有 system 門牌（不會變成空的） |
| `app.name` | `eqp-collector` | 程式名稱，所有資源都用這個名字 | 不建議改 |
| `image.repository` / `tag` | `equipment-collector` / `test` | 要跑哪個 image | 要跟 tar 裡的名字一模一樣 |
| `image.pullPolicy` | `Never` | 只用 node 上已經有的 image，不上網拉 | **不要改**：這台沒有可以拉的地方 |
| `replicas` | `1` | 跑幾個 | **不能改**，改了部署直接失敗（原因見 [9](#9-為什麼這樣設計)） |
| `nodeSelector` | `{}` | 指定跑在哪台 node | 多台 node、只有一台有 image 時要設（見 [Step 3](#step-3把-image-匯入-node)） |
| `ports.http` | `8080` | 健康檢查用（`/healthz`、`/readyz`） | 要跟 `env.HTTP_PORT` 一樣 |
| `ports.metrics` | `9464` | metrics 用 | 要跟 `env.METRICS_PORT` 一樣 |
| `resources` | CPU 20m、記憶體 48Mi～128Mi | 資源上下限 | 可以 |
| `env.EQUIPMENT` | `EQP001,EQP002,EQP003` | 模擬哪幾台機台 | 可以 |
| `env.PARAMS` | `temperature,pressure,flow` | 每台收哪些參數 | 可以 |
| `env.INTERVAL_SECONDS` | `10` | 幾秒收一次 | 可以 |
| `env.SIMULATED_FAIL_RATE` | `0.03` | 模擬失敗的機率（3%） | 可以。`0` 就不會失敗 |
| `env.METRICS_PORT` / `HTTP_PORT` | `9464` / `8080` | 程式實際開的 port | 跟 `ports` 一起改 |

chart 會自動擋下這些錯誤，部署前就失敗，不會帶著錯誤上線：

- `replicas` 不是 1
- `env.METRICS_PORT`、`env.HTTP_PORT` 跟 `ports` 不一樣
- `observability` 底下出現 `dept`、`tenant`、`system` 以外的欄位（例如打錯成 `tenent`，或還在寫舊的 `group`）

---

## 6. 操作步驟

以下都在 **AP1105** 上執行（SSH / MobaXterm），除非特別註明。**前一步沒過，不要往下做。**

### Step 0：前提

- [ ] AP1105 上 `kubectl get nodes` 跑得動，`helm version` 有東西
- [ ] cluster 已經有 VictoriaMetrics operator 和 vmagent（Step 4、Step 6-8 會確認）
- [ ] 那台 vmagent 用的是 repo 的 [`platform/victoriametrics/vmstack-values.yaml`](../../platform/victoriametrics/vmstack-values.yaml)（Step 6-8 會確認）
- [ ] vmagent 所在 namespace 有 `vmauth-token` Secret，裡面有 `ctsp`、`erlin` 兩個 key（兩座各發給這個 cluster 的那一把）

### Step 1：把檔案放上 AP1105

目標長這樣：

```
/home/aiadm/eqp-collector-sim/
├── image/equipment-collector-test.tar
└── k8s/         ← 這個資料夾的 k8s/ 整包複製過去
```

三種方法擇一：

- MobaXterm 左側 SFTP 面板，把 `k8s/` 資料夾拖進 `/home/aiadm/eqp-collector-sim/`
- 在開發機的 repo 根目錄（Git Bash）：
  ```bash
  scp -r app/eqp-collector/k8s aiadm@AP1105:/home/aiadm/eqp-collector-sim/
  ```
- 在 AP1105 `git clone` 這個 repo，直接用 repo 裡的 `app/eqp-collector/k8s`（下面指令的路徑要跟著換）

### Step 2：確認 cluster 長什麼樣、image 要放哪台

```bash
kubectl config current-context
kubectl get nodes -o wide
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.nodeInfo.containerRuntimeVersion}{"\n"}{end}'
hostname; hostname -I
```

怎麼看：

| 問題 | 看哪裡 |
|---|---|
| 有幾台 node？ | `get nodes` 有幾行 |
| **AP1105 本身是不是 node？** | `hostname` 或 `hostname -I` 的 IP 有沒有出現在 `get nodes -o wide` 的 NAME / INTERNAL-IP。**沒有的話，AP1105 只是管理機，image 放在 AP1105 沒有用**，要複製到 node 上 |
| node 用什麼跑 image？ | 第三個指令每行後面那段：`containerd://`、`docker://`、`cri-o://` |
| 是不是 Rancher RKE2？ | `get nodes` 的 VERSION 結尾是 `+rke2r1` 就是 |

**image 必須在 Pod 實際跑的那台 node 上**，不是在 AP1105 上。

### Step 3：把 image 匯入 node

先把 tar 複製到要跑的那台 node（如果 AP1105 本身就是那台，跳過）：

```bash
scp /home/aiadm/eqp-collector-sim/image/equipment-collector-test.tar <帳號>@<node>:/tmp/
```

再**登入那台 node**，依 Step 2 看到的結果選一條：

| Step 2 看到 | 匯入指令（在 node 上） | 確認 Kubernetes 看得到 |
|---|---|---|
| `containerd://…-k3s1`，VERSION `+rke2r1`（RKE2） | `sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock -n k8s.io images import /tmp/equipment-collector-test.tar` | `sudo /var/lib/rancher/rke2/bin/crictl --runtime-endpoint unix:///run/k3s/containerd/containerd.sock images \| grep equipment` |
| `containerd://…-k3s1`，VERSION `+k3s1`（k3s） | `sudo k3s ctr -n k8s.io images import /tmp/equipment-collector-test.tar` | `sudo k3s crictl images \| grep equipment` |
| `containerd://…`（一般） | `sudo ctr -n k8s.io images import /tmp/equipment-collector-test.tar` | `sudo crictl --runtime-endpoint unix:///run/containerd/containerd.sock images \| grep equipment` |
| `docker://…` | `docker load -i /tmp/equipment-collector-test.tar` | `docker images \| grep equipment` |
| `cri-o://…` | `sudo skopeo copy docker-archive:/tmp/equipment-collector-test.tar containers-storage:docker.io/library/equipment-collector:test` | `sudo crictl images \| grep equipment` |

確認時要看到：

```
docker.io/library/equipment-collector   test   ...
```

> ⚠ **containerd 一定要加 `-n k8s.io`**。沒加的話 image 會進 ctr 自己的預設區，Kubernetes 看不到，Pod 會卡在 `ErrImageNeverPull`，而且匯入時不會報錯。
>
> 匯入時出現 `content digest ... not found`：指令加上 `--platform linux/amd64` 再試一次。

**多台 node 時**，二選一：

- **每台可以跑 Pod 的 node 都匯入**：不用改設定。
- **只匯入一台**：把 Pod 釘在那台。在 `k8s/values.yaml` 改成：
  ```yaml
  nodeSelector:
    kubernetes.io/hostname: <那台 node 的 NAME>
  ```
  不然 Pod 可能被排到沒有 image 的 node。不要選有 `NoSchedule` taint 的 node（通常是 control-plane），Pod 會一直 `Pending`：
  ```bash
  kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints[*].key
  ```

### Step 4：部署前檢查

```bash
cd /home/aiadm/eqp-collector-sim
helm lint ./k8s
helm template eqp-collector ./k8s
```

- `helm lint`：要看到 `0 chart(s) failed`（`icon is recommended` 可以不理）。
- `helm template`：把會部署的內容印出來，**還沒有真的部署**。要看到這兩段：

```yaml
kind: Namespace
metadata:
  name: eqp-collector-dev
  labels:
    dept: D1110
    tenant: imc
    system: eqp
```

```yaml
          image: equipment-collector:test
          imagePullPolicy: Never
```

再確認兩件事：

```bash
# 要有東西。沒有的話部署會失敗：no matches for kind "VMServiceScrape"
kubectl get crd vmservicescrapes.operator.victoriametrics.com

# 要是 NotFound。已經存在的話見第 8 節「invalid ownership metadata」
kubectl get ns eqp-collector-dev
```

### Step 5：部署

```bash
helm upgrade --install eqp-collector ./k8s -n default --reset-values --wait --timeout 3m
```

看到 `STATUS: deployed` 就是部署完成，Pod 也已經 Ready。

這個指令為什麼長這樣：

| 參數 | 為什麼 |
|---|---|
| `upgrade --install` | 第一次是安裝，之後改設定再跑同一行就是更新 |
| `-n default` | 這只決定 Helm 把「安裝紀錄」存在哪。程式本身一定在 `eqp-collector-dev`。寫成 `-n eqp-collector-dev` 會失敗，原因見 [9](#9-為什麼這樣設計) |
| `--reset-values` | 每次都只照 `values.yaml`。不加的話，Helm 會偷偷沿用上一次用 `--set` 改過的值 |
| `--wait --timeout 3m` | 等 Pod 真的 Ready 才算成功，3 分鐘還沒好就報失敗 |
| **不要加** `--create-namespace` | 它會先建一個沒有門牌的 namespace，跟 chart 自己要建的撞在一起 |

### Step 6：驗證

照順序做。每一步都有「預期」；不一樣就先停下來看第 8 節。

**6-1. 門牌有沒有貼上**

```bash
kubectl get ns eqp-collector-dev --show-labels
```

預期包含 `dept=D1110`、`tenant=imc`、`system=eqp`。另外會有 `app.kubernetes.io/managed-by=Helm`（Helm 自己加的，正常）。

**6-2. Pod 有沒有在跑**

```bash
kubectl get pods -n eqp-collector-dev -o wide
```

預期 `READY 1/1`、`STATUS Running`。`NODE` 那欄要是你匯入 image 的那台。

**6-3. 用的是不是手動匯入的 image**

```bash
kubectl get pods -n eqp-collector-dev -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"  "}{.spec.containers[0].imagePullPolicy}{"  ready="}{.status.containerStatuses[0].ready}{"\n"}{end}'
```

預期 `equipment-collector:test  Never  ready=true`。

node 上沒有 image 時，狀態會是 `ErrImageNeverPull`（不是 `ImagePullBackOff`，因為它根本不會去拉）。

**6-4. 程式有沒有在收值**

```bash
kubectl logs -n eqp-collector-dev deploy/eqp-collector -f
```

預期第一行是 `開始收值`，之後每 10 秒 EQP001、EQP002、EQP003 各一行 `收值成功`，偶爾一行 `收值失敗`（模擬的）。按 `Ctrl+C` 離開。

**6-5. Service**

```bash
kubectl get svc,endpoints -n eqp-collector-dev
```

預期 `service/eqp-collector` 的 PORT 是 `9464/TCP`，`endpoints` 後面有一個 `<Pod IP>:9464`。endpoints 是空的，代表 Service 找不到 Pod。

**6-6. VMServiceScrape 有沒有被 vmagent 收下**

```bash
kubectl get vmservicescrape -n eqp-collector-dev -o yaml
```

看最下面的 `status.conditions`，預期有一行像這樣：

```
type: <vmagent 名稱>.<它的 namespace>.vmagent.victoriametrics.com/Applied
status: "True"
reason: ConfigParsedAndApplied
```

這行直接告訴你**是哪一台 vmagent 收下了它**，下一步會用到。舊版 operator 可能沒有這段，就直接看 6-8。

**6-7. /metrics 有沒有數字**

開**第二個終端機分頁**：

```bash
kubectl port-forward -n eqp-collector-dev svc/eqp-collector 9464:9464
```

回到第一個分頁：

```bash
curl -s localhost:9464/metrics | grep -E '^eqp_(collect_total|value|last_success_timestamp_seconds|up|collect_duration_seconds)'
```

預期看到 `eqp_collect_total`、`eqp_value`、`eqp_last_success_timestamp_seconds`、`eqp_up`、`eqp_collect_duration_seconds_*` 開頭的行。看完回第二個分頁按 `Ctrl+C`。

**6-8. 既有 vmagent 有沒有抓到、有沒有帶門牌、分流規則對不對**

先查 vmagent 的實際名稱（不要用猜的）：

```bash
kubectl get vmagent -A
```

把輸出的 NAMESPACE 和 NAME 填進去（如果不只一台，選 6-6 裡寫的那台）：

```bash
VMA_NS=<NAMESPACE>; VMA=<NAME>
kubectl get svc -n $VMA_NS -l app.kubernetes.io/name=vmagent,app.kubernetes.io/instance=$VMA
```

第二個分頁（Service 名稱照上一行的結果，通常是 `vmagent-<NAME>`）：

```bash
kubectl port-forward -n $VMA_NS svc/vmagent-$VMA 8429:8429
```

回到第一個分頁，**(a) 有沒有抓到、有沒有帶門牌**：

```bash
curl -s localhost:8429/api/v1/targets | python3 -c 'import sys,json; [print(t["health"], {k: t["labels"].get(k) for k in ("namespace","pod","dept","tenant","system")}) for t in json.load(sys.stdin)["data"]["activeTargets"] if t["labels"].get("namespace") == "eqp-collector-dev"]'
```

預期：

```
up {'namespace': 'eqp-collector-dev', 'pod': 'eqp-collector-...', 'dept': 'D1110', 'tenant': 'imc', 'system': 'eqp'}
```

- 什麼都沒印：vmagent 還沒抓到，等 30 秒再試；一直沒有見第 8 節。
- `down`：vmagent 連不到 Pod 的 9464。
- 有 `up` 但 `tenant` 是 `platform`：門牌沒帶進資料（沒貼的會被補成 `platform`），全部會進兜底。見第 8 節。

**(b) 分流規則是不是 repo 那一份**：

```bash
curl -s localhost:8429/remotewrite-relabel-config
```

預期七條：第一條 `labeldrop` + `vm_account_id|vm_project_id`，接著四條把 `tenant` 換成 `vm_account_id`（`tsid` 1001、`bsid` 1002、`icsd` 1003、`imc` 1004），最後兩條是 `^$` 的兜底（`platform`、1000）。

**如果只有一段、或沒有 keep / drop**：這個 cluster 的 vmagent 不是用 repo 的設定，**這個測試就驗不到分流**，要先找平台確認。

**6-9. 資料進了哪個 tenant（最後結論）**

寫入用的 token 只能寫、不能查。查詢要擇一：

**(a) 有可以查全部 tenant 的讀取 token**：

```bash
read -s -p 'read token: ' READ_TOKEN; echo
curl -sk -H "Authorization: Bearer $READ_TOKEN" --data-urlencode 'query=count by (vm_account_id) (last_over_time(eqp_up{namespace="eqp-collector-dev"}[2m]))' https://vmauth.aillm.spil.com.tw/api/v1/query
```

`read -s` 是為了讓 token 不留在指令歷史裡。實際路徑以中央 vmauth 的設定為準。

**(b) 可以用 kubectl 連中央 cluster**：先 `kubectl get svc -A | grep vmselect` 找到 vmselect，port-forward 到本機 8481，再：

```bash
for id in 1000 1001 1002 1003 1004; do echo "accountID=$id $(curl -s --data-urlencode 'query=count(last_over_time(eqp_up{namespace="eqp-collector-dev"}[2m]))' localhost:8481/select/$id/prometheus/api/v1/query)"; done
```

預期（第一輪 `tenant=imc`）：

| tenant | accountID | 預期 |
|---|---|---|
| IMC | 1004 | **3**（EQP001～003 各一條） |
| 其他四個 | 1000 ~ 1003 | 沒有結果（`"result":[]`） |

accountID 的對應寫在 [`platform/victoriametrics/vmstack-values.yaml`](../../platform/victoriametrics/vmstack-values.yaml) 的 `inlineRelabelConfig`，只有那一份。

進了 IMC 的數據每筆都會帶 `dept=D1110 tenant=imc system=eqp app=eqp-collector version=test namespace=eqp-collector-dev`。

### Step 7：測其他 tenant

第一輪 IMC 跑通之後才做。**同一個 chart，只改 `k8s/values.yaml` 的 `tenant`**：

1. 改 `observability.tenant`（`tsid`、`icsd`，或寫一個不存在的值）
2. 跑 Step 5 同一行指令
3. 6-1 確認門牌變了
4. **等 2 分鐘**再做 6-9（舊的資料要先離開 `[2m]` 查詢視窗，不然會看到上一輪的結果）
5. 結果填進第 7 節的表

### Step 8：清除

```bash
helm uninstall eqp-collector -n default
```

會刪掉 `eqp-collector-dev` 整個 namespace 和裡面所有東西。已經送進 tenant 的數據不會被刪。

node 上的 image 要一起清的話（containerd，在 node 上）：

```bash
sudo ctr -n k8s.io images rm docker.io/library/equipment-collector:test
```

RKE2 / k3s 的 ctr 路徑照 Step 3 的表換。

---

## 7. 測試紀錄

| 日期 | 環境 | tenant | 進了哪個 AccountID | 符合預期 |
|---|---|---|---|:---:|
| 2026-09-22 | 本機 k3d，**舊分流**（`group=imc`、三把 token） | — | IMC 3 筆、IT 0、platform 0 | ✓ |
| | AP1105 | `imc` | | |
| | AP1105 | `icsd` | | |
| | AP1105 | 不合法 / 沒寫 | | |

⚠ 2026-09-22 那一列測的是改版前的分流（`group`、每個租戶一把 token）。改成 `tenant` 之後，這份文件的步驟還沒有在 AP1105 重跑過；本機的實測在 repo 的 [`tests/tenant-routing`](../../tests/tenant-routing)。

本機參考環境：k3s v1.30.14（containerd 1.7.27-k3s1）、VictoriaMetrics operator v0.74.1、vmagent v1.148.0、Helm v4.3.0。image 用 `docker save` + `ctr -n k8s.io images import` 匯入，不是 `k3d image import`。vmagent 的 scrapeClasses 和 remoteWrite 取自 `platform/victoriametrics/vmstack-values.yaml`，只把送出去的位址換成本機的 vmauth，tenant 是本機的 VMSingle，不是中央。

---

## 8. 出問題時

| 看到什麼 | 原因 | 怎麼辦 |
|---|---|---|
| Pod `ErrImageNeverPull` | 那台 node 沒有 image；或 containerd 匯入時沒加 `-n k8s.io` | 在 `NODE` 欄那台重做 Step 3；多台 node 設 `nodeSelector` |
| Pod 一直 `Pending` | `nodeSelector` 指到不存在或有 taint 的 node；或資源不夠 | `kubectl describe pod -n eqp-collector-dev` 看最下面的 Events |
| Pod 一直重啟（`CrashLoopBackOff`） | 程式啟動失敗 | `kubectl logs -n eqp-collector-dev deploy/eqp-collector --previous` |
| helm：`namespaces "eqp-collector-dev" not found` | 用了 `-n eqp-collector-dev` | 改成 `-n default`（Step 5） |
| helm：`invalid ownership metadata` | `eqp-collector-dev` 已經存在，不是這個 chart 建的 | 如果是空的測試 namespace：`kubectl delete ns eqp-collector-dev` 再部署。要保留的話交給 Helm 管：<br>`kubectl label ns eqp-collector-dev app.kubernetes.io/managed-by=Helm`<br>`kubectl annotate ns eqp-collector-dev meta.helm.sh/release-name=eqp-collector meta.helm.sh/release-namespace=default` |
| helm：`no matches for kind "VMServiceScrape"` | cluster 沒有 VictoriaMetrics operator | 找平台，不是這個 chart 的問題 |
| helm：`replicas 只能是 1`、`env.METRICS_PORT ... 跟 ports 不一樣`、`observability.xxx 不認得` | `values.yaml` 寫錯，chart 主動擋下 | 照錯誤訊息改 |
| 改了 `tenant`，門牌沒變 | upgrade 沒加 `--reset-values`，Helm 沿用了上一次 `--set` 的值 | 用 Step 5 那一行（有 `--reset-values`） |
| 6-6 沒有任何 `Applied` | vmagent 沒選到它：vmagent 被設成只看特定 label 或 namespace | 看 `kubectl get vmagent -n $VMA_NS $VMA -o yaml` 的 `selectAllByDefault`、`serviceScrapeSelector`、`serviceScrapeNamespaceSelector` |
| 6-8 target 是 `down` | vmagent 連不到 Pod 的 9464，常見是 NetworkPolicy 擋住 | `kubectl get networkpolicy -n eqp-collector-dev` |
| 6-8 target `up`，但 `tenant` 是 `platform` 而不是你寫的值 | operator 或 vmagent 版本太舊，不認得 scrapeClasses。**CRD 沒有欄位檢查，不認得的設定會被默默丟掉，不會報錯** | 查版本，跟本機參考環境比（第 7 節） |
| 全部只進 platform | namespace 沒有 `tenant` 門牌、或值不在對照裡（6-1），或上一條 | 先看 6-1，再看 6-8 (a) |
| 每個 tenant 都沒有 | vmagent 送不出去：token 錯（常見是 token 結尾多一個換行 → 一直 401）或 Secret 少 key | `kubectl logs -n $VMA_NS svc/vmagent-$VMA \| grep -iE '401\|error'`；`kubectl describe secret vmauth-token -n $VMA_NS` 看每個 key 的 byte 數（只顯示長度，不會印出 token） |
| 6-8 (b) 是空的或條數不對 | 這個 cluster 的 vmagent 不是用 repo 的 `vmstack-values.yaml` | 這個測試驗不到分流。先找平台確認要不要套 |
| 換 tenant 後，舊 tenant 還查得到 | 舊的資料還在查詢視窗裡 | 等 2 分鐘，用 6-9 有 `[2m]` 的那條查詢 |

---

## 9. 為什麼這樣設計

**為什麼 namespace 由 Helm 建，不是先 `kubectl create ns`？**
正式環境是 ArgoCD 把 `values.yaml` 的 `observability` 貼到 namespace 上。這台沒有 ArgoCD，所以改由 chart 的 `namespace.yaml` 做同一件事。規則一樣：照抄，不查表、不從 dept 推導 group。先手動建 namespace 的話門牌是空的，而且 chart 會因為 namespace 已經存在而失敗。

**為什麼 Helm 的紀錄放在 `default`？**
Helm 在建任何東西之前，要先把「安裝紀錄」寫進 `-n` 指定的 namespace。`eqp-collector-dev` 這時還不存在，所以會失敗（本機實測：`namespaces "eqp-collector-dev" not found`）。`--create-namespace` 能解決這個，但它會先建一個沒門牌的 namespace，跟 chart 要建的撞在一起。所以紀錄放 `default`，chart 裡每個資源都明確寫 `namespace: eqp-collector-dev`。
代價：`helm list` 要加 `-n default` 才看得到；`helm uninstall` 會刪掉整個 `eqp-collector-dev`。

**為什麼固定 1 個、而且用 Recreate？**
每個 Pod 都會去收**全部**機台。2 個就是每台收兩次，數據重複。更新時預設是「先起新的、再停舊的」，中間會有幾秒兩個一起收；改成 Recreate 就是先停舊的再起新的。代價是更新時會有幾秒沒有收值。

**為什麼 `pullPolicy: Never`？**
image 是手動匯入 node 的，外面沒有地方可以拉。設 `Never` 的話，node 上沒有 image 會直接顯示 `ErrImageNeverPull`，一看就知道是 Step 3 沒做好。不會卡在「一直嘗試下載」。
代價：node 磁碟快滿時，Kubernetes 可能把沒在用的 image 清掉，之後要重新匯入。

**為什麼 VMServiceScrape 不寫 tenant、token、group？**
送去哪是平台的事。寫在 app 裡，部門改組就要改每一個 app。它只說「這個 Service 的 `/metrics` 要抓」。

既有 vmagent 會自動選到它，不需要額外設定。根據 `platform/victoriametrics/vmstack-values.yaml`：

- vmagent 沒有設 `serviceScrapeSelector`、`serviceScrapeNamespaceSelector`，chart 預設 `selectAllByDefault: true` → **所有 namespace 的 VMServiceScrape 都會被選到**，不需要特定 label。
- `scrapeClasses` 的 `default` 設了 `default: true` → **自動套用**，把 namespace 門牌上的 dept / tenant / system 貼到每筆數據上。VMServiceScrape 裡**不能**寫 `scrapeClass`，寫了別的名字會換掉它，門牌就帶不進去。
- 依 `tenant` 填租戶編號（`vm_account_id`），是 vmagent 的 `inlineRelabelConfig` 做的；送出去是兩條 `remoteWrite`，中科站、二林站各一條。

**為什麼有 securityContext？**
有些 cluster（例如開了 CIS 設定的 RKE2）會強制 Pod 用非 root 身分跑。先設好，這種 cluster 也放得進去。程式本來就不需要 root。

---

## 10. 跟其他 app 的差別

`app/` 底下其他 app（`mes-dispatch`、`metrics-sim`、`yms-sim`）都是給 ArgoCD 部署的。這個 app 是**手動 Helm 部署**，刻意不一樣：

| | 其他 app | eqp-collector |
|---|---|---|
| 誰部署 | ArgoCD（`platform/argocd/applicationset.yaml`） | 人手動 `helm` |
| namespace 門牌誰貼 | ArgoCD | chart 的 `namespace.yaml` |
| image 從哪來 | Nexus，自動拉 | tar 手動匯入 node，`Never` |
| `values.yaml` | 有 `argocd:` 區塊，`env` 是清單 | **沒有 `argocd:`**，`env` 是 key: value |
| 模板 | `_*.tpl` 共用標準模板 | 精簡版，只有 4 個檔 |
| vmagent / vmauth / tenant | 平台那一套 | **同一套**，不另外建 |

沒有 `argocd:` 區塊的影響：

- **`tests/validate.sh` 認得這種 app**。沒有 `argocd:`、有 `templates/namespace.yaml` 就當成手動 Helm，第 1 段標示「手動 Helm」：不查 `argocd` 欄位，`observability` 照查。第 3 段渲染這個 chart，確認它建的 Namespace 的 label 跟 `values.yaml` 一樣。
- ⚠ **有 ArgoCD 的環境會把它當成錯誤**（已知、刻意保留）：ApplicationSet 掃 `app/*/k8s/values.yaml` 會掃到這個 app，因為設了 `missingkey=error`，產生 Application 時會報錯。**不要把這個 repo 直接接到有 ArgoCD 的環境**，除非先補上 `argocd:` 區塊，或讓 ApplicationSet 排除這個路徑。

其他要注意的：

- 模板是從原本的標準 chart 精簡來的：label 名稱、probe、VMServiceScrape 的 relabel 都一樣。平台以後改標準模板，這個 app **不會**跟著變。
- 沒有 ArgoCD 幫忙加 `cluster` 標籤（repo 的 `externalLabels` 是空的，平常由 ArgoCD 補上）。**分流不受影響**，但 IMC tenant 裡分不出這些數據是哪個 cluster 來的。

### 這個測試證明什麼、不證明什麼

**證明**

- `values.yaml` → namespace 門牌 → 既有 vmagent 依 `tenant` 分流 → 中央 tenant，這條路在這個 cluster 是通的。
- 程式本身不需要知道 tenant、vmauth、token。

**不證明**

- **ArgoCD 那一段**：在 repo 的 [`tests/routing`](../../tests/routing) 驗。
- **log 的分流**：Fluentd 看的是 `dept` 不是 `tenant`，是另一條路，見 repo 根目錄 README 的 Known limitations。
- **真的機台**：現在是模擬的。接真機台只要換掉 `app/eqp-collector/main.py` 的 `read_equipment()`。
