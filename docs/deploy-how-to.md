# 部署：公司已有 Elastic APM 與 ES，只裝 AIOps

> **類型：操作 How-To**　原理見 [architecture.md](architecture.md)・APM 的欄位與能做到什麼見 [aiops-engine/code/configs/apm/README.md](../aiops-engine/code/configs/apm/README.md)

收值（APM agent、apm-server）和 Elasticsearch 都是公司現有的，**不用部署**。要做的只有三件：在公司 ES 上手動補一次設定、部署 aiops-engine、（要發通知時）接 Grafana。不跑 `es-setup`。

| 順序 | 做什麼 | 誰做 | 必要 |
|---|---|---|---|
| 0 | APM agent 的三個設定 | 應用團隊、公司 APM 團隊 | ✅ |
| 1 | 在公司 ES 補設定：`traces-merged` 的 ILM 與 template、engine 的角色與 key | 有 ES 管理權限的人（Kibana Dev Tools） | ✅ |
| 2 | 部署 aiops-engine | 我們 | ✅ |
| 3 | 接 Grafana（通知、儀表板、回饋表單） | 我們 | 要發通知時 |

## 0. APM agent 的設定

| 設定 | 為什麼 | 沒做的話 |
|---|---|---|
| transaction sample rate **1.0**（不取樣） | trace-merge 要拿到一個請求的每一段 | 根因分析的 trace 不完整 |
| 全域標籤 `labels.dept`、`labels.group` | 告警依部門分送 | 全部歸 `unassigned`，送平台 |
| 開 log shipping（log 帶 `trace.id`） | 根因分析撈同一個請求在各服務的 log | 只看得到 `logs-apm.error-*` 的例外 |

## 1. 在公司 ES 補設定（Kibana → Dev Tools）

不跑 `es-setup` 的話，以下四段貼一次。全部是新增，不碰 APM 的任何索引。

**1-1. `traces-merged` 的保留天數（ILM）**：engine 合成的一個請求一筆，留 7 天。

```
PUT _ilm/policy/traces-merged
{"policy": {"phases": {"hot": {"actions": {}}, "delete": {"min_age": "7d", "actions": {"delete": {}}}}}}
```

**1-2. `traces-merged` 的 index template**：`spans`、`entries` 要是 nested，沒有這個 template 根因分析會查詢失敗。內容整份貼 [elasticsearch/code/storage/template-traces-merged.json](../elasticsearch/code/storage/template-traces-merged.json)：

```
PUT _index_template/traces-merged
<template-traces-merged.json 的內容>
```

**1-3. engine 的角色**：讀 APM 的三種資料，只能寫自己的 `aiops-*`、`traces-merged-*`。只有寫、沒有讀，engine 讀不到資料。

```
PUT _security/role/aiops_engine_role
{
  "cluster": [],
  "indices": [
    {"names": ["traces-apm*", "logs-apm*", "metrics-apm*"], "privileges": ["read", "view_index_metadata"]},
    {"names": ["aiops-*", "traces-merged-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}
  ]
}
```

**1-4. engine 的 API key**：

```
POST _security/api_key
{"name": "aiops-engine", "role_descriptors": {"aiops_engine_role": {"indices": [
  {"names": ["traces-apm*", "logs-apm*", "metrics-apm*"], "privileges": ["read", "view_index_metadata"]},
  {"names": ["aiops-*", "traces-merged-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}]}}}
```

回傳的 `encoded` 下一步用；不要貼進 git、聊天或 log。

確認：

| 要確認的 | 怎麼看 |
|---|---|
| template 在 | `GET _index_template/traces-merged` |
| ILM 在 | `GET _ilm/policy/traces-merged` |
| key 讀得到 APM | 用這把 key 打 `GET metrics-apm.transaction.1m-*/_count`，大於 0 |

## 2. 部署 aiops-engine

```bash
cd aiops-engine/code && docker build -t <nexus>/aiops-engine:<tag> . && docker push <nexus>/aiops-engine:<tag>   # 內網：Dockerfile 的 pip 指到 Nexus
```

```bash
kubectl create namespace aiops
```

```bash
kubectl -n aiops create secret generic aiops-secrets --from-literal=ES_API_KEY=<1-4 的 encoded>
```

```bash
cp aiops-engine/helmchart/values-local.example.yaml aiops-engine/helmchart/values-local.yaml   # 填 LLM 的 provider、model、baseUrl、apiKey；不進 git
```

```bash
helm upgrade --install aiops aiops-engine/helmchart -n aiops -f aiops-engine/helmchart/values-local.yaml \
  --set source=apm --set image.repository=<nexus>/aiops-engine --set image.tag=<tag> --set esUrl=https://<公司 ES>:9200
```

- **`--set source=apm` 一定要帶**：預設是 `otel`（讀 Collector 寫的格式），不帶就讀不到 APM 的資料。GitLab CI 的 `deploy-aiops-engine` 目前沒帶這個參數，用 CI 部署前要先補。
- 排程器預設 **stage 1**：rules、detect、alert、trace-merge、train。跑穩再 `--set scheduler.stage=2` 開 Pod 同儕比對、trace 每一段的基準線。
- 回饋網頁的網址要放進通知時：`configs/apm/rca.yaml` 的 `alerts.feedback_url`、`report_url`，用 `--set-file configOverrides.apm.rca=<改好的 rca.yaml>` 蓋掉。

