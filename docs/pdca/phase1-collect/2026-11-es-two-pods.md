# 測試用 ES 一律兩個 Pod：先把 HA 機制做起來（2026-11）

**狀態**：已完成；ES 全停時的遺失決定記為已知風險、Ingress／Gateway 實際導流沒實測　　**日期**：2026-11-01　　**相關**：分支 `claude/es-retry`（從 `claude/trace-merge` 合入 master 最新後開出）；起因見 [trace-merge 的 Act](2026-10-trace-merge.md)「ES 停 180 秒」
**模組**：③ 存、① 收值（Collector 出口對 ES 掛掉的反應）

## Plan：這一次要達到什麼

- **需求**：測試用 ES 只有一個 Pod。ES 停機時 Service 後面沒有任何可用的 Pod，Collector 的連線沒有回應（連線黑洞），ES 元件對這種逾時不重試，資料就丟（trace 遺失 35%）。要先有 HA，才分得出「剩下的遺失是 Collector 的問題」還是「單點故障的測試條件」。
- **目的**：測試用 ES 預設兩個資料節點加一個只投票的節點；掛掉任何一個資料節點時叢集仍選得出主、Service 只把流量給還活著的資料節點，Collector 不再碰到連線黑洞。要用實測確認：掛一個資料節點時 Collector 掉不掉資料、叢集本身能不能寫入；兩個資料節點都停時 Collector 掉不掉資料。前面放 Ingress／Gateway 時，流量只到資料節點、不到只投票的節點。
- **範圍**：
  - 做：`elasticsearch/helmchart` 預設兩個資料節點（原本是 `twoNode.enabled` 開關，預設關）加只投票節點；Service 只選資料節點；`deploy-es.sh` 自己產憑證、處理單節點升多節點的順序；依賴單節點的測試腳本（`elasticsearch-0`、`--replicas=1`）改成兩個 Pod；文件（含 Ingress／Gateway 怎麼導）。
  - 不做：Collector 的 ES 元件設定（連線逾時不重試）怎麼修，等量過新的遺失率再討論；正式環境的 ES 部署（節點數由管 ES 的人決定，這裡只給拓樸與實測）。
  - 第一輪只做兩個資料節點、沒有只投票節點；實測後使用者決定加只投票節點（兩個資料節點只有一個有投票權，它掛了叢集選不出主），第二輪加上。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `elasticsearch/helmchart/values.yaml` | `replicas: 2`、`twoNode.enabled: true` 成預設；新增 `voter`（enabled、heap 256m、requests 640Mi／limits 768Mi、PVC 1Gi） | 測試用 ES 一律兩個資料節點；兩個資料節點只有一個有投票權，它掛了叢集選不出主，加一個不放資料的小節點湊三票 |
| `elasticsearch/helmchart/templates/elasticsearch.yaml` | Service `elasticsearch` 的 selector 加 `role: data`；資料節點 Pod 加 label `role: data`；`podManagementPolicy: Parallel`；`cluster.initial_master_nodes: elasticsearch-0`；新增 StatefulSet `elasticsearch-voter`（`node.roles: master,voting_only`、`http.host: 127.0.0.1`、`NODE_INDEX=2`）；共用片段（叢集 env、readiness、socat、volumes）改成 `define` 給兩個 StatefulSet 用；socat 改成對每個其他節點各一條（3 節點）、port 依 `NODE_INDEX` 而不是只看 hostname；headless Service 多 `es-node-2` | Ingress／Gateway、Collector 經 Service 時只會到資料節點；只投票節點連 9200 都不對外。整個叢集停掉再起時有投票權的可能是 `-1`，照順序起 `-0` 選不出主、永遠不 ready；voter 的 hostname 是 `elasticsearch-voter-0`，`${HOSTNAME##*-}` 算出來是 0 會和 `-0` 撞 port |
| `e2e/k8s/deploy-es.sh` | 沒有 Secret `es-transport-certs` 就自己產憑證（原本在 `run-es-2node.sh`）；單節點升多節點分兩步（先用新設定換 `-0`、再放大）；`--reset-values`；`podManagementPolicy` 不同時 `--cascade=orphan` 刪掉 StatefulSet 重建（這個欄位建好不能改）；Windows 上 `mktemp` 路徑轉成 `C:/`、`MSYS_NO_PATHCONV` 擋 `-subj /CN=` 被當路徑 | 兩節點成預設後，部署腳本要能從零或從單節點裝起來。Helm 4 的 `upgrade` 預設沿用上一次的 `--set`，上次 `--set replicas=1` 的話這次沒給就還是 1（實測卡了三次才發現） |
| `e2e/k8s/load/run-durability.sh` | `es_up`／`es_down`／`ensure_ready` 等兩個 Pod（`ES_REPLICAS`，預設 2），`es_up` 每一步失敗印出來；新情境 `es-one-down`：卡住一個資料節點（`kill -STOP` java）`DOWN_SECONDS` 秒，卡住期間在活著的資料節點裡每 5 秒寫一筆測試文件，記成功／失敗次數 | 量「掛一個資料節點」時 Collector 掉不掉資料、叢集本身能不能寫（第一輪經 port-forward 查叢集狀態，接到卡住的那個 Pod 整段叫不到，什麼都量不到） |
| `e2e/k8s/load/run-load.sh`、`e2e/k8s/run-metrics-scenarios.sh` | ES 起停等兩個 Pod、`ES_REPLICAS` | 寫死 `elasticsearch-0`、`--replicas=1` 的地方 |
| `elasticsearch/README.md` | 測試 ES 改寫成三個 Pod 的拓樸；新增「前面放 Ingress／Gateway 時流量怎麼導」 | 跟著程式 |

