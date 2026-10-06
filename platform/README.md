# platform — 平台這一側的設定

| 資料夾 | 內容 | 狀態 |
| --- | --- | --- |
| `otel/` | OpenTelemetry Collector：收程式與 server 的健康度數值、每個容器的 log，貼標籤，送進 Elasticsearch | 已在本機 k3d 實測 |
| `argocd/applicationset.yaml` | 有 ArgoCD 的環境用：一個 app 長出一個 Application，並把 `observability` 貼成 namespace 的 label | 留著，沒有實測 |

## `otel/` 的四個檔

| 檔案 | 內容 | 每個 cluster 一樣嗎 |
| --- | --- | --- |
| `common.yaml` | namespace `obs`，以及允許的部門清單、預設的部門、server 算哪個部門 | 一樣 |
| `site-ctsp.yaml` | 這個 cluster 的廠區、cluster 名稱、Elasticsearch 的位址 | 每個 cluster 一份 |
| `cluster-collector.yaml` | 整個 cluster 一個：抓程式吐的數值、K8s 回報的狀態 | 一樣 |
| `node-collector.yaml` | 每個節點一個：server 的 CPU、記憶體、磁碟、網路，每個容器的資源用量，以及每個容器印的 log | 一樣 |

⚠️ `site-ctsp.yaml` 的 `CLUSTER` 與 `ES_ENDPOINT` 是佔位，上線前要換掉。`ES_ENDPOINT` 沒換的話
收集程式會一直重試、資料一直排隊，不會報錯。

## 資料會被標成哪個部門

| 資料 | `tenant` 怎麼來 |
| --- | --- |
| 程式吐的數值、每個容器的資源用量、容器重啟次數、容器印的 log | pod 所在 namespace 的 `tenant` 門牌 |
| namespace 沒掛門牌，或值不在清單裡 | `platform` |
| server 本身的數值、節點是不是 Ready | `icsd`，不管那個節點上跑的是誰的程式 |

每筆另外帶 `dept`、`system`（門牌有的話）、`site`、`cluster`。Elasticsearch 裡每個部門一份
資料流，名字結尾是部門代號，例如 `metrics-prometheusreceiver.otel-tsid`、`logs-generic.otel-tsid`。
同一個 pod 的數值與 log，這幾個標籤的值一模一樣，是同一份規則貼的。

## app 要做什麼才會被抓

