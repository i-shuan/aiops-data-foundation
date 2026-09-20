# log 分流（Fluentd → Elasticsearch）

一句話：**Fluentd 依 namespace 的 `dept` label 改 tag，每個 group 一個 output、各帶只能寫自己 index 的 API key。**

設定檔在 [../platform/fluentd/fluent.conf](../platform/fluentd/fluent.conf)，已經在本機 k3d 實測通過。

## 資料怎麼走

```
容器 stdout
   → tail（CRI 格式）
   → 解析應用送出的 JSON
   → 補上 K8s metadata（含 namespace label）
   → 攤平成跟 metrics 同名的欄位
   → 依 dept 改 tag
   → it.**  → k8s-it-*          （IT 的 API key）
     imc.** → k8s-imc-*         （IMC 的 API key）
     其他   → k8s-unassigned-*  （兜底的 API key）
```

## 本機實測結果

**環境**：Fluentd v1.17（`fluentd-kubernetes-daemonset:v1.17-debian-elasticsearch8-1`）、Elasticsearch 8.15.3 Basic、k3d 單節點。

**結果**

| index | 收到的 namespace | 筆數 |
|---|---|---|
| `k8s-it-*` | `zk-mes-prod`（dept D3120） | 55 |
| `k8s-imc-*` | `zk-wip-prod`（dept D1110） | 55 |
| `k8s-unassigned-*` | `kube-system`（沒有 dept label） | 4 |

資料持續累積，分流一直正確（後續再查已經是 200 多筆）。

**一筆文件實際長這樣**

```json
{
  "cluster": "zk",
  "group": "it",
  "dept": "D3120",
  "namespace": "zk-mes-prod",
  "app": "mes-logger",
  "version": "2.3.1",
  "pod": "mes-logger-594899898c-r2sq6",
  "container": "logger",
  "level": "INFO",
  "message": "report ok",
  "trace_id": "abc123"
}
```

## 兩邊的欄位比對（AIOps 關聯用）

實際查過兩邊的資料，八個關聯欄位完全一致：

| 欄位 | metrics | log | 來源 |
|---|---|---|---|
| `cluster` | ✓ | ✓ | metrics 是 `externalLabels`；log 由 Fluentd 補上 |
| `group` | ✓ | ✓ | metrics 在 remoteWrite 分流時蓋上；log 取 tag 前綴 |
| `dept` | ✓ | ✓ | 兩邊都從 namespace 的 label 來 |
| `namespace` | ✓ | ✓ | 自動 |
| `pod` | ✓ | ✓ | 自動 |
| `container` | ✓ | ✓ | 自動 |
| `app` | ✓ | ✓ | 兩邊都從 pod label `app.kubernetes.io/name` 來 |
| `version` | ✓ | ✓ | 兩邊都從 pod label `app.kubernetes.io/version` 來 |

只有一邊有的欄位：

| 欄位 | 只在哪 | 說明 |
|---|---|---|
| `job`、`instance`、`service`、`endpoint` | metrics | Prometheus 體系的標準欄位 |
| `level`、`message`、`trace_id` | log | 應用自己寫出來的 |

**`namespace` + `pod` 是主要的關聯鍵**，要按服務分析就用 `app`，按組織分析就用 `dept` 或 `group`。

### group 的取法：從 tag 前綴，不要另外維護對照表

log 這邊的 `group` 是取分流後 tag 的第一段（`it.kube.xxx` → `it`）：

```
<filter it.** imc.** unassigned.**>
  @type record_transformer
  enable_ruby true
  <record>
    group ${tag.split(".").first}
  </record>
</filter>
```

這樣「部門 → group」的對照只存在 `rewrite_tag_filter` 的規則裡一份。如果改成在 `record_transformer` 裡自己查表填 group，就會變成同一份對照在設定裡出現兩次，改組時容易漏改一邊。

### metrics 的 app 和 version 要靠 VMServiceScrape 的 relabel

不是自動就有的。`app-common` 模板已經包含這段，照模板走的服務不用管；**手寫 VMServiceScrape 的服務會少掉這兩個欄位，而且不會有任何錯誤訊息**：