## Check：測試的目的與結果

環境：Rancher Desktop 單節點 K8s、ES 8.19.23、Collector 用分支版設定（trace 直寫 ES）。每個情境：產生器 log 200 行／秒、trace 20 個／秒共 360 秒，第 60 秒開始故障。

### 結構

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 三票、只投票節點不放資料 | `_cluster/state` 的 voting config、`_cat/shards`、`_cat/nodes` | 投票名單 `elasticsearch-0`、`-1`、`elasticsearch-voter-0`；voter 身上 0 個 shard（46／46 在兩個資料節點）；叢集 green、3 個節點 |
| 流量不會導到只投票節點 | `kubectl get endpoints elasticsearch`；從資料節點用 voter 的 Pod IP 打 9200 | Endpoints 只有 `elasticsearch-0`、`-1`；打 voter 的 9200 連不上（`http.host: 127.0.0.1`） |

### 掛資料節點（`run-durability.sh es-one-down`，`kill -STOP` java 卡住 180 秒）

| 情境 | log 遺失 | trace 遺失 | 卡住期間在活著的節點直接寫入 | 叢集 |
|---|---|---|---|---|
| 第一輪，沒有只投票節點：卡住 `-1`（沒投票權） | 0／71,990 | 0／7,199 | 沒量（經 port-forward 查，接到卡住的那個 Pod） | 查得到，yellow |
| 第一輪：卡住 `-0`（有投票權；實際卡約 6.6 分鐘，查詢卡住拖長） | 0／71,992 | 0／7,199 | 沒量 | 卡住期間叫不到 |
| 第二輪，有只投票節點：卡住 `-1` | 0／71,996 | 0／7,199 | 成功 22、失敗 7 | yellow、2 個節點 |
| 第二輪：卡住 `-0`（原本有投票權的） | 0／71,986 | 0／7,199 | 成功 15、失敗 10 | yellow、2 個節點（`-1` 與 voter 選出新的主） |

單 Pod 時同樣停 180 秒，trace 遺失 2,552／7,199（35%）。掛一個資料節點後，log、trace、metrics 都沒有遺失、沒有重複，metrics 沒有超過 40 秒的空檔。卡住的最初 35～50 秒，在活著的節點裡直接寫會失敗（每 5 秒一次，失敗 7 與 10 次），之後都成功；失敗期間 Collector 收到逾時或 503 重試，資料留在佇列。

### 兩個資料節點都停（`es-down`，scale 成 0，180 秒）

| log | trace | metrics | 佇列（停機結束時） |
|---|---|---|---|
| 遺失 7,580／71,988（10.5%），時間在 t+0～30.8 秒（5,720）、+40.3 秒（20）、+50.0～59.2 秒（1,840） | 遺失 993／7,199（13.8%），時間在 t+0～10 秒（161）、+50.8～92.4 秒（832） | 6 點（預期約 12），最大間隔 209 秒 | 節點上 1.4 GB；metrics 880、traces 7,843、logs 38,949 個請求，遠低於上限 |

只投票節點在這個情境一直活著，但它不放資料、不處理 HTTP，所以沒有可用的資料節點，Service 沒有後端。恢復後叢集 green、3 個節點。

**沒測到的**：Ingress／Gateway 本身（只確認 Service 的 selector 與 README 的導流說明）；直接刪 Pod；主機整台消失（連線黑洞）；正式環境。

## Act：Check 後沒改善的、還沒解決的

| 沒解決的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 兩個資料節點都停時仍會掉資料（log 10.5%、trace 13.8%） | `es-down` 180 秒 | 只有「沒有任何資料節點」時才發生；掛一個沒事。遺失集中在停機後 0～30 秒與 50～92 秒，其餘時間沒掉，不是整段都掉。原因還沒查：需要 Collector 的 log 依時間與錯誤種類（`dial tcp i/o timeout`、`context deadline`）分類 | **決定：不處理，記為已知風險**（掛任何一個資料節點都安全，全停才會掉；記在 [open-issues 第 1 節](../meta/2026-10-open-issues.md)） |
| 資料節點被卡住的最初 35～50 秒，直接寫入叢集會失敗 | `es-one-down` 卡住期間每 5 秒寫一筆 | Collector 重試，沒掉資料；但經 Ingress／Gateway 進來的非 Collector 流量會看到這段失敗 | 這是 ES 偵測節點失聯的時間（推測，沒驗證）；Gateway 要設逾時與重試 |
| Ingress／Gateway 沒實測 | 只做了 Service 與文件 | 後端只列資料節點的做法沒在真的 Gateway 上跑過 | 有 Gateway 環境時補測 |
| 第一輪 `es-down` 沒出結果表 | 腳本在停機結束的恢復步驟中斷，沒有錯誤訊息 | 改了 `es_up`（每一步失敗印出來、等兩個 Pod）後第二輪正常 | 舊寫法為何中斷沒查 |
| Helm 4 的 `upgrade` 預設沿用上一次的 `--set` | 部署腳本重複升級時 `replicas` 一直是 1 | 其他用 `helm upgrade` 的部署腳本可能也有同樣問題 | 只在 `deploy-es.sh` 加了 `--reset-values`，其他沒查 |