確認：

| 要確認的 | 怎麼看 |
|---|---|
| Pod 起來 | `kubectl -n aiops get pod`：`aiops-engine-…` 2/2 |
| 排程在跑 | `kubectl -n aiops logs deploy/aiops-engine -c scheduler -f`：每分鐘有 rules、detect、alert、trace-merge |
| 讀得到資料 | Kibana 看 `traces-merged-*` 開始有資料、`aiops-alerts` 有 `heartbeat` 那筆 |
| 基準線 | 每天 02:00 train；資料要累積 2 週才有「同星期幾 × 同小時」，之前只有靜態臨界值規則在報 |

## 3. 接 Grafana（要發通知時）

engine 只把告警寫進 `aiops-alerts`，**不自己發通知**。先只寫入、不通知跑 2～4 週，期間用 Kibana 看 `aiops-alerts`；要通知時再接 Grafana。細節見 [grafana/README.md](../grafana/README.md)。

**3-1. 準備的東西**

| 項目 | 內容 | 哪來 |
|---|---|---|
| Grafana 的唯讀 key | 讀 `aiops-alerts`、`aiops-anomalies`、`aiops-tuning`、`aiops-feedback`，加 cluster `monitor`（Grafana 會先打 `GET /` 看版本） | 下面的 Dev Tools 指令 |
| 各部門的通知管道 | `NOTIFY_IT_URL`、`NOTIFY_IMC_URL`、`NOTIFY_PLATFORM_URL`（範例是 webhook；Teams、Email 改 `contact-points.yaml`） | 各部門、平台團隊 |
| dept 清單 | `policies.yaml` 的 dept 要跟 APM agent 的 `labels.dept` 值一樣 | 第 0 節的標籤 |
| 回饋表單外掛 | Business Forms（`volkovlabs-form-panel`）**6.3.1**；Grafana 12.2、13.0.7 實測過 | Nexus，或 repo 的 `grafana/plugins/`（git lfs） |
| 回饋網頁的網址 | 使用者**瀏覽器**連得到的 engine 回饋網頁（Service `aiops-feedback:8080` 前面加 Ingress） | 我們 |

```
POST _security/api_key
{"name": "grafana-aiops", "role_descriptors": {"grafana-aiops": {"cluster": ["monitor"], "index": [
  {"names": ["aiops-alerts", "aiops-anomalies", "aiops-tuning", "aiops-feedback"], "privileges": ["read", "view_index_metadata"]}]}}}
```

**3-2. 送進 Grafana**：內容都在 `grafana/code/`（data source、3 條告警規則、contact point、policy、3 個儀表板），依公司有沒有 Grafana 選一種：

| 情況 | 怎麼做 |
|---|---|
| 公司已有 Grafana | `grafana/code/` 的檔交給管 Grafana 的人放進 provisioning 資料夾；Grafana 要有環境變數 `ES_URL`、`GRAFANA_ES_API_KEY`、`NOTIFY_*_URL`；外掛沒裝就照 [INSTALL-form-plugin.md](../grafana/code/INSTALL-form-plugin.md) |
| 公司用官方 chart 管 Grafana | `grafana-community/grafana` 加 `-f grafana/values-official.yaml`（GitLab CI 的 `deploy-grafana` 就是這樣） |
| 沒有 Grafana | 用 `grafana/helmchart/` 裝一台（Secret `grafana-env` + `helm upgrade`，指令在 [grafana/README.md「部署」](../grafana/README.md#部署)） |

**3-3. 跟 engine 接起來的三個網址**

| 改哪裡 | 改成 |
|---|---|
| `grafana/code/dashboards/aiops-feedback.json` 裡兩個 `http://localhost:18080` | 回饋網頁的 Ingress 網址 |
| engine 部署加 `--set feedback.corsOrigin=https://<Grafana 網址>` | 表單從瀏覽器直接打回饋網頁，要開 CORS |
| `alert-rules.yaml` 的 `feedback` annotation、engine `configs/apm/rca.yaml` 的 `alerts.feedback_url`、`report_url` | Grafana 與回饋網頁的網址（通知裡的連結） |

確認：Grafana 的 AIOps 資料夾有 `aiops-rca`、`aiops-learning`、`aiops-feedback` 三個儀表板；Alerting 有 `aiops-incident`、`aiops-heartbeat`、`aiops-baseline-missing` 三條規則；data source 測試通過。

## 可以晚點做的

| 項目 | 什麼時候需要 | 怎麼做 |
|---|---|---|
| snapshot 備份 | 有老師傅回饋、事故紀錄之後（掉了不能重算） | [elasticsearch/README.md「備份與還原」](../elasticsearch/README.md#備份與還原aiops--的人工判斷) |
| 開通知 | 只寫入跑 2～4 週、誤報率可接受之後 | 第 3 節 |
| stage 2 | stage 1 跑穩之後 | `--set scheduler.stage=2` |