```yaml
relabelConfigs:
  - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
    target_label: app
  - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_version]
    target_label: version
```

**隔離是真的擋得住**：用 IT 的 API key 去寫 `k8s-imc-*`，Elasticsearch 回 403 `security_exception`。Basic 免費版就有這個能力。

## 三個坑，每一個都是靜默失敗

### 1. API key 只有 index 權限時，plugin 啟動就 403

fluent-plugin-elasticsearch 啟動時會先打 `GET /` 問版本，那需要 cluster 層級的 `monitor` 權限。只給 index 權限的 key 會被擋，整個 output 卡在重試，一筆都送不出去。

**解法**：關掉版本檢查，key 維持最小權限。

```
verify_es_version_at_startup false
default_elasticsearch_version 8
```

另一條路是給 key 加 cluster `monitor` 權限，但那會削弱隔離，不建議。

### 2. API key 要給 `index` 權限，不是 `create_doc`

這個坑有兩層，兩層都不會報錯：

- plugin 預設的寫入動作是 `index`，但 `create_doc` 權限只允許 `create` 動作 → **每筆都被 403 拒絕，而且 Fluentd 的 log 完全沒有訊息**，index 會被建出來但永遠 0 筆。
- 改成 `write_operation create` 之後，plugin 變成要求每筆自帶 `_id`，沒有就丟掉，而且**只有 debug 等級看得到**：`Dropping record because its missing an '_id' field`。

**解法**：API key 的權限用 `index`、`create_index`、`auto_configure`，plugin 維持預設的寫入動作。

### 3. `rewrite_tag_filter` 沒對到規則的記錄會直接消失

Fluentd 跟 Fluent Bit 行為不同。Fluent Bit 沒對到的記錄會維持原 tag 繼續往下走，**Fluentd 的 `rewrite_tag_filter` 是 output plugin，它會吃掉所有進來的記錄，只把對到規則的重新送出**。

所以「在後面放一個兜底的 `<match>`」是無效的，那個 match 永遠收不到東西。

**解法**：兜底要寫成 `rewrite_tag_filter` 的最後一條規則。

```
<rule>
  key     dept
  pattern /^.*$/
  tag     unassigned.${tag}
</rule>
```

搭配在 `record_transformer` 裡給 `dept` 預設值，沒有 label 的就填 `unassigned`，行為才會確定。

## 另外兩個發現

**namespace 的 label 預設就會被帶出來。** `kubernetes_metadata_filter` 不需要任何額外設定就會產生 `kubernetes.namespace_labels`，這正是歸屬的來源。但它需要 namespaces 的 `get`、`list`、`watch` 權限，RBAC 要一起給。

**label 的 key 不會被改寫，點還是點。** 實際看到的是 `app.kubernetes.io/name`，不是 `app_kubernetes_io/name`。取值時要用帶點的原始 key，寫錯的話欄位會是 `null`，一樣沒有任何錯誤訊息。

## 待確認

1. **API key 用環境變數帶入的寫法還沒驗證。** `custom_headers` 的值是以 JSON 解析，不是雙引號字串，`#{ENV[...]}` 可能不會被展開。建議整份 `fluent.conf` 放 Secret 由 CD 渲染好真值，而不是 ConfigMap 加環境變數。
2. **正式環境的 Fluentd 版本與 plugin 版本**，尤其 `fluent-plugin-elasticsearch` 的版本會影響 `custom_headers` 與 `write_operation` 的行為。
3. **正式環境現有的 Fluentd 設定要先 diff。** 這份是整份取代，現有的 parser、buffer 設定會不見。
4. **buffer 要設檔案模式。** 本機測試用的是記憶體 buffer，Elasticsearch 掛掉時資料會隨 pod 重啟消失。正式環境要用 file buffer 並設容量上限。
5. **`dept` 是寫入當下焊進資料的**，組織改組後舊資料保留的是當時的歸屬。metrics 那邊也是同樣的取捨。
