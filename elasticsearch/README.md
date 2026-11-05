# elasticsearch：在公司 ES 上補 AIOps 需要的東西（③）

> **類型：操作 How-To**　原理見 [docs/architecture.md](../docs/architecture.md)

收值是公司現有的 Elastic APM，資料已經在公司的 Elasticsearch 8.19（`traces-apm*`、`logs-apm*`、`metrics-apm*`），**這個 repo 不收值、不部署 ES、不改 APM 寫入的任何索引**。這裡只放套在它上面的兩件事：

| 資料夾 | 內容 |
|---|---|
| `code/storage/` | `traces-merged-*` 的 ILM 與 index template（trace-merge 一個請求一筆，nested，要先有 template） |
| `code/scripts/` | `elasticsearch-setup.sh`（套上面兩個、發 engine 與 Grafana 的 key、註冊 snapshot）、`retention.conf`（`traces-merged` 的保留天數與 snapshot 設定） |
| `setup-helmchart/` | 在 K8s 裡用 Job 跑 `elasticsearch-setup.sh`（`esSetup.enabled` 開關，預設不跑）；連不到內網 ES 時用 |

`aiops-*` 的索引 engine 自己建（帶 mapping），不用在這裡建。

## 套結構、發 key

```bash
ES_URL=https://<公司 ES>:9200 ES_AUTH=elastic:<password> elasticsearch/code/scripts/elasticsearch-setup.sh
```

重跑是安全的（已存在的只更新）。發兩把 key：

| key | 權限 | 放哪 |
|---|---|---|
| engine | 讀 `traces-apm*`、`logs-apm*`、`metrics-apm*`；讀寫 `aiops-*`、`traces-merged-*` | engine 的 Secret `aiops-secrets` 的 `ES_API_KEY` |
| grafana | 讀 `aiops-alerts`、`aiops-anomalies`、`aiops-tuning`、`aiops-feedback`；cluster `monitor` | Grafana 的 Secret `grafana-env` 的 `GRAFANA_ES_API_KEY` |

公司的 ES 不讓這個腳本跑（沒有 `manage_security`）時，請管 ES 的人手動建：`traces-merged` 的 ILM 與 template 用 `code/storage/` 的兩個檔（ILM 的 `__TRACES_DELETE_AFTER__` 換成天數，預設 7d），engine 的角色照上表（細節見 [aiops-engine/code/configs/apm/README.md](../aiops-engine/code/configs/apm/README.md)）。保留天數改 `retention.conf` 再重跑。

## 在 K8s 裡用 Job 跑 setup

ES、AIOps、Grafana 在同一個 cluster，而你連不到內網 ES 時：用 [`setup-helmchart/`](setup-helmchart/values.yaml)，chart 用 Job 在 cluster 裡跑 `elasticsearch-setup.sh`，發的 key 直接寫進 Secret，不經過你的機器。

```bash
kubectl create namespace es-setup
kubectl -n es-setup create secret generic es-setup-admin --from-literal=ES_AUTH=<有 manage_security 的帳號:密碼>
tar czf - -C elasticsearch code | base64 -w0 > /tmp/es-code.b64       # chart 讀不到 chart 外面的檔，打包帶進去
helm upgrade --install es-setup elasticsearch/setup-helmchart -n es-setup --timeout 15m   --set esSetup.enabled=true --set esUrl=https://<ES>:9200 --set-file bundle=/tmp/es-code.b64
kubectl -n es-setup logs job/es-setup --tail=20
```

| 什麼時候 | 怎麼跑 | 發生什麼 |
|---|---|---|
| 第一次部署（engine 啟動之前） | 上面的指令 | Job 套結構、發 2 把 key：直接寫進 `aiops/aiops-secrets`、`grafana/grafana-env` 的 `GRAFANA_ES_API_KEY`，2 把都存一份在 `es-setup/es-setup-keys`；key 不印進 log |
| 改了 `retention.conf`、`storage/` | 同上，加 `--set esSetup.skipKeys=true` | 只更新結構，不發新 key、不碰 Secret |
| 要換 key | 同上 | 發新 key、寫進 Secret。**舊 key 不會作廢**，要到 ES 手動作廢 |

- `esSetup.enabled` 預設 `false`，沒開不會建 Job；日常部署 engine、Grafana 不用跑它。
- Job 只給 Secret 的 get、create、patch 權限，範圍是自己的 namespace 和 `values.yaml` 的 `targets`（namespace 要先存在）。
- `grafana-env` 兩邊各寫各的欄位（server-side apply）：Job 寫 key，Grafana 那邊寫 `ES_URL`、webhook、admin 密碼，互不蓋掉。
- 設計與實測：[pdca/phase1-collect/2026-11-es-setup-helm-job.md](../docs/pdca/phase1-collect/2026-11-es-setup-helm-job.md)；測試 cluster 跑過，公司環境還沒實跑。

## 備份與還原（aiops-* 的人工判斷）

回饋、事故紀錄、調整（`aiops-feedback`、`aiops-incidents`、`aiops-tuning`）不會自己消失、掉了不能重算，setup 會註冊每天一次的 snapshot（SLM 政策 `aiops-daily`，另外也帶 `aiops-baseline`、`aiops-alerts`；索引、保留天數在 [retention.conf](code/scripts/retention.conf)）。**儲存庫要先有**，由管 ES 的人決定類型與位置，跑 setup 時給：

```bash
SNAPSHOT_REPO_TYPE=s3 SNAPSHOT_REPO_SETTINGS='{"bucket":"es-snapshots","base_path":"aiops"}' ES_URL=… ES_AUTH=… elasticsearch/code/scripts/elasticsearch-setup.sh
```

沒給就跳過並印一行提醒（沒有備份）。

| 要做的 | 指令 |
|---|---|
| 看有哪些備份 | `GET _snapshot/aiops/_all`、`GET _slm/policy/aiops-daily`（`last_success`、`last_failure`） |
| 馬上備一次 | `POST _slm/policy/aiops-daily/_execute` |
| 還原一個索引 | 先把壞掉的關掉或刪掉：`DELETE aiops-tuning`；再 `POST _snapshot/aiops/<快照名>/_restore` 帶 `{"indices": "aiops-tuning", "include_global_state": false}`；`GET _cat/recovery/aiops-tuning` 看進度 |
| 還原到別的名字（對照用） | 同上，多帶 `"rename_pattern": "(.+)", "rename_replacement": "restored-$1"` |
| 還原後 | `aiops-baseline` 還原後不用重 train；`aiops-tuning` 還原後 `detector adjust list` 看一眼 |

快照之間只存差異（segment 檔共用），每天一次、留 30 天不會長太大；這五個索引都是 MB 等級（[data-storage-reference.md](../docs/data-storage-reference.md)）。衍生資料的保留天數是 engine 每天刪（`detector prune`），不在這裡。
