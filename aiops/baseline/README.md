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

### MAD 一步一步算

MAD（Median Absolute Deviation，中位數絕對偏差）＝「每個值離中位數多遠」的中位數。例：某支 API「週一 9 點」過去幾週的 5 分鐘請求數，其中 5000 那次是當時出事：

```
1180, 1190, 1200, 1210, 1220, 1230, 1250, 1300, 5000
```

| 步驟 | 算法 | 結果 |
|---|---|---|
| 1. 中位數 | 排好取正中間（第 5 個） | **1220** |
| 2. 每個值離中位數多遠 | \|值 − 1220\| | 40, 30, 20, 10, 0, 10, 30, 80, 3780 |
| 3. MAD | 上面這些距離的中位數（排好：0, 10, 10, 20, **30**, 30, 40, 80, 3780） | **30** |
| 4. 穩健標準差 σ | 1.4826 × MAD ＝ 44.5；但至少是中位數的 5%（`rel_floor`）＝ 61 | **61** |
| 5. 正常範圍 | 中位數 ± z × σ ＝ 1220 ± 5 × 61 | **915～1525** |
| 6. 判斷新值 1900 | z ＝ (1900 − 1220) ÷ 61 ≈ 11.1 ≥ 5，而且差 680 ≥ `min_abs` | **異常** |

- **1.4826**：資料是常態分布時，1.4826 × MAD 剛好等於標準差，所以門檻能用熟悉的「幾倍標準差」來設。
- **`rel_floor`（5%）**：平常幾乎不動的序列 MAD 接近 0，沒有保底的話一點點變化就報。
- **同一組資料用平均和標準差**：平均 1531（被 5000 拉高）、標準差約 1193（被撐大），正常範圍變成約 −4400～7500，1900 被當成正常，之後幾乎什麼都抓不到。

程式在 [aiops_baseline/model.py](../aiops_baseline/model.py) 的 `train`（每個時段算中位數、MAD）和 `score`（算 σ、z、判斷）。跟兄弟比（peer）、trace 每一段的 baseline 用的是同一套。

### 什麼時候開始、多久訓練一次

| | 時間 |
|---|---|
| 每條序列多細 | 每 5 分鐘一個點（metrics 本身 30 秒一點，訓練時聚合成 5 分鐘），每條序列切成 168 個時段（星期幾 × 小時）各自學 |
| 最快多久開始判斷 | 約 4 小時（50 個點，`all` 層）；3 天後有「每天同一小時」；2 週後有「同星期幾 × 同小時」 |
| 訓練（`train`） | 建議每天一次，用最近 4 週重算 |
| 偵測（`detect`） | 建議每 5 分鐘一次，看最近 30 分鐘、只看已經結束的 5 分鐘 |
| 現況 | ⚠️ 正式環境還沒排程，目前是測試環境手動跑（排程範例見「怎麼跑」） |

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

## 自動調整：粒度、低頻事件、借用 baseline

> 狀態：設計定案，實作中（順序 ①→②→③）。

固定「每 5 分鐘一點」對 MES、EAP、設備類不夠：大量 API、設備告警、SECS 事件都是低頻，5 分鐘裡只有幾筆，p95 是雜訊、筆數大多是 0、MAD 變 0。新 API 沒有歷史就完全不判斷。改成**每條序列自己決定**，分三個方向，彼此獨立：

```
原始資料（5 分鐘）
      │
      ▼
① 粒度：看每個時間窗裡「底下的事件」夠不夠多
      ├─ 夠        → 5 分鐘
      ├─ 中        → 15 / 30 分鐘
      ├─ 少        → 1 小時
      └─ 1 小時還不夠 → 筆數類改用卜瓦松；比例、延遲類沒有自己的 baseline
      │
      ▼
② 自己的歷史（季節性，維持現狀）：dow_hour → hour → all
      │ 自己不夠
      ▼
③ 借用：同類 API → 同領域（只限比例、延遲類，門檻調高）
      │ 還不夠
      ▼
   不判斷，只靠規則
```

### ① 每條序列自己選粒度

- **看底下的事件數，不是看有幾個點**：p95 由 3 個請求算出來就是雜訊。每條序列算「每個時間窗的事件數」的中位數，從 5m、15m、30m、1h 裡挑**最細、而且夠多**的：

  | 指標類型 | 事件數是什麼 | 預設門檻（每窗中位數） |
  |---|---|---|
  | 比例（錯誤率）、百分位（p95） | 這段時間的請求數 | ≥ 20 |
  | 筆數（請求數、log 數、事件數） | 筆數本身 | ≥ 10 |
  | 量測值（CPU、記憶體） | 不檢查，固定 5 分鐘 | — |

