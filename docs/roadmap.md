# 三階段路線圖

目標是替 AIOps 打資料地基，最終要能分析產線與 MES 的異常，不是只看 K8s 有沒有活著。

## 前提

- **中央的 tenant 設計不碰。** 對方依 bearer token 分 tenant，要新增由他們自己加。我們只決定 K8s 這側「哪一批資料用哪一把 token」。
- **一個 cluster 一台 vmagent**（`victoria-metrics-k8s-stack` 產生的那台），不另外長新的收集器。
- **log 收集器是 Fluentd**，每個 node 一台。
- **Elasticsearch 是 Basic 免費版**，所以權限只到 index 層級，沒有 ML。
- **namespace 名稱不含組織資訊**，格式是 `{cluster}-{app}-{env}` 或 `{app}-{env}`。

---

## 階段一：metrics 分流

**目標**：IT 和 IMC 的 metrics 各自進自己的 tenant，不再混在一起。

**做什麼**

1. 跟中央要三把 token（IT、IMC、平台），放進 `vmauth-token` Secret 的三個 key。
2. `remoteWrite` 從一條拆成三條，各自過濾、各自帶 token。原本那條改成兜底。
3. kube-state-metrics 的 label allowlist 打開。

**完成標準**

- `count by (vm_account_id, group, namespace) (up)`：每個 namespace 只出現在一個 tenant。
- `count by (cluster, namespace, job, instance) (up) > 1`：查不到東西。

**坑**：舊那條一定要改掉不能留著，否則每筆資料送兩次；三段規則必須互斥；改完 Secret 要重啟 vmagent。

細節見 [vmagent.md](vmagent.md)。

---

## 階段二：log 接進 EFK

**目標**：log 的欄位跟 metrics 對得起來，而且各 group 只看得到自己的。

### 要做的事

**1. 統一欄位（這階段最重要的一步）**

Fluentd 的 `kubernetes_metadata_filter` 會把 metadata 放在 `kubernetes` 這個巢狀結構下，欄位名稱跟 metrics 對不起來。要攤平改名：

```
<filter kube.**>
  @type record_transformer
  enable_ruby true
  <record>
    namespace ${record.dig("kubernetes","namespace_name")}
    pod       ${record.dig("kubernetes","pod_name")}
    container ${record.dig("kubernetes","container_name")}
    app       ${record.dig("kubernetes","labels","app_kubernetes_io/name")}
    version   ${record.dig("kubernetes","labels","app_kubernetes_io/version")}
    dept      ${record.dig("kubernetes","namespace_labels","dept")}
    cluster   "#{ENV['CLUSTER_NAME']}"
  </record>
  remove_keys kubernetes,docker
</filter>
```

**欄位名稱不同名，後面的關聯分析全部失效**，所以這一步要先做對。

`kubernetes_metadata_filter` 預設會連 namespace 的 label 一起帶出來（`namespace_labels`），這正好是歸屬的來源。但 label 的 key 在 plugin 裡會被改寫（`.` 換成 `_`），實際長什麼樣子要先送一筆 log 出來看。

**2. 依歸屬分流**

用 `rewrite_tag_filter` 依 `dept` 改 tag，再由不同的 `<match>` 送到不同 index：

```
<match kube.**>
  @type rewrite_tag_filter
  <rule>
    key     dept
    pattern /^(D3120|D3130)$/
    tag     it.${tag}
  </rule>
  <rule>
    key     dept
    pattern /^(D1110|D1120)$/
    tag     imc.${tag}
  </rule>
</match>
```

沒被改到 tag 的（對不上、沒標 label 的）維持原 tag，由最後的兜底 `<match>` 送進 `k8s-unassigned-*`。**兜底一定要有，否則那些 log 直接消失。**

規則要從同一份「部門 → group」對照表渲染，不要手寫。

**3. 每個 group 一把寫入 API key**

用 index 權限限制只能寫自己的 index，寫錯會被 Elasticsearch 直接拒絕，不會寫進別人家。腳本見 [../platform/central/elasticsearch-setup.sh](../platform/central/elasticsearch-setup.sh)。

Fluentd 的 ES output 帶 API key，依 plugin 版本用 `api_key` 或 `custom_headers` 帶 `Authorization`，實際用哪個要看你們裝的版本。

**4. ILM 設定保存期限**

Basic 版就有。至少要涵蓋一個完整業務週期，否則階段三的 AI 沒有基準線可學。

**5. 讀取權限**

每個 group 一個唯讀角色，限制在自己的 index。

**6. 看板**

Kibana 或 Grafana 都免費。Grafana 的好處是 metrics 和 log 在同一個介面，關聯查詢時不用切來切去。

### 完成標準

