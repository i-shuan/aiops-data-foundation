# AIOps baseline：學「平常的樣子」、抓異常

一句話：**每條時間序列（服務 × API × 指標），取過去 4 週同一個星期幾、同一個小時的中位數與波動範圍當「平常」；之後的值超出範圍就記成異常。**

這是 [docs/aiops-rca.md](../../docs/aiops-rca.md) 的「方法 A」，屬於四種 baseline 裡的「數字 × 時段」（其他三種：集合、關係、趨勢與標註，見[不同用途要的 baseline](../../docs/aiops-rca.md#不同用途要的-baseline-不一樣)）：不需要 GPU、不需要付費授權，結果好解釋（「比過去四週同時段高 5 倍」）。一台 16GB 記憶體的機器就跑得動。

## 做什麼

```
ES（log、metrics、trace）
   ↓ train：讀過去 4 週，每 5 分鐘一點
   ↓        每條序列 × 每個時段（星期幾 × 幾點）算中位數、波動（MAD）
   → aiops-baseline（每條序列 168 個時段）
   ↓ detect：讀最近 30 分鐘，跟同時段的 baseline 比
   → aiops-anomalies（超出範圍的點）
```

學哪些序列寫在 [series.yaml](series.yaml)，目前是每個服務的核心指標：

| 序列 | 來源 | 依什麼切 | 什麼算異常 |
|---|---|---|---|
| `http.requests` | 應用的 metrics | 服務 × method × API | 請求數變多或變少 |
| `http.error_rate` | 應用的 metrics | 服務 × method × API | 5xx 的比例變高（至少多 5 個百分點） |
| `http.latency_p95_s` | 應用的 metrics | 服務 × method × API | 延遲 p95 變高（至少慢 50 毫秒） |
| `log.lines` | log | 服務 | log 筆數變多或變少 |
| `log.errors` | log | 服務 | error／fatal 的 log 變多 |
| `pod.cpu`、`pod.memory_bytes` | K8s metrics | namespace × Deployment | 用量變高 |

**流量、錯誤率、延遲從 metrics 算，不從 trace、log 算**（分工見 [docs/aiops-rca.md「分工」](../../docs/aiops-rca.md#分工metrics-決定什麼時候告警trace-和-log-說明原因)）：

- 來源是 Java agent 的 `http.server.request.duration` 直方圖（存進 ES 的欄位是 `metrics.http_server_request_duration_seconds`）。每個請求都算到；Collector 把 Prometheus 的累加值換成「每 30 秒多少」，所以任一段時間都能直接加總。
- trace 有取樣（調到一成就只看到一成的請求），而且只留 7 天，不適合學 4 週的 baseline。
- log 只學筆數（`log.lines`、`log.errors`）：抓得到 metrics 看不到的問題，例如背景工作一直失敗、但 API 都正常。

要加序列，在 `series.yaml` 加一段就好，不用改程式。

## 怎麼判斷

- **平常的樣子**：同一個星期幾、同一個小時（台北時間）過去幾週的值，取**中位數**和**中位數絕對偏差**（MAD）。用中位數不用平均，歷史裡混了幾次事故也不太會被拉偏。
- **異常**：離中位數超過 **5 倍穩健標準差**（1.4826 × MAD），而且差距超過 `min_abs`（避免量很小的序列一點點變化就告警）。延遲、錯誤只看變高；流量兩邊都看。
- **歷史不夠時往下退**，`level` 欄位記錄用了哪一層：

  | level | 用什麼當「平常」 | 條件 |
  |---|---|---|
  | `dow_hour` | 同星期幾 × 同小時 | 至少 2 個不同的日子（約 2 週） |
  | `hour` | 每天同一小時 | 至少 3 天 |
  | `all` | 不分時段 | 至少 50 個點 |
  | （沒有） | 不判斷 | 都不夠 |

- **沒資料的時段**：筆數類的序列補 0（沒有請求就是 0），但兩種情況當成「不知道」：服務第一次出現之前，以及整個 index 都沒有資料的時段（收集系統本身停了）。偵測時，有 baseline 但整段都沒資料的服務補 0，這樣「服務整個停掉」也抓得到。

## 學出來長什麼樣子

`synth_e2e` 學出來的 mes-api 每 5 分鐘請求數（從 `aiops-baseline` 直接查出來的）：

| 時段 | 週一：平常 | 週一：正常範圍 | 週六：平常 | 週六：正常範圍 |
|---|---|---|---|---|
| 凌晨 2 點 | 20 | 12.5～27 | 10 | 2.5～17 |
| 早上 8 點 | 98 | 65～131 | 50 | 28～72 |
| 上午 10 點 | 99.5 | 51～148 | 51 | 36～66 |
| 晚上 7 點 | 97 | 52～141 | 50.5 | 24～76 |
| 晚上 8 點 | 20 | 12.5～27 | 10 | 2.5～17 |

- **週期是自己學出來的**：沒有告訴程式「8 點上班、20 點下班、週末減半」。把每個時段的範圍連起來，就是一條隨時間起伏的帶子，落在帶子外面就是異常。
- **範圍的寬窄每個時段自己決定**：白天平常就晃比較大（MAD 6.5），範圍寬；半夜很穩（MAD 1），範圍窄。所以白天晃 30% 不告警、半夜晃 30% 會告警。

### 為什麼用中位數和 MAD，不用平均和標準差

假設「週一 10 點」的 48 個值裡，有一天出事衝到 500：

| | 沒有事故 | 混了一次事故 |
|---|---|---|
| 平均 | 約 100 | 約 108（被拉高） |
| 標準差 | 約 8 | 約 58（正常範圍變成 0～400，之後什麼都抓不到） |
| **中位數** | 99.5 | 約 100（幾乎不動） |
| **MAD** | 6.5 | 約 6.5（幾乎不動） |

歷史裡一定會混到事故、保養、測試流量，中位數和 MAD 不容易被帶偏。

### 兩個條件都成立才算異常

1. 離中位數超過 `z` 倍（預設 5）的穩健標準差（1.4826 × MAD）。
2. 差距超過 `min_abs`。量很小的序列需要這條：例如凌晨的錯誤數平常是 1、MAD 是 0，只看倍數的話，1 變 2 就告警；`min_abs: 5` 表示至少多 5 筆才算。

### 調靈敏度

| 調整 | 效果 | 代價 |
|---|---|---|
| `z` 調小（例如 3） | 小一點的變化也抓得到 | 誤報變多 |
| `z` 調大（例如 8） | 只抓很明顯的 | 可能漏掉 |
| `min_abs` 調大 | 小服務、小數字的告警變少 | 小服務真的出事可能漏掉 |

`detect` 的 `z` 是全部共用；`min_abs`、`direction` 每條序列各自設。先用預設跑兩、三週，依誤報和漏報再調。要知道漏了哪些，就需要事故紀錄。

### baseline 會跟著時間更新

每天重新 `train`，用的永遠是最近 4 週：

- **慢慢成長**（例如新產線上線、流量逐月增加）：baseline 跟著慢慢調高，不會一直告警。
- **突然永久改變**（例如換版後每個請求都變慢）：前一、兩週會持續告警，直到新的狀況佔了歷史的一半以上，中位數才跟上。換版後要不要重新學，規則還沒定（見「還沒做」）。

## 抓不到的

這是方法本身的限制，不是程式的 bug。要靠其他種類的 baseline 補（見 [docs/aiops-rca.md 的「不同用途要的 baseline」](../../docs/aiops-rca.md#不同用途要的-baseline-不一樣)）：

| 抓不到的狀況 | 例子 | 原因 | 要怎麼補 |
|---|---|---|---|
| **慢慢惡化** | 記憶體每天漲一點，三週後才爆 | 每天都在範圍內，baseline 也跟著漲上去 | 「趨勢」baseline：看長期斜率、預測什麼時候會到上限 |
| **指標之間的關係** | 流量正常、CPU 卻很高（程式出問題，不是忙） | 一次只看一個數字，兩個各自都在範圍內 | 「關係」baseline：學平常 CPU 跟流量的比例 |
| **特殊日子** | 過年、年度大保養、盤點 | 過去 4 週沒有這種日子，會誤報 | 行事曆：那段時間不判斷，或用去年同期當 baseline（要先有 13 個月的資料，見 [docs/retention.md](../../docs/retention.md)） |
| **量太少的序列** | 一小時才幾個請求的 API | 跳動相對太大，範圍很寬，小變化抓不到 | 用比較長的時段（例如 1 小時一點）、或合併成服務層級再學 |
| **沒見過的東西** | 新出現的錯誤訊息、新出現的呼叫關係 | 這套只看數字大小，不看「有沒有見過」 | 「集合」baseline：log 樣式字典、呼叫關係圖 |
| **新服務、新 API** | 剛上線的服務 | 沒有歷史就沒有 baseline，前幾天不判斷 | 先用同類服務的範圍，或接受前兩週只記錄不告警 |

## trace 每一段的 baseline

上面是「每個服務一條線」。很多故障這樣看不到（[docs/aiops-cases.md](../../docs/aiops-cases.md)）：N+1、重試風暴、快取穿透時，每一次呼叫都很快，服務的延遲只動一點點，真正變的是**每個請求呼叫某一段幾次**。所以另外學 trace 每一段的「平常」：

| 數字 | 意思 | 抓什麼 |
|---|---|---|
| `trace.segment.calls` | 每個請求呼叫這一段幾次 | N+1（1 次 → 100 次）、重試風暴（1 → 4）、快取穿透（0.05 → 1）；變少也報（功能可能沒跑） |
| `trace.segment.time_ms` | 每個請求在這一段花多久 | 下游拖累（簡訊廠商掛了）、熱點鎖、慢查詢 |
| `trace.segment.error_rate` | 這一段出錯的請求比例 | 某個呼叫開始失敗 |
| `trace.segment.new` | 這支 API 從來沒出現過的呼叫 | 換版後開始直連另一個資料庫、多呼叫一個外部系統（規則：這支 API 有 baseline、這一段沒有，每個請求至少 0.5 次） |
| `trace.clock_skew_ms` | 被呼叫方比呼叫方早開始（或晚結束）多少 | 時鐘不同步（規則，不用學：超過 500ms 就記） |

**「段」是什麼：** 以每個 API（服務收到的請求）為單位，只算這個服務自己的部分：
- 自己的程式：扣掉往外呼叫的時間。
- 往外的每一個呼叫：查 DB、呼叫別的服務、外部廠商，等多久就算多久。

下游服務裡面發生的事，由下游自己那支 API 的 baseline 管。所以 N+1 會出在 wip-worker 那支 API，不會連 mes-api 也一起報。

```
mes-api › GET /lots/{id}（每個請求）
  ├─ mes-api › GET /lots/{id}            自己的程式        平常 3 ms
  ├─ mes-api › SELECT LOT → oracle       快取沒命中才查    平常 0.05 次、5 ms
  ├─ mes-api › GET → rca-wip-worker      呼叫 wip-worker   平常 1 次、50 ms
  └─ mes-api › POST → sms-gateway        通知簡訊          平常 1 次、20 ms
```

**怎麼算：**

```
trace-detect（每 5 分鐘）
   ↓ 每個 API 抽 20 個請求（依 trace_id 取前 20 個，trace_id 是隨機的，等於隨機抽）
   ↓ 每個請求、每一段：呼叫幾次、花多久、有沒有出錯 → 平均
   → aiops-trace-segments（統計值，很小，可以留很久；trace 原文只留 7 天）
   ↓ 跟 baseline 比 → aiops-anomalies
trace-train（每天）
   ↓ 讀 aiops-trace-segments 過去 4 週 → 每個「API × 段」× 時段（星期幾 × 小時）的中位數、MAD
   → aiops-baseline
```

學和判斷的方法跟上面一樣（中位數＋MAD、z 5 倍、`min_abs`），設定在 [trace.yaml](trace.yaml)。

```bash
python3 -m aiops_baseline trace-collect --config baseline/trace.yaml   # 第一次：補過去 7 天的統計（trace 只留 7 天）
python3 -m aiops_baseline trace-train   --config baseline/trace.yaml   # 每天
python3 -m aiops_baseline trace-detect  --config baseline/trace.yaml   # 每 5 分鐘（同時累積統計）
```

剛上線時只有 7 天的統計，學出來是 `hour` 或 `all` 層級；累積到 2 週以上才有「同星期幾 × 同小時」。

**實測**（`test/local-k3s/run-trace-baseline-e2e.sh`，兩個模擬服務走真正的路線：Collector → ES；測試用 1 分鐘一點、學 20 分鐘）：

| 情境 | 抓到的（預期那一段） | 同時也報的 |
|---|---|---|
| 正常 3 分鐘 | 沒有異常 ✅ | — |
| N+1：wip-worker 每個請求查 100 次 | ✅ `calls`：wip-worker › SELECT LOT_HISTORY → oracle（z 990） | 同一段的時間（200ms，平常 33ms）；wip-worker 自己的時間；mes-api 等 wip-worker 的時間（正確：上游看到的是變慢） |
| 重試風暴：mes-api 呼叫 wip-worker 4 次 | ✅ `calls`：mes-api › GET → rca-wip-worker（z 30） | 同一段的時間（4 次加起來 261ms，平常 66ms）；mes-api 自己的時間一次（模擬程式多跑了迴圈） |
| 快取穿透：每個請求都查 DB（平常 5%） | ✅ `calls`：mes-api › SELECT LOT → oracle（z 12） | — |
| 簡訊廠商掛了：20ms → 3 秒 | ✅ `time_ms`：mes-api › POST → sms-gateway（z 1490） | — |

單元測試（`tests/test_trace.py`）在記憶體裡模擬 4 週、每小時 20 個請求，同樣 4 個案例都抓到、正常 5 次 0 誤報。

## 規則：狀態變化、離極限多遠

有些問題不用學「平常」：發生了就是問題（容器重啟、副本不夠），或有明確的上限（記憶體、磁碟）。這些用規則判斷，**不用歷史，裝上去第一天就有用**。規則在 [rules.yaml](rules.yaml)：

| 規則 | 看什麼 | 觸發 | 嚴重度 |
|---|---|---|---|
| `container.restarted` | 容器重啟次數 | 最近 10 分鐘增加 ≥ 1 | critical |
| `deployment.unavailable` | 要的副本 − 可用副本 | ≥ 1，3 分鐘裡至少 80% 的時間（換版滾動的短暫不足不算；一直重啟、每次短暫起來的也抓得到） | critical |
| `statefulset.unavailable`、`daemonset.unavailable` | 同上 | 同上 | critical／warning |
| `pod.failed` | Pod 狀態 | Failed | critical |
| `pod.pending` | Pod 狀態 | Pending 連續 5 分鐘（拉不到 image、資源不夠排不上去） | warning |
| `job.failed` | Job 失敗的 Pod 數 | 增加 ≥ 1 | warning |
| `node.not_ready`、`node.memory_pressure`、`node.disk_pressure` | 節點狀態 | 成立 | critical |
| `scrape.down` | 應用的 metrics 抓不抓得到（`up`） | 0，連續 1 分鐘 | warning |
| `container.memory_near_limit` | 記憶體 ÷ 上限 | ≥ 90%，連續 2 分鐘（再上去會被 OOM kill） | warning |
| `node.memory_high` | 節點記憶體 ÷ 總量 | ≥ 90%，連續 5 分鐘 | warning |
| `node.disk_high`、`node.disk_full` | 節點磁碟用量 ÷ 容量 | ≥ 85%（5 分鐘）／≥ 95%（1 分鐘） | warning／critical |
| `container.cpu_throttled` | 被限流的 CPU 週期 ÷ 全部週期（cAdvisor） | ≥ 25%，5 分鐘 | warning |
| `jvm.old_gen_after_gc_high` | 老年代 GC 後用量 ÷ 上限 | ≥ 80%，2 分鐘 | warning |
| `jvm.major_gc_frequent` | major GC 次數 | 5 分鐘內 ≥ 5 次 | warning |
| `jvm.gc_overhead` | GC 花的時間 ÷ 經過的時間 | ≥ 10%，3 分鐘（heap 大的服務） | warning |
| `db.pool_exhausted`、`db.pool_waiting` | 連線池使用中 ÷ 上限；排隊中的請求 | ≥ 90%，1 分鐘；≥ 1，1 分鐘 | critical／warning |
| `kafka.consumer_lag`、`kafka.consumer_lag_growing` | 消費落後筆數；5 分鐘增加多少 | ≥ 10,000，3 分鐘；≥ 5,000 | warning |
| `eap.secs_disconnected`、`eap.plc_scan_slow`、`eap.secs_retransmits` | 機台 SECS 通訊、PLC 掃描週期、重傳（依 `equipment_id`） | 斷線 1 分鐘；≥ 規格 50ms，2 分鐘；5 分鐘重傳 ≥ 10 次 | critical／warning |

四個領域的案例與實測見 [docs/aiops-metrics-cases.md](../../docs/aiops-metrics-cases.md)。

寫法：`value`（欄位跟門檻比）、`compare`（兩個欄位相減）、`ratio`（欄位 ÷ 分母）、`increase`（累計值增加多少，歸零重來也算得對）、`increase_ratio`（兩個累計值增加量的比例）、`rate`（加總 ÷ 秒數）、`total`（加總；`hist_count` 數直方圖的次數）。分母在另一種文件時用 `join` 補進來。`for` 是「這段時間都成立才算」，`min_ratio` 放寬成「這段時間裡至少多少比例成立」。對象不在了（Pod 刪掉、節點拿掉，最新的資料超過 2 分鐘）就不報；只有容器重啟、Job 失敗設了 `report_gone: true`，刪掉了也照報。要加規則，在 rules.yaml 加一段，不用改程式。

觸發的寫進 `aiops-anomalies`：`series` 是 `rule.<名稱>`、`level` 是 `rule`，另外有 `severity`、`message`（例如「容器重啟：rules-test、crash-app-…、app（1，門檻 >= 1）」）、`since`（從什麼時候開始成立）。同一條規則、同一個對象，1 分鐘只記一筆。

```bash
python3 -m aiops_baseline rules --config baseline/rules.yaml      # 每分鐘
```

**還沒加：** 趨勢（例如老年代照這個速度幾小時後會滿）、網卡丟包（要 node_exporter）、資料庫自己的指標（長交易、慢查詢，要 DB 的 exporter）。

## 怎麼跑

```bash
cd aiops                            # baseline、根因分析是同一個 Python 專案
pip install -r requirements.txt     # pandas、numpy、pyyaml、requests

export ES_URL=https://<es>:9200
export ES_API_KEY=<elasticsearch-setup.sh 發的「AIOps baseline key」>   # 只能讀資料、寫 aiops-*
# export ES_CA=/path/to/ca.crt      # ES 用內部 CA 的話

python3 -m aiops_baseline train  --config baseline/series.yaml   # 每天跑一次（例如凌晨）
python3 -m aiops_baseline detect --config baseline/series.yaml   # 每 5 分鐘跑一次，看最近 30 分鐘
```

排程例子（crontab）：

```
15 3 * * *  cd /opt/aiops && python3 -m aiops_baseline train  --config baseline/series.yaml >> train.log 2>&1
*/5 * * * * cd /opt/aiops && python3 -m aiops_baseline detect --config baseline/series.yaml >> detect.log 2>&1
```

回測（拿過去某個時間點當「現在」）：`train --end 2026-10-01T00:00:00Z`、`detect --at 2026-10-01T10:00:00Z --window 1D`。

detect 只看**已經結束**的 5 分鐘：還沒收齊的那一段筆數會偏低，看起來像驟降。重跑同一段時間不會重複寫（異常的 `_id` 由序列、維度、時間決定）。

## 結果在哪裡

| index | 內容 |
|---|---|
| `aiops-baseline` | `series`、`keys`（例如 `keys.service.name`）、`dow`（0 = 週一）、`hour`、`level`、`median`、`mad`、`p05`、`p95`、`n`、`trained_at` |
| `aiops-anomalies` | `@timestamp`、`series`、`keys`、`value`、`median`、`expected_low`／`expected_high`（正常範圍）、`z`（偏離幾倍）、`level`、`direction` |

在 Kibana 建一個 `aiops-anomalies` 的 data view，就能依 `series`、`keys` 看哪個服務、哪個時間點異常。

## 驗證

```bash
python3 -m unittest discover -s tests -t .              # 在 aiops 底下；單元測試，不需要 ES
ES_URL=... ES_AUTH=... python3 -m tests.synth_e2e      # 模擬資料，需要 ES
```

`synth_e2e`：寫 4 週＋1 天的模擬資料進 ES（3 個服務，有每日、每週的週期和雜訊），最後一天插入 4 個異常，只用前 4 週訓練、偵測最後一天：

| 插入的異常 | 結果 |
|---|---|
| mes-api 10:00 流量暴增 3 倍 | ✅ 抓到（z = 20.5） |
| wip-worker 14:30 延遲變 4 倍 | ✅ 抓到（z = 43.8） |
| eap-gw 凌晨 2:00 出現白天的流量 | ✅ 抓到（z = 53.7）：同樣的值在白天是正常的 |
| mes-api 16:00 錯誤數暴增 | ✅ 抓到（z = 28.3） |
| 正常的點 | **誤報 0 個／2,592 個** |

寫入 2.5 萬筆＋訓練＋偵測共 15 秒。

測試環境的真實資料只有幾個小時，筆數類的序列都還「資料不夠，先不判斷」，CPU、記憶體用 `all` 這一層。這是預期的：歷史不夠時寧可不報。

## 還沒做

1. **告警**：現在只寫進 `aiops-anomalies`。接 Kibana 的告警規則、或推到通訊軟體，要再決定。
2. **合併**：同一個服務的流量、錯誤、延遲同時異常時，現在是好幾筆，之後依服務與時間合併成一件事（[docs/aiops-rca.md](../../docs/aiops-rca.md) 的步驟 2）。
3. **排除已知事件**：保養、事故期間的資料目前會一起學進去（中位數不太受影響，但最好排除）。
4. **換版**：新版本上線後要不要重新學，規則還沒定。
5. **不是 Java 的服務**：流量、錯誤率、延遲來自 Java agent 的 `http.server.request.duration`。其他語言要用 OpenTelemetry SDK 開出同名的直方圖（Prometheus 格式），才學得到。
6. **更準的方法**：用一陣子、收集到誤報和漏報後，再考慮方法 B（拆週期）、C（預測模型）。
