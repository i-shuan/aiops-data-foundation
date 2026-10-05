# Plan — 健康度數值經 OpenTelemetry Collector 進 Elasticsearch

> `2-spec.md` 與這份 plan 已確認（2026-10-05）。分三批，前一批驗收過才做下一批，每批一個 commit。
> 檔案數超過 3，沿用之前的做法：每批各自驗證。

## 第一批：兩支範例程式

做完 `./tests/validate.sh` 要 `PASS`，而且兩支程式在本機 k3d 上跑得起來、弄得壞。

1. `app/demo-api/`：收到請求做一點假的運算後回應。吐請求數、處理時間、進行中的請求數。
2. `app/demo-web/`：收到請求後呼叫 `demo-api`。自己定時打自己，產生流量。
3. 兩支都有一個管理用的網址，可以開關四種故障：變慢、回錯誤、佔 CPU、佔記憶體。
4. 兩支都印 JSON 格式的 log，帶 trace 的編號；呼叫時把編號傳下去。這一步不收，只確認有吐。
5. 各自的 `k8s/`：`namespace.yaml` 建 namespace 並貼門牌，pod 上標註「我要被抓」與埠號。
6. 在 `obs-test` 上用 `helm` 部署，確認數值吐得出來、把 `demo-api` 弄慢時兩支的處理時間都變長。

| 檔案 | 內容 |
| --- | --- |
| `app/demo-api/main.py`、`Dockerfile`、`requirements.txt` | 程式 |
| `app/demo-api/k8s/`（`Chart.yaml`、`values.yaml`、三個 template） | 部署 |
| `app/demo-web/` 同樣一組 | 同上 |

## 第二批：正式的收集設定與靜態檢查

1. `platform/otel/`：把 `spike/collector.yaml` 整理成正式的，分成兩份。
2. cluster 那一份：抓程式的數值、K8s 回報的狀態。佇列掛 PVC。
3. 節點那一份：每個節點一個，收 server 的 CPU、記憶體、磁碟、網路，以及每個容器的資源用量。
4. 標籤規則照 spec：程式讀門牌、server 標 `icsd`、其餘 `platform`。`site` 與 `cluster` 由每個 cluster 自己的一小份設定提供。
5. 第二座 Elasticsearch 的位置用註解保留在設定裡，不啟用。
6. 允許的 `tenant` 清單只寫在收集設定裡；`tests/validate.sh` 改成從這裡讀，腳本裡暫時的那份拿掉。
7. `tests/validate.sh` 加檢查：重試不能是 0、先丟掉程式自己帶的再填、對 pod 的那一段還在、server 有標 `icsd`、密碼沒有寫在檔案裡。每一條加「改壞一定被擋下」的自我檢查。

| 檔案 | 內容 |
| --- | --- |
| `platform/otel/cluster-collector.yaml` | 步驟 2、4 |
| `platform/otel/node-collector.yaml` | 步驟 3、4 |
| `platform/otel/site-ctsp.yaml` | 這個 cluster 的 `site` 與 `cluster` |
| `tests/validate.sh` | 步驟 6、7 |

## 第三批：k3d 實測與文件

1. `tests/obs/run.sh`：在 `obs-test` 上建 Elasticsearch、套 `platform/otel/`（只換位址與憑證）、部署範例程式。
2. 情境：`tsid`、`bsid` 各一組範例程式，加上沒貼、打錯、冒充三個假程式。
3. `check.py` 查 Elasticsearch：每個情境進了哪一份、標籤對不對、server 的在 `icsd`、容器的資源用量有進來。
4. 弄壞的檢查：把 `demo-api` 弄慢，兩支的處理時間在 Elasticsearch 裡都看得到變化。
5. `run.sh outage`：停掉 Elasticsearch、中間重啟收集程式、再恢復，比停機期間的筆數。
6. 反向驗證：用改壞的設定跑（重試改成 0、拿掉擋冒充、拿掉 PVC），每一種都要 FAIL。
7. 文件：`README.md`、`platform/README.md`、`.agents/project/commands.md`。
8. 刪掉 `requirements/health-metrics-to-elastic/spike/`：正式的設定與測試蓋過它之後才刪。

| 檔案 | 內容 |
| --- | --- |
| `tests/obs/run.sh`、`check.py`、`backends.yaml` | 新增 |
| `README.md`、`platform/README.md`、`.agents/project/commands.md` | 更新 |
| `requirements/health-metrics-to-elastic/spike/` | 刪除 |

## 風險與退路

| 風險 | 怎麼發現 | 退路 |
| --- | --- | --- |
| 收集程式重啟後佇列不見 | 第三批的 `outage`：補回的筆數不到 95% | 檢查 PVC 有沒有真的掛在佇列的目錄。spike 沒測過這一項 |
| server 與容器的數值對不到節點或 pod，標籤是空的 | 第三批：`icsd` 那一份沒有資料，或容器的資料全在 `platform` | 調整對 pod 的那一段。這是 spike 沒涵蓋的新東西，預期要試幾次 |
| 容器的資源用量被算成「屬於哪個部門」時對錯 | 第三批：`tsid` 的 pod 的 CPU 出現在別的部門 | 規則是照 pod 所在的 namespace，不是照節點 |
| 節點那一份要讀主機的資料，需要比較高的權限 | pod 起不來，或起來了沒資料 | 只開需要的掛載與權限，在文件寫明 |
| 範例程式的假流量不穩，測試偶爾誤判 | 同一個設定連跑兩次結果不同 | 檢查用「有沒有、進對地方沒」而不是比精確的數字；比數字的地方留容許範圍 |
| 弄慢 `demo-api` 之後 `demo-web` 的變化太小看不出來 | 第三批步驟 4 | 把延遲調大到明顯超過正常波動 |
| 允許的 `tenant` 清單改成從收集設定讀時，寫法太難解析 | `tests/validate.sh` 讀不出來或讀錯 | 清單在設定裡用固定的寫法，腳本對那個寫法做檢查，寫法變了就 FAIL |
| 刪掉 `spike/` 之後才發現正式的設定少了什麼 | 第三批的驗收沒過 | 驗收全過才刪；刪掉的在 git 歷史裡 |

## 不在這個 plan 裡的

| 不做 | 為什麼 |
| --- | --- |
| 收 log 與 trace | 總綱第 2、4 步。範例程式會吐，但收集設定這一步不接 |
| Kibana 與儀表板 | spec 已列為不涵蓋。用查詢確認 |
| 部門的權限 | spec 已列為不涵蓋 |
| 分層保留與壓縮 | spec 已列為不涵蓋 |
| 幫 `mes-dispatch` 補上「我要被抓」的標註、改成手動 Helm | 它是 ArgoCD 的寫法，另外決定 |
| 用 Helm chart 包收集程式 | 先用單純的設定檔。很多 cluster 時怎麼發設定是之後的事 |
| 第二座 Elasticsearch 的測試 | 已定先一座 |
| 動 `platform/argocd/`、`platform/logging/` | 這一步不碰 |