- 粗的粒度要重新跟 ES 查（百分位不能從 5 分鐘的值合併），只對需要的序列查。
- **粗粒度自然會退到粗的季節性**：1 小時一點時，「同星期幾 × 同小時」4 週只有 4 個值，不夠（每個時段至少要 6 個值），自動退到「每天同一小時」。
- **代價是慢**：1 小時一點，最慢 1 小時後才報。要馬上知道的（設備停機、重大告警）本來就該用規則，有就報。

### ② 很稀少的事件：卜瓦松

1 小時還是大多是 0 的筆數（例如設備告警一小時 0～2 次），中位數和 MAD 都是 0，MAD 方法失效。改用卜瓦松分布：

- 學每個時段的**平均次數 λ**（去掉最高的 2%，避免事故的爆量把平均拉高）。
- 判斷：平常每小時 λ = 0.5 次，這小時出現 6 次，機率 P(X ≥ 6) ≈ 0.00002，小於門檻（預設 0.001）就算異常；也要跟 λ 差至少 `min_abs`。
- 變少也能判斷，但 λ 很小時「0 次」本來就很常見，自然不會報。

### ③ 自己沒有 baseline：借同類的

新 API、量太少的 API，借**同一組**其他成員的 baseline：

- **組要事先定義**（系統不會自己知道誰是「Query API」）：`series.yaml` 的 `groups`，依 keys 的正規表示式比對，由細到粗排，例如先 `mes-query`（服務 `mes-*`、路徑查詢類），再 `mes`（服務 `mes-*`）。之後可改從 K8s label 或服務清單讀。
- **只借比例、延遲類**（序列設 `borrow: true`）：錯誤率在同類 API 間大致可比；p95 勉強可比；**請求數、事件數不借**，新 API 的流量跟別人無關。
- 組的 baseline：該組至少 3 個成員有自己的 baseline，每個時段取成員中位數的中位數、成員 MAD 的中位數。
- 借來的門檻調高（預設 z 從 5 變 7）。

### 異常帶的欄位

每筆異常多記這幾個，讓根因分析和之後的 LLM 知道可信度：

```json
{
  "resolution": "30m",
  "method": "mad",
  "level": "hour",
  "baseline_source": "self",
  "confidence": "medium"
}
```

| 欄位 | 值 |
|---|---|
| `resolution` | 5m / 15m / 30m / 1h |
| `method` | `mad`（中位數＋MAD）／`poisson` |
| `level` | dow_hour / hour / all / group |
| `baseline_source` | `self` 或 `group:<組名>` |
| `confidence` | dow_hour → high；hour → medium；all、借來的 → low |

根因報告遇到 `confidence: low` 或借來的 baseline，要寫出「這是借同類 API 的 baseline，可信度低」。

## 抓不到的

