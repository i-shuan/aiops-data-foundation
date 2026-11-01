# elasticsearch：中央 ES 的結構設定（③）

> **類型：操作 How-To**　原理見 [docs/architecture.md](../docs/architecture.md)、[docs/retention.md](../docs/retention.md)

正式環境用公司的中央 Elasticsearch 8.19，**不由這個 repo 部署**；這裡放的是套在它上面的結構設定與腳本。

| 資料夾 | 內容 |
|---|---|
| `code/storage/` | mapping、ingest pipeline（log 解析、OTel log 轉形、trace 的 `_id`）、ILM、index／component template、transform（log 統計）、HAProxy 健康檢查範例 |
| `code/scripts/` | `elasticsearch-setup.sh`（把 `storage/` 全部套上去、建角色、發 key）、`retention.conf`（保留天數）、`rotate-writer-key.sh`（換寫入 key 不掉資料） |
| `helmchart/` | **只給測試用**：兩個資料節點＋一個只投票節點的 ES（可改回單節點）。正式環境不要拿這份去部署 |

## 套結構設定、發 key

```bash
ES_URL=https://<中央 ES>:9200 ES_AUTH=elastic:<password> elasticsearch/code/scripts/elasticsearch-setup.sh
```

重跑是安全的（已存在的只更新）。印出四把 key：寫入 key（給 Collector 的 Secret `es-writer-keys`）、AIOps baseline key（給 engine 的 Secret `aiops-secrets`）、Grafana 唯讀 key、健康檢查 key。保留天數改 `retention.conf` 再重跑；細節見 [docs/retention-how-to.md](../docs/retention-how-to.md)。

## 測試用 ES

```bash
helm upgrade --install elasticsearch elasticsearch/helmchart -n es-test --create-namespace     # 或 e2e/k8s/deploy-es.sh
```

預設三個 Pod：兩個資料節點（`elasticsearch-0`、`-1`，heap 1g、PVC 10Gi）加一個只投票的節點（`elasticsearch-voter-0`，`node.roles: master,voting_only`，heap 256m、不放資料）。三個都有投票權，掛掉任何一個資料節點叢集仍選得出主；掛有投票權的資料節點時叢集能不能寫、Collector 掉不掉資料，實測見 [es-two-pods PDCA](../docs/pdca/phase1-collect/2026-11-es-two-pods.md)。開 security、HTTP 不走 TLS、密碼 `changeme-test-only`。裝好之後對它跑上面的 setup；`run-e2e.sh` 會做這兩步並把 key 建成 Secret。

- 單節點：`e2e/k8s/deploy-es.sh --set replicas=1 --set twoNode.enabled=false`（只投票節點隨 `twoNode` 一起關）。
- `deploy-es.sh` 會自己產節點之間的 TLS 憑證（Secret `es-transport-certs`），單節點升多節點時先讓 `elasticsearch-0` 換成多節點設定再放大，否則新 Pod 找不到 master、舊 Pod 永遠不更新。
- 測試機可開檔案數不足，transport 只綁 127.0.0.1、由同一個 Pod 的 socat 轉給其他 Pod（headless Service `es-node-0/1/2`，port 9300/9301/9302）。正式環境不需要這段。

### 前面放 Ingress／Gateway 時流量怎麼導

| 要做的 | 怎麼做 |
|---|---|
| 後端只有資料節點 | 後端指向 Service `elasticsearch`（selector `app: elasticsearch, role: data`），或自己建一個同 selector 的 Service。不要列 Pod IP，也不要用 `es-node-*`（那是節點之間的 transport） |
| 不會導到只投票的節點 | 它的 label 是 `app: elasticsearch-voter, role: voting-only`，Service 選不到；而且它的 HTTP 只綁 127.0.0.1，就算直接打 Pod IP 的 9200 也連不上（chart 的 `http.host`） |
| 健康檢查 | Pod 的 readiness 已用健康檢查 key 打 `GET /`，Service 只給 ready 的 Pod；Gateway 自己的健康檢查打同一個路徑即可（`code/storage/` 有 HAProxy 的範例） |
| 正式環境照這個拓樸 | 節點數由管 ES 的人決定；只投票節點 `node.roles: [master, voting_only]`、9200 不對外；LB／Gateway 後端只列資料節點（或協調節點） |
