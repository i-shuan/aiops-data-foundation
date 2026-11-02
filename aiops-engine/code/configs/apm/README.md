# 用 Elastic APM 的資料跑 aiops-engine

> **類型：操作 How-To**　同主題：[原理](../../../../docs/rca/README.md)・[baseline 怎麼跑](../baseline/README.md)・[規格](../baseline/reference.md)・[紀錄](../../../../docs/pdca/meta/2026-11-apm-source.md)

公司的 ES 已經有 Elastic APM（Fleet 的 `apm` integration）收的資料，**不改 Collector、不改 ES 現有索引**，engine 直接讀。
這個資料夾是 APM 版的設定檔；寫法跟 `configs/baseline/`、`configs/rca/` 一樣，只是索引、欄位換成 APM 的。

## 哪些功能能用

| 功能 | APM 資料 | 讀哪裡 |
|---|---|---|
| 每支 API 的請求數、錯誤率、p95／p99、慢請求比例 | ✅ | `metrics-apm.transaction.1m-*`（每分鐘的直方圖，微秒） |
| 往外呼叫（DB、下游）的次數、平均耗時、錯誤率 | ✅ 沒有 p95 | `metrics-apm.service_destination.1m-*`（只有次數與總和） |
| 靜態臨界值規則：錯誤率 5%、p95 2 秒 | ✅ | 同上 |
| Pod 同儕橫向比對：錯誤率、p95 | ✅ | 同上，依 `service.node.name` 分 Pod |
| trace 合成一筆、每一段的 baseline、根因分析 | ✅ | `traces-apm-*` → engine 合成 `traces-merged-*` |
| 例外數、錯誤 log 樣式 | ✅ | `logs-apm.error-*`（每個服務都有） |
| 一般 log 行數 | ⚠️ 只有開 log shipping 的服務 | `logs-apm.app.<服務>-*` |
| 告警分部門（dept、group） | ⚠️ 要 agent 設 `labels.dept`、`labels.group` | 沒設全部 `unassigned` |
| Pod CPU、記憶體、同儕比 CPU | ❌ 公司的 agent 沒送 system metrics | 有 `metrics-apm.app.*` 時打開 `series.yaml` 註解掉的兩條 |
| K8s 狀態（重啟、Pending、節點）、kubelet、cAdvisor、ntp、JVM／.NET runtime 規則 | ❌ APM 不收 | — |
| 例外吞噬檢測 | ❌ | APM 的例外是獨立文件，沒接回 span |
| 資料品質檢查、慢性病清單 | ❌ 沒做 | 整份是 OTel 專用 |

## 怎麼接

### 1. ES：給 engine 一把 key

engine 要**讀** APM 的三種資料，**寫**自己的 `aiops-*` 和 `traces-merged-*`。只有寫沒有讀跑不起來；
`aiops-*` 的索引名是 `aiops-anomalies`、`aiops-alerts`…（沒有連字號後綴），role 的 pattern 要用 `aiops-*`。權限跟
[elasticsearch-setup.sh](../../../../elasticsearch/code/scripts/elasticsearch-setup.sh) 發的 baseline key 一樣：

```json
PUT /_security/role/aiops_engine_role
{
  "cluster": [],
  "indices": [
    {"names": ["traces-apm*", "logs-apm*", "metrics-apm*"], "privileges": ["read", "view_index_metadata"]},
    {"names": ["aiops-*", "traces-merged-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}
  ]
}
```

```json
POST /_security/api_key
{"name": "aiops-engine", "role_descriptors": {"aiops_engine_role": {"indices": [
  {"names": ["traces-apm*", "logs-apm*", "metrics-apm*"], "privileges": ["read", "view_index_metadata"]},
  {"names": ["aiops-*", "traces-merged-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}]}}}
```

回傳的 `encoded` 就是 `ES_API_KEY`。

### 2. ES：`traces-merged-*` 的 template

`aiops-*` 的索引 engine 自己建（帶 mapping）；`traces-merged-*` 一天一個 index，mapping 要 nested，得先放 template
（沒有 es-setup 時手動放；只新增，不碰現有索引）。ILM 的 `__TRACES_DELETE_AFTER__` 換成要留的天數（我們是 7d）：

