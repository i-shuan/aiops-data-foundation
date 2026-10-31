# elasticsearch：中央 ES 的結構設定（③）

> **類型：操作 How-To**　原理見 [docs/architecture.md](../docs/architecture.md)、[docs/retention.md](../docs/retention.md)

正式環境用公司的中央 Elasticsearch 8.19，**不由這個 repo 部署**；這裡放的是套在它上面的結構設定與腳本。

| 資料夾 | 內容 |
|---|---|
| `code/storage/` | mapping、ingest pipeline（log 解析、OTel log 轉形、trace 的 `_id`）、ILM、index／component template、transform（log 統計）、HAProxy 健康檢查範例 |
| `code/scripts/` | `elasticsearch-setup.sh`（把 `storage/` 全部套上去、建角色、發 key）、`retention.conf`（保留天數）、`rotate-writer-key.sh`（換寫入 key 不掉資料） |
| `helmchart/` | **只給測試用**：單節點 ES（`run-es-2node.sh` 可切成兩節點）。正式環境不要拿這份去部署 |

## 套結構設定、發 key

```bash
ES_URL=https://<中央 ES>:9200 ES_AUTH=elastic:<password> elasticsearch/code/scripts/elasticsearch-setup.sh
```

重跑是安全的（已存在的只更新）。印出四把 key：寫入 key（給 Collector 的 Secret `es-writer-keys`）、AIOps baseline key（給 engine 的 Secret `aiops-secrets`）、Grafana 唯讀 key、健康檢查 key。保留天數改 `retention.conf` 再重跑；細節見 [docs/retention-how-to.md](../docs/retention-how-to.md)。

## 測試用 ES

```bash
helm upgrade --install elasticsearch elasticsearch/helmchart -n es-test --create-namespace     # 或 e2e/k8s/deploy-es.sh
```

單節點、開 security、HTTP 不走 TLS、密碼 `changeme-test-only`、PVC 10Gi。裝好之後對它跑上面的 setup；`run-e2e.sh` 會做這兩步並把 key 建成 Secret。兩節點（選 master、副本、滾動重啟的實測）用 `e2e/k8s/run-es-2node.sh up`。