- `k8s-it-*`、`k8s-imc-*` 都有資料。
- `k8s-unassigned-*` 裡只剩平台的 namespace。
- 用 IT 的帳號查不到 IMC 的 index。
- 同一支服務的 log 和 metrics，用 `namespace` + `pod` 對得起來。

### Basic 版做不到的兩件事

| 做不到 | 影響 | 怎麼繞 |
|---|---|---|
| 同一個 index 內的資料級權限 | 權限只能切到 group | 要再細只能每個部門一個 index，shard 會爆，不建議 |
| Elastic ML 異常偵測 | log 的異常偵測沒有現成的 | 階段三用規則和自建的方式處理 |

### 坑

- **Fluentd 設定是整份取代**，現有的 parser、filter 會不見，切換前一定要 diff。
- **Fluentd 需要 namespaces 的 `get`、`list`、`watch` 權限**才拿得到 namespace label，RBAC 要一起改。
- **每個 output 有自己的 buffer**，Elasticsearch 掛掉時 log 會堆在本地，要設 file buffer 和容量上限，不然塞爆節點磁碟。
- **沒標 label 的 namespace 會靜默進 unassigned**，要有人定期看。

---

## 階段三：產線與 MES 異常分析

**前半段要最早開始，不要等前兩階段做完。** 業務指標沒吐出來的期間，資料永遠補不回來。

### 為什麼現在的資料不夠

現在收的是「系統活著沒」：CPU、記憶體、HTTP 延遲。產線卡住通常長這樣：**所有技術指標都正常，但工單沒有往下走**。

實際例子：MES 的 API 回應 50ms、CPU 30%、沒有錯誤 log，一切正常。但某站點的機台報工資料 20 分鐘沒進來，因為中介程式的 queue 卡住了。這件事在現有資料裡完全看不見。

### 3-1　讓系統吐出業務指標

| 指標 | 為什麼重要 |
|---|---|
| **每台機台最後一筆資料距今幾秒** | 卡住時直線上升，最靈敏的訊號 |
| 每分鐘報工、派工筆數 | 吞吐掉下來代表有東西塞住 |
| 待處理工單數、queue 長度 | 積壓是卡住的前兆 |
| 設備連線中斷次數、交易失敗率 | 直接的故障訊號 |
| 派工到機台確認的耗時 | 端到端變慢，中間某段有問題 |

只有 MES 和中介程式吐得出來，工具買再貴也變不出來。`app-common` 已經把 OpenTelemetry 接好，多吐幾個指標只是多幾行程式。

**注意基數**：一台機台一條 series 沒問題，但不要把批號、工單號放進 label，那會把儲存撐爆。

### 3-2　先用規則做第一版

不需要 AI，Grafana 的告警免費：

- 某台機台超過 5 分鐘沒有資料
- queue 長度連續 10 分鐘上升
- 報工吞吐掉到過去同時段的一半

**產線八成的異常這樣就抓到了，而且現在就能做。**

### 3-3　累積資料之後再談 AI

跑三到六個月，有了歷史基準線，再決定要不要上異常偵測。那時你會知道哪些指標真的需要 AI 判斷，談授權也有底氣。

免費的路是自己寫程式讀跨租戶唯讀帳號做統計；要買就是 Elastic ML（需升級授權）或 vmanomaly（企業版）。

**先買工具再想要收什麼，結果通常是買了一套很貴的系統，看著一堆綠燈，產線還是停。**

### 完成標準

產線出事時，是系統先叫，而不是人打電話來。

---

## 花費

| 階段 | 費用 |
|---|---|
| 一、二 | 0，全部用免費版做得到 |
| 三的 3-1、3-2 | 0，只有開發工時 |
| 三的 3-3 | 自建是 0；要買 Elastic ML 或 vmanomaly 才需要授權 |

---

## 待確認

1. **`scrapeClasses` 你們的 operator 版本支不支援**（決定 metrics 能不能在抓取時讀 namespace label）：
   ```bash
   kubectl explain vmagent.spec.scrapeClasses.attachMetadata
   ```
2. **Fluentd 的 `kubernetes_metadata_filter` 帶出來的 namespace label 實際 key 長什麼樣**，要送一筆 log 出來看。
3. **歸屬放 namespace 的 label，還是一份「app → group」的中央清單**。前者要每個 namespace 都標，後者要 app 名稱全廠唯一。
4. **token 怎麼自動進新廠的 cluster**。現在靠 Rancher 手動建 Secret，擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
5. **產線資料能不能出廠**。這決定階段三能不能考慮雲端的 SaaS 方案。
6. **MES 和中介程式是自己開發的嗎**。能改程式才吐得出業務指標；外購套裝軟體只能從資料庫或 log 側面撈。
