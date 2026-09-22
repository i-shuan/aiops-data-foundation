# vmagent 分流：上線步驟

分流怎麼運作、regex 為什麼這樣寫，見 [architecture.md](architecture.md)。這份只講**怎麼把它裝上去**。

一句話：**拆的是 `remoteWrite`，不是 vmagent。** vmagent 維持一個 cluster 一台，出口從一條變成三條。

![tenant 從依廠分改成依組織分](architecture/before-after-tenant-split.svg)

## 這份設定在哪一層：cluster，不是 app

| | vmstack values（這份） | app 的 values |
|---|---|---|
| 幾份 | 一份共用，由 cluster-addons 的 ApplicationSet 部到每個場區叢集 | 一個 app 一份 |
| 誰改 | 平台 | app 團隊 |
| 有沒有 token | 有三把 | **絕對不能有** |

app 那邊只負責讓 `app-common` 產生一個 VMServiceScrape，跟那台唯一的 vmagent 說「來抓我」。分流全部發生在 vmagent，app 不知情。

把 `vmagent.enabled` 改成 `false` **不是分流**，是把收集器整台刪掉，整廠 metrics 會直接停。

## 要改的檔案

[../platform/cluster-addons/vmstack-values.yaml](../platform/cluster-addons/vmstack-values.yaml) 是**照你們現有的 values 模板改出來的完整版本**，直接跟你們的 `cluster-addons/vmagent/values.yaml` diff。只動了三個地方：

| 位置 | 改什麼 |
|---|---|
| `vmagent.spec.scrapeClasses` | **新增**：抓取時把 namespace 的 label 貼到資料上 |
| `vmagent.spec.remoteWrite` | 一條拆三條，依 `group` 過濾、各帶自己的 token |
| `kube-state-metrics.metricLabelsAllowlist` | **新增**：讓 `kube_namespace_labels` 帶出 namespace 上的歸屬 |

另外順手修了原始模板一個縮排錯誤：`victoria-metrics-operator` 底下的 `requests` / `limits` 跟 `resources` 同層，等於 operator 沒吃到任何資源設定。

`externalLabels` 維持空的，cluster 名字由 ApplicationSet 的 `helm.parameters` 注入。這份是全廠共用的，寫死會讓其他廠掛上錯的 cluster 名。

## 上線順序：Secret 一定要先

1. **請中央發三把 token，`vmauth-token` Secret 從一個 key 變成三個**：`it`、`imc`、`platform`
2. **app 的 namespace 已經有 label**（app.yaml 的 observability 已經上線）
3. **最後才套 values**

順序顛倒的後果不一樣：

| 漏掉哪一步 | 後果 |
|---|---|
| 第 1 步 | vmagent 找不到 `it` / `imc` 這些 key，remoteWrite 起不來，**整個 cluster 的 metrics 會停** |
| 第 2 步 | 三條都過濾不到東西，**全部落到 platform**。資料不會掉，只是沒分流 |

所以真正致命的是第 1 步。套之前先檢查三個 key 都在：

```bash
for k in it imc platform; do
  kubectl -n <vmstack namespace> get secret vmauth-token -o jsonpath="{.data.$k}" >/dev/null 2>&1 \
    || { echo "vmauth-token 缺少 $k，先補齊再套 values，否則整廠 metrics 會停"; exit 1; }
done
```

## Secret 怎麼建

### Rancher

1. 切到目標 cluster，進 Storage 底下的 Secrets
2. 選 vmstack 部署的那個 namespace，**不是 app 的 namespace**
3. 編輯既有的 `vmauth-token`，型別維持 Opaque，用 key/value 模式逐一加 `it`、`imc`、`platform`
4. 存檔後重啟 vmagent

### kubectl

```bash
kubectl -n <vmstack namespace> create secret generic vmauth-token \
  --from-literal=it=<IT token> \
  --from-literal=imc=<IMC token> \
  --from-literal=platform=<平台 token> \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n <vmstack namespace> rollout restart deployment/vmagent-<名稱>
```

舊的 `token` key 可以留著，已經沒有 remoteWrite 引用它。

### 建完怎麼確認

