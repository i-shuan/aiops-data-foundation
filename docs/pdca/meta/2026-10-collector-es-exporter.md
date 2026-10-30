# Collector 出口改用 elasticsearch 寫入元件：ES 停機、沒回應會不會掉資料（2026-10）

**狀態**：完成（k3d 單節點、ES 在主機 docker；三輪實測，設定改了兩次）　　**日期**：2026-10-30　　**相關**：分支 `claude/collector-es-exporter`；前置 [compat-es8-grafana13](2026-10-compat-es8-grafana13.md)、[durability](../phase1-collect/2026-10-durability.md)
**模組**：① 收值（Collector 出口）、③ 存

## Plan：這一次要達到什麼

- **需求**：公司的 ES 是 8.19，沒有 `/_otlp/v1/*` 入口；Collector 三條出口（node、cluster、trace-sampler）都走它，8.19 上一筆都寫不進去。
- **目的**：出口改成 `elasticsearch` 寫入元件（`mapping.mode: otel`），並證明當初不用它的理由已經不成立：ES **停機**（連線被拒）與 **沒回應**（連線掛住到逾時）兩種情況，停機期間的 log、metrics、trace 恢復後會補送，不會掉。
- **範圍**：`telemetry/collectors/common.yaml`、`sampler.yaml`（出口定義）、`node.yaml`、`cluster.yaml`（引用名）。佇列、重試、檔案儲存的設定不動。不改中央 pipeline、不改 engine。
- **為什麼當初不用它**：`common.yaml` 舊註解——「把連線逾時、沒拿到 HTTP 回應當永久錯誤，不重試；實測 ES 停 3 分鐘 metrics 幾乎全掉」。這次用 0.162.0，`retry_on_failure` 開、`max_elapsed_time: 0`，要重測同樣情境。

## Do：改了什麼

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `common.yaml` | `otlp_http/es` → `elasticsearch/es`：`endpoint`、`mapping: {mode: otel}`、`auth` 同一個 `bearertokenauth/es`（scheme ApiKey）；加 `retry: {enabled, max_retries: 3}`（bulk 裡單筆被退的重試）；`sending_queue`、`retry_on_failure` 原樣 | 8.19 沒有 `_otlp` |
| `sampler.yaml` | 同上 | trace-sampler 自己有出口 |
| `node.yaml`、`cluster.yaml` | pipelines 的 `exporters` 改名 | |

## Check：測試的目的與結果

環境：k3d 單節點＋主機 docker 的 ES 8.19.23；Collector 用正式設定（k3d overlay 只改 ES 位址與取樣）；腳本 [e2e/local-k3d/run-es-outage.sh](../../../e2e/local-k3d/run-es-outage.sh)。產生器：log 每秒 200 行帶序號、trace 每秒 20 個帶序號、metrics 是 kubeletstats 每 30 秒一點。

### 三輪的結果（同一套情境，設定改兩次）

| 情境 | 第 1 輪：只換元件（`retry` 預設） | 第 2 輪：`retry` 一直重試 | 第 3 輪：再加 `timeout: 0`、`retry_on_status` 含 401 |
|---|---|---|---|
| 正常 120 秒 | log 0／trace 0／metrics 無空檔 | — | — |
| ES 停機 180 秒（`docker stop`） | **log 掉 41,850（58%）、trace 掉 4,093、metrics 空 240 秒** | log 掉 30,470、trace 掉 2,785、metrics 0 | **log 掉 2,320（3.2%）、trace 掉 201（2.8%）、metrics 0**；掉的全在停機那一刻的前後 10 秒 |
| ES 沒回應 90 秒（`docker pause`） | log 掉 1,508 | **0／0／0** | （第 2 輪已 0，沒重跑） |

重複一律 0；應用端丟 0；恢復後補送都在 1～2 分鐘內收齊。

### 每一輪掉資料的原因（看 Collector 的 log）