這是方法本身的限制，不是程式的 bug。要靠其他種類的 baseline 補（見 [docs/aiops-rca.md 的「不同用途要的 baseline」](../../docs/aiops-rca.md#不同用途要的-baseline-不一樣)）：

| 抓不到的狀況 | 例子 | 原因 | 要怎麼補 |
|---|---|---|---|
| **慢慢惡化** | 記憶體每天漲一點，三週後才爆 | 每天都在範圍內，baseline 也跟著漲上去 | 「趨勢」baseline：看長期斜率、預測什麼時候會到上限 |
| **指標之間的關係** | 流量正常、CPU 卻很高（程式出問題，不是忙） | 一次只看一個數字，兩個各自都在範圍內 | 「關係」baseline：學平常 CPU 跟流量的比例 |
| **特殊日子** | 過年、年度大保養、盤點 | 過去 4 週沒有這種日子，會誤報 | 行事曆：那段時間不判斷，或用去年同期當 baseline（要先有 13 個月的資料，見 [docs/retention.md](../../docs/retention.md)） |
| **量太少的序列** | 一小時才幾個請求的 API | 跳動相對太大，範圍很寬，小變化抓不到 | 自動放大粒度、卜瓦松（見「自動調整」） |
| **沒見過的東西** | 新出現的錯誤訊息、新出現的呼叫關係 | 這套只看數字大小，不看「有沒有見過」 | 「集合」baseline：log 樣式字典、呼叫關係圖 |
| **新服務、新 API** | 剛上線的服務 | 沒有歷史就沒有 baseline，前幾天不判斷 | 借同類 API 的 baseline（只限比例、延遲類，見「自動調整」） |

## trace 每一段的 baseline

上面是「每個服務一條線」。很多故障這樣看不到（[docs/aiops-cases.md](../../docs/aiops-cases.md)）：N+1、重試風暴、快取穿透時，每一次呼叫都很快，服務的延遲只動一點點，真正變的是**每個請求呼叫某一段幾次**。所以另外學 trace 每一段的「平常」：

| 數字 | 意思 | 抓什麼 |
|---|---|---|
| `trace.segment.calls` | 每個請求呼叫這一段幾次 | N+1（1 次 → 100 次）、重試風暴（1 → 4）、快取穿透（0.05 → 1）；變少也報（功能可能沒跑） |
| `trace.segment.time_ms` | 每個請求在這一段花多久 | 下游拖累（簡訊廠商掛了）、熱點鎖、慢查詢 |
| `trace.segment.error_rate` | 這一段出錯的請求比例 | 某個呼叫開始失敗 |
| `trace.segment.new` | 這支 API 從來沒出現過的呼叫 | 換版後開始直連另一個資料庫、多呼叫一個外部系統（規則：這支 API 有 baseline、這一段沒有，每個請求至少 0.5 次） |
| `trace.segment.swallowed_rate` | 這一段丟了例外、卻沒標成錯誤的請求比例 | 吞掉的例外（catch 掉、回空值或 200）變多 |
| `trace.clock_skew_ms` | 被呼叫方比呼叫方早開始（或晚結束）多少 | 時鐘不同步（規則，不用學：超過 500ms 就記） |
| `trace.segment.repeated` | 同一個請求裡，同一個往外的呼叫平均幾次 | **N+1、迴圈裡打 API**（規則，不用學：≥ 20 次）。從上線第一天就有的 N+1，baseline 會學成平常，所以要有固定門檻 |
| `trace.segment.swallowed` | 同上的吞掉例外比例 | **吞掉的例外**（規則，不用學：≥ 5% 的請求）。訊息帶例外種類，例如「5% 的請求丟了 System.TimeoutException，但沒標成錯誤」 |

**訊息消費者（Kafka）：** 入口除了 HTTP 請求（Server），也算訊息消費者（Consumer）和「沒有上一層的 span」。消費者多一段「`服務 › 排隊（topic）← 生產者`」＝拿到訊息的時間 − 生產者送出的時間（生產者是上一層，或用 link 找回來）。.NET 自動埋點的消費者處理時查的 DB 自成一個 trace，當成自己的入口算，所以看得到「這個消費者寫 DB 變慢、出錯」。實測見 [docs/aiops-trace.md 場景四](../../docs/aiops-trace.md#四非同步訊息佇列)。

吞掉的例外要靠 Collector：ES 不存 span 的例外事件，node Collector 先把例外的種類、訊息抄到 span 的屬性（`exception.type`、`exception.message`，[node.yaml](../../platform/cluster-addons/otel-collector/node.yaml) 的 `transform/exceptions`）。

**尾端取樣之後只用隨機那份：** trace-sampler 把錯的、慢的全留，正常的只留 5%；直接拿留下來的算平均，錯誤率、時間都會偏高。所以抽樣只抽 `tailsampling.policy = random` 的請求（[trace.yaml](trace.yaml) 的 `sample_policy`）；隨機那 5% 錯的慢的也一樣抽，統計跟「全部都記」一樣。

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
| N+1（同上） | ✅ 不用學的規則 `repeated`：每個請求 100 次，門檻 20 | — |
| 吞掉的例外：wip-worker 查 DB 逾時、catch 掉回 200 | ✅ 規則 `swallowed`：「100% 的請求丟了 System.TimeoutException，但沒標成錯誤」；baseline `swallowed_rate` 也報 | — |

（2026-10-11 重跑：trace 已經經過兩層 Collector、尾端取樣，全部通過；trace 收集 `run-trace-e2e.sh`、根因分析 `run-rca-e2e.sh` 也全部通過。）

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

## 跟兄弟比：同一個服務的 Pod 互相比

10 個 Pod 只有 1 個壞掉（錯誤率 100%、或只有它記憶體一直漲），整個服務平均起來只差一點，跟平常比（baseline）可能還在範圍內。同一個服務的 Pod 跑同一份程式、分到差不多的流量，應該長得差不多，所以**拿其他 Pod 當標準**：不用歷史，裝上去第一天就有用。設定在 [peer.yaml](peer.yaml)，程式在 [aiops_baseline/peer.py](../aiops_baseline/peer.py)。

| 檢查 | 每個 Pod 算什麼（最近 10 分鐘） | 分組 | 觸發（跟其他 Pod 的中位數比） |
|---|---|---|---|
| `peer.http_error_rate` | 錯誤率（5xx ÷ 全部） | namespace × 服務 | 高 5 倍穩健標準差，而且多 5 個百分點以上 |
| `peer.http_latency_p95_s` | p95 延遲 | namespace × 服務 | 同上，而且慢 100ms 以上、是別人的 2 倍以上 |
| `peer.pod_cpu` | CPU 平均 | namespace × Deployment | 同上，而且多 0.2 核以上、是別人的 3 倍以上 |
| `peer.pod_memory_bytes` | 記憶體（working set）最高 | namespace × Deployment | 同上，而且多 100MB 以上、是別人的 1.5 倍以上 |

怎麼比：每個 Pod 跟「**其他** Pod」的中位數、MAD 比（跟 baseline 一樣的穩健統計；比的時候不含自己，壞掉的那個不會把標準拉過去）。另外：
- **至少 3 個 Pod 才比**：只有 2 個分不出誰才是怪的。
- **只比現在還在的 Pod**（最近 2 分鐘有資料）：換版、縮容刪掉的不算兄弟。換版途中新舊 Pod 會一起比，新版的 Pod 都比舊的差也會報，這正好是想知道的。
- **流量太少的 Pod 不比錯誤率、延遲**（10 分鐘不到 20 個請求）：一兩個錯就跳很高。
- **全部一起壞不報**：例如下游 DB 掛了，每個 Pod 都錯，那不是「某個 Pod」的問題，交給 baseline 和規則。
- 門檻比 baseline 寬（`rel_floor` 10%，再加上至少差多少、至少幾倍）：同一個服務的 Pod 本來就不會一模一樣。

觸發的寫進 `aiops-anomalies`：`series` 是檢查名稱（`peer.…`）、`level` 是 `peer`、`median` 是其他 Pod 的中位數，`message` 例如「這個 Pod 的錯誤率比同服務其他 Pod 高：metrics-cases、dotnet-app、dotnet-app-…（100%，其他 2 個 Pod 中位數 0%，z=…）」。

```bash
python3 -m aiops_baseline peer --config baseline/peer.yaml      # 每分鐘
```

**實測**（[test/local-k3s/run-peer-e2e.sh](../../test/local-k3s/run-peer-e2e.sh)，2026-10-10，全部通過）：真的 .NET 應用（自動埋點）開 3 個副本，每次只弄壞一個。

| 案例 | 怎麼弄壞 | 抓到的 | 其他 2 個 Pod |
|---|---|---|---|
| 正常 | — | 不報 | 不報 |
| 連線池漏光 | 一個 Pod 拿了連線不還，之後的請求等 2 秒後 500 | 4 分鐘：錯誤率 **13%**（其他 0%）、p95 **1,750ms**（其他 2ms） | 不報 |
| 記憶體洩漏 | 一個 Pod 每個請求 32KB 放進不會清的 list | 第 5 分鐘：**294MB**（其他 163MB） | 不報 |
| 無窮迴圈 | 一個 Pod 裡有程式佔滿 1 核 | 第 3 分鐘：CPU **0.65 核**（其他 0.21 核，10 分鐘平均） | 不報 |

同一段時間整個服務的錯誤率是 **9%**（3 個 Pod 裡 1 個壞）。這次服務平常是 0%，跟平常比（baseline）也會報，但只知道「服務錯誤率高」、不知道是哪個 Pod；Pod 越多稀釋越多：10 個 Pod 壞 1 個大約剩 1%，就低於 baseline 的門檻（多 5 個百分點）了。

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
5. **不是 Java、.NET 的服務**：流量、錯誤率、延遲來自 `http.server.request.duration`（Java agent、.NET 自動埋點都有，見 [docs/dotnet.md](../../docs/dotnet.md)）。其他語言要用 OpenTelemetry 開出同名的直方圖，才學得到。
6. **更準的方法**：用一陣子、收集到誤報和漏報後，再考慮方法 B（拆週期）、C（預測模型）。