```bash
curl -u ... -X PUT "$ES_URL/_ilm/policy/traces-merged" -H 'Content-Type: application/json' -d "$(sed 's/__TRACES_DELETE_AFTER__/7d/' elasticsearch/code/storage/ilm-traces-merged.json)"
```

```bash
curl -u ... -X PUT "$ES_URL/_index_template/traces-merged" -H 'Content-Type: application/json' -d @elasticsearch/code/storage/template-traces-merged.json
```

### 3. 部署：`source: apm`

[helmchart](../../../helmchart/values.yaml) 的 `source` 設 `apm`：環境變數 `AIOPS_SOURCE=apm`（程式裡寫死的那幾個欄位名換成 APM 的：dept／group
查 `metrics-apm.transaction.1m-*` 的 `labels.*`、根因分析讀 log 用 `service.name`、`trace.id`），排程表換成 [schedule.yaml](schedule.yaml)（指令同，設定檔指到這個資料夾）。

```bash
helm upgrade --install aiops aiops-engine/helmchart -n aiops --set source=apm --set esUrl=https://<es>:9200 -f aiops-engine/helmchart/values-local.yaml
```

其餘（Secret、LLM、回饋網頁）跟 [aiops-engine/README.md](../../../README.md) 一樣。

### 4. 先確認資料對得上

部署前在有 ES 連線的地方跑一次，看每條序列查得到幾筆（0 筆就是索引名或欄位名不對）：

```bash
cd aiops-engine/code && AIOPS_SOURCE=apm ES_URL=https://<es>:9200 ES_API_KEY=<key> python3 -m detector detect --config configs/apm/series.yaml --window 10m
```

```bash
cd aiops-engine/code && AIOPS_SOURCE=apm ES_URL=https://<es>:9200 ES_API_KEY=<key> python3 -m detector trace-merge --config configs/apm/trace.yaml
```

要對照的欄位（各拿一筆 `GET <索引>/_search?size=1` 看）：

| 索引 | 這裡假設的欄位 | 不一樣時改哪裡 |
|---|---|---|
| `metrics-apm.transaction.1m-*` | `transaction.duration.histogram`（histogram、微秒）、`transaction.name`、`transaction.type: request`、`event.outcome`、`service.name`、`service.node.name` | `series.yaml`、`rules.yaml`、`peer.yaml` |
| `metrics-apm.service_destination.1m-*` | `span.destination.service.resource`、`span.destination.service.response_time.count`／`.sum.us` | `series.yaml` |
| `traces-apm-*` | `processor.event`、`trace.id`、`transaction.id`／`span.id`、`parent.id`、`*.duration.us`、`event.outcome`、`span.type`、`span.destination.service.resource`、`span.db.statement`、`kubernetes.pod.name`、`labels.*`、`event.ingested` | `core/traces.py` 的 `_apm_span` |
| `logs-apm.app.*`、`logs-apm.error-*` | `service.name`、`log.level`、`message`／`error.exception.message`、`trace.id`、`processor.event: error` | `core/source.py` 的 `apm.log` |

## 跟 OTel 版差在哪（調門檻時要知道）

| | OTel（我們的 Collector） | APM |
|---|---|---|
| 延遲的單位 | 秒 | 微秒；序列用 `scale: 1.0e-6` 換成秒，`min_abs`、`threshold`、`gt` 照樣寫秒 |
| 直方圖的桶 | OTel 預設 14 個固定邊界，p95 的 σ 下限是半個桶寬（`buckets`） | HDR，桶寬約值的 1%，不設 `buckets` |
| API 的 key | `service`、`method`、`route` | `service`、`route`（`transaction.name` 含 method） |
| 錯誤 | 狀態碼 ≥ 500 | `event.outcome: failure`（agent 判：5xx、沒接住的例外） |
| 最細粒度 | 30 秒 | 1 分鐘（apm-server 每分鐘算一次） |
| 往外呼叫 | `http.client.request.duration` 直方圖 → p95 | 次數、總和 → 平均（`div`） |
| 入口（entries） | `kind` Server／Consumer | transaction（`transaction.type` request → Server、messaging → Consumer） |