pod 上加兩個標註，namespace 掛門牌。範例見 `app/demo-svc/k8s/templates/`。

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9464"
```

## log 怎麼收

app 不用做任何事：印到標準輸出、標準錯誤的都會被收，沒掛門牌的進 `platform`。

| 項目 | 行為 |
| --- | --- |
| 一行是 JSON | 拆成欄位，原文留在 `body.text`。欄位在 `attributes.` 底下，例如 `attributes.trace_id` |
| 一行不是 JSON | 整行在 `body.text`，照樣收 |
| 等級 | `level` 或 `log.level` 都會放進 `severity_text`（一律大寫）。查「所有錯誤」用這個 |
| 訊息 | `msg` 或 `message` 都會放進 `attributes.message` |
| 時間 | 容器印出那一行的時間，不是收集程式讀到的時間 |
| 跨好幾行的 log（錯誤堆疊） | 每行各自一筆，不合併 |
| 寫到容器裡檔案的 log | 收不到 |
| 收集程式自己的 log | 不收 |

⚠️ 兩支程式用同一個欄位名稱但型別不同時（一支的 `code` 是數字，另一支是文字），兩筆都進得去，
但後到的那一筆裡那個欄位查不到，只能從原文找。

⚠️ 收集程式第一次到一個節點時，會把節點上現有的 log 全部讀進來。之後重啟不會重讀。
正式上線的第一次要挑時間。

## 部署

下面是本機 k3d 的寫法。正式環境把 `--context` 換成該 cluster 的，由人執行。

```bash
kubectl --context k3d-obs-test apply -f platform/otel/common.yaml
```

```bash
kubectl --context k3d-obs-test -n obs create secret generic es-credentials --from-literal=username=<帳號> --from-literal=password=<密碼>
```

```bash
kubectl --context k3d-obs-test apply -f platform/otel/site-ctsp.yaml -f platform/otel/cluster-collector.yaml -f platform/otel/node-collector.yaml
```

⚠️ 設定放在 ConfigMap 裡，改了之後收集程式不會自己重啟：

```bash
kubectl --context k3d-obs-test -n obs rollout restart statefulset/otel-cluster-collector daemonset/otel-node-collector
```

## 加一個部門

改 `otel/common.yaml` 的 `ALLOWED_TENANTS`（用 `|` 隔開，不能有空白），重新套用並重啟兩份
收集程式。`tests/validate.sh` 也是讀這一份，不用另外改。

## 加一個 cluster

複製 `site-ctsp.yaml` 成 `site-<廠區>.yaml`，改裡面三個值。那個 cluster 只套自己的那一份。

## 第二座 Elasticsearch

`cluster-collector.yaml` 裡有一段註解掉的 `elasticsearch/second`，沒有啟用、沒有測過。
啟用的步驟寫在那段註解裡。不要用假的位址啟用它：收集程式會一直重試，佇列越積越多。

## 改錯了不會報錯的地方

這幾個地方改錯了，收集程式照樣起得來，資料只是安靜地掉或進錯部門。
`tests/validate.sh` 會擋下，設定檔裡也都標了 ⚠。

| 位置 | 改錯的後果 | 怎麼知道的 |
| --- | --- | --- |
| `retry.max_retries` 寫 0 | 是「不重試」。Elasticsearch 一連不上資料就直接丟掉 | 實測 |
| 佇列不放 PVC（或節點的磁碟） | 收集程式一重啟，排隊中的資料就沒了 | 實測 |
| 拿掉 `transform/tenant` 裡丟掉樣本自帶欄位的那幾行 | 程式自稱的 `tenant`、`site` 會留在樣本上 | 實測 |
| 拿掉 `transform/pre` 裡放 `k8s.pod.ip` 的那一行 | 讀門牌時對不到 pod，程式的資料全部進 `platform` | 先前的試驗實測 |
| 拿掉 `cumulative_to_delta` | 處理時間這種分布圖整個被丟掉，log 只有一行警告 | 實測 |
| `file_log` 拿掉 `storage` | 收集程式一重啟就從頭重讀，每行 log 重複一次 | 靜態檢查 |
| `file_log` 拿掉 `exclude` | 收集程式讀到自己的 log，越印越多 | 靜態檢查 |
| 拿掉 `transform/tenant` 裡丟掉 log 自帶欄位的那幾行 | 程式在 log 裡自稱的 `tenant`、`site` 會留在資料上 | 實測 |
| `filesystem` 用預設值 | 根目錄是 overlay 的節點一筆磁碟資料都沒有 | 實測（k3d） |
| 兩份收集設定的標籤規則不一樣 | 同一種資料被標成不同的部門 | 靜態檢查 |

## 節點那一份要的權限

| 權限 | 為什麼 |
| --- | --- |
| 用 root 執行 | 要讀主機的 `/proc`、`/sys`，也要寫主機上的佇列目錄 |
| 唯讀掛載主機的 `/` 到 `/hostfs` | 讀 server 的 CPU、記憶體、磁碟、網路 |
| 讀寫主機的 `/var/lib/otelcol-node` | 佇列。放在節點上，pod 重建也還在 |
| `nodes/stats`、`nodes/proxy` 的讀取 | 向 kubelet 要每個容器的資源用量 |

⚠️ 向 kubelet 取資料時沒有驗證它的憑證（`insecure_skip_verify: true`）。正式環境要不要改，
看那邊 kubelet 的憑證是誰簽的。

## 還沒做、沒測的

| 項目 | 狀態 |
| --- | --- |
| trace | 還沒收。總綱第 4 步 |
| 節點本身的系統 log | 只收容器的 |
| log 留多久、要不要只留某些等級 | 現在全收、不刪 |
| log 量很大時收集程式夠不夠力 | 只用範例程式的量驗過，每秒十行左右 |
| 誰能看哪個部門的資料 | 資料流已經依部門分開，權限還沒設 |
| 多個節點 | 本機的 k3d 只有一個節點 |
| 真的 server | 只在 k3d 的節點（一個 docker 容器）上測過。磁碟的過濾規則在真的節點上要再看一次 |
| 正式環境的 Elasticsearch | 本機的沒有開 TLS |