```bash
kubectl -n <vmstack namespace> describe secret vmauth-token
```

會列出每個 key 的 byte 數。**比 token 實際長度多 1，通常就是結尾多了換行**——最常見的失敗，一直 401 而且錯誤訊息不會說原因。

### 坑

- **不要用 Rancher 的「Edit as YAML」貼部分內容**，那等於整份取代，沒貼到的 key 會消失
- **namespace 和 cluster 很容易選錯**
- **手動改沒有紀錄可追**，只能翻 audit log

### 手動 vs Vault

兩廠的時候手動撐得住，擴到五六廠就不行：每開一廠要貼三把 vmauth token、三把 ES API key，貼錯是靜默失敗。

| | 手動 | Vault + Vault Secrets Operator |
|---|---|---|
| token 經過哪 | **不經過 git、不經過 CI** | 多經過 Vault 和 VSO |
| 排查 | `kubectl get secret` 就看到 | 要查 VSO 的 CR、Vault 的 auth |
| 擴廠 | 每開一廠人工做一次 | list 加一行，自動出現 |
| 輪替 | 每個廠各改一次 | 改 Vault 一處 |
| cluster 重建 | **會漏掉**，vmagent 起不來 | 跟著 ApplicationSet 回來 |

`cluster-addons/vault-secrets-operator/` 已經在每個場區叢集部署，`cctv-inspection-app` 也已經在用 `k8sAuthMount`，路鋪了一半。Vault 上線後在 `cluster-addons/` 放一份 `VaultStaticSecret` 指向 token 的路徑，那份 CR 裡只有「去哪裡拿」，可以進 git。

## 驗收

```
count by (vm_account_id, group, namespace) (up)
```

一個 namespace 出現在幾個 `vm_account_id`，應該等於它 `group` label 裡有幾段。`group=it` 的出現在兩個 tenant，就是規則寫錯了。

再開 vmagent 的 `/metrics`，看 `vmagent_remotewrite_` 開頭的指標。三條出口分開統計，哪一條的 token 有問題一眼就看得出來。

**確認 vmagent 實際載入的分流規則**：

```bash
kubectl -n <vmstack namespace> port-forward svc/vmagent-<名稱> 8429
# 開 http://localhost:8429/remotewrite-url-relabel-config
```

**改完設定不會馬上生效。** operator 更新 Secret 後，vmagent 要等 kubelet 同步（最多一分鐘左右）才重新載入。這段期間跑的還是舊設定。E2E 測試就是因為這樣曾經假裝通過，後來加了「等實際載入的設定一致才開始檢查」這一步。

## 坑

- **原本那條 remoteWrite 一定要改掉，不能留著再加新的。** 它沒有任何過濾，留著等於每筆資料多送一份到舊 tenant。
- **platform 那條不能省。** 沒有它，node-exporter、kube-state-metrics 這些節點層級的資料會整批消失。它的 drop regex 必須列出所有 tenant 的名字。
- **`kubectl explain` 問不出東西。** chart 裝的 CRD 沒帶 schema，API server 不驗證，**欄位打錯不會報錯，只會靜默失效**。要看 operator 產生出來的設定或上面那個端點才準。
- **VMServiceScrape 是用 Service 的 label 去選的**，不是 Service 的 pod selector。Service 沒打 label，target 整個不存在，沒有任何錯誤訊息。`app-common` 有幫 Service 打 label，照模板走不會踩到。
- **每多一條出口，每筆資料就多比對一次、多一個磁碟佇列。** 3 條沒問題，之後要切到部門層級（幾十條）要重新評估。
- **好處是故障隔離。** 每條出口的佇列獨立，某個 tenant 的 token 過期，只有那條卡住重試，其他不受影響。

## 待確認

1. **正式環境的 operator 版本。** 本機 v0.74.1 實測 `scrapeClasses` + `attachMetadata` 可用。
2. **cluster 裡是不是只有這一台 vmagent。** 還有別的就會重複收，分流做得再對也沒用：`kubectl get vmagent -A`
3. **token 怎麼自動進新廠的 cluster**，見上面「手動 vs Vault」。