| 輪 | Collector 印什麼 | 原因 | 改什麼 |
|---|---|---|---|
| 1 | `Exporting failed. Dropping data`，`connection refused`，**沒有任何重試** | 這個元件沒接 exporterhelper 的 `retry_on_failure`（設了也沒作用）；重試只靠自己的 `retry`，預設 `max_retries: 2`、100ms 起，幾百毫秒就放棄 | `retry: {max_retries: 100000000, initial_interval: 1s, max_interval: 30s}`；單一 Collector 驗：ES 停 45 秒再起，那筆有補到 |
| 2 | `Permanent error: flush failed (401)` 16,640 行、`context deadline exceeded` 1,840 行 | (a) ES 剛起來的一秒多 API key 被拒（[es-startup-401](../phase1-collect/2026-10-es-startup-401.md)），401 被當永久錯誤，**停機期間排隊的整批在 ES 回來那一刻被丟掉**；(b) `timeout: 30s` 是整個請求含重試的總期限，到期那批被丟 | `retry_on_status: [401, 429, 502, 503, 504]`（這些都是整個請求沒寫入，重送不會重複）；`timeout: 0`。單一 Collector 連續送、ES 停 60 秒：改前掉 279（全是 401）、改後 0 |
| 3 | `dial tcp …:9200: i/o timeout`，只在停機的前 10 秒 | `docker stop` 讓容器的 IP 消失，連線變成「送出去沒人理」（SYN 黑洞），這種 dial 逾時元件不重試。ES 在**主機 port** 上停掉（連線被拒）時同樣的測試是 0 遺失 | 沒改設定。正式環境 ES 前面要有 LB／VIP＋健康檢查（[healthcheck-haproxy.cfg](../../../telemetry/storage/healthcheck-haproxy.cfg)），主機掛掉時 LB 回拒絕或 503，不會是黑洞 |

### 中途遇到的測試環境問題（跟 Collector 無關）

- k3d 節點磁碟接近上限，kubelet 每 5 分鐘回收沒在用的 image，把產生器用的 `python:3.12-alpine` 清掉，兩輪測試其實卡在 ImagePullBackOff 沒跑到。改用一個常駐 Pod 佔住那兩個 image。
- 第一輪 `pkill -f` 的樣式比對到自己的指令，把測試連帶殺掉；改用更長的路徑樣式。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 連線黑洞（主機整台不見）那一刻前後約 10 秒的 in-flight 資料會掉 | `docker stop` 情境，第 3 輪 | 每次 ES 主機硬當掉掉幾秒資料（log 3%、trace 3%）；程序掛掉（連線被拒）不會掉 | ES 前面放 LB＋健康檢查就不是黑洞；若要連這段都不掉，要查 go-docappender 對 dial 逾時的判斷 |
| `timeout: 0` 沒有總期限 | 設計 | 一條連線掛死時那個 consumer 卡住到 TCP 自己放棄（分鐘級）；10 個 consumer 全卡就停送，但佇列在磁碟不掉 | 真實環境觀察 `otelcol_exporter_queue_size`；要上限的話改大（例如 10m）而不是 30s |
| `retry_on_status` 含 401 | 設計 | key 真的失效時會一直重試、不丟也不報；靠 [rotate-writer-key.sh](../../../telemetry/scripts/rotate-writer-key.sh) 的流程避免，和佇列監控 | 加一條規則：`otelcol_exporter_queue_size` 持續成長就告警 |
| 只測單節點、3 種故障 | 範圍 | ES 限流（429）、Collector 自己重啟、多節點沒跟新元件一起跑 | 在 k3s 上重跑 [durability](../phase1-collect/2026-10-durability.md) 全部情境 |
| `common.yaml` 舊註解「連線逾時當永久錯誤」 | 第 1 輪重現了它 | 以前的觀察是對的，但原因是預設 `retry` 太短，不是元件不能用 | 已改寫註解 |

- **決定**：出口改用 `elasticsearch` 寫入元件，設定三項一起：`retry` 一直重試、`timeout: 0`、`retry_on_status` 含 401；ES 前面要有 LB＋健康檢查。
- **下一輪**：k3s 全套耐久情境；佇列成長的告警規則。
