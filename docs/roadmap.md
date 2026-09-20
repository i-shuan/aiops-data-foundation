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

**這一階段已在本機 k3d 完整實測通過**，可用的設定與三個踩到的坑見 [efk.md](efk.md)。

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
    app       ${(record.dig("kubernetes","labels") || {})["app.kubernetes.io/name"]}
    version   ${(record.dig("kubernetes","labels") || {})["app.kubernetes.io/version"]}
    dept      ${((record.dig("kubernetes","namespace_labels") || {})["dept"]) || "unassigned"}
    cluster   zk
  </record>
  remove_keys kubernetes,docker,logtag,stream
</filter>
```

**欄位名稱不同名，後面的關聯分析全部失效**，所以這一步要先做對。

`kubernetes_metadata_filter` 預設會連 namespace 的 label 一起帶出來（`namespace_labels`），這正好是歸屬的來源。**實測確認 label 的 key 不會被改寫，點還是點**，要用 `app.kubernetes.io/name` 取值，寫成底線會得到 `null` 而且不會報錯。

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

**兜底必須寫成 `rewrite_tag_filter` 的最後一條規則**（`pattern /^.*$/` 配上 `dept` 的預設值），不能在後面放一個兜底的 `<match>`。Fluentd 的 `rewrite_tag_filter` 會吃掉所有進來的記錄，只把對到規則的重新送出，所以後面的 `<match>` 永遠收不到東西，那些 log 會直接消失。這點跟 Fluent Bit 的行為相反。

規則要從同一份「部門 → group」對照表渲染，不要手寫。

**3. 每個 group 一把寫入 API key**

用 index 權限限制只能寫自己的 index，寫錯會被 Elasticsearch 直接拒絕，不會寫進別人家。腳本見 [../platform/central/elasticsearch-setup.sh](../platform/central/elasticsearch-setup.sh)。

Fluentd 的 ES output 用 `custom_headers` 帶 `Authorization: ApiKey ...`（實測可行）。**權限要給 `index`，不能只給 `create_doc`**，否則 index 建得出來但永遠 0 筆，而且完全沒有錯誤訊息。另外要加 `verify_es_version_at_startup false`，不然 plugin 啟動時的版本檢查會因為缺少 cluster 權限被擋。

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

**範圍先對齊**：這一階段做的是「資料有沒有漏、系統哪裡出問題、為什麼」，**不是良率分析**。良率、製程參數、設備異常是 SPC 和 FDC 的範圍，這套系統不會去取代它們。詳見 [README 的「這套系統回答什麼、不回答什麼」](../README.md#這套系統回答什麼不回答什麼)。

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

1. **正式環境的 operator 版本**。本機在 v0.74.1 實測 `scrapeClasses` 加 `attachMetadata` 可用，你們那邊要確認版本不會比這舊太多。注意 chart 裝的 CRD 沒帶 schema，`kubectl explain` 查不到欄位也不會驗證，要看產生出來的設定 Secret 才準。
2. **歸屬放 namespace 的 label，還是一份「app → group」的中央清單**。前者要每個 namespace 都標，後者要 app 名稱全廠唯一。
3. **token 怎麼自動進新廠的 cluster**。現在靠 Rancher 手動建 Secret，擴廠後不可行。要確認有沒有 External Secrets、Vault 或 sealed-secrets。
4. **產線資料能不能出廠**。這決定階段三能不能考慮雲端的 SaaS 方案。
5. **MES 和中介程式是自己開發的嗎**。能改程式才吐得出業務指標；外購套裝軟體只能從資料庫或 log 側面撈。
