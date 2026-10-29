# baseline 原理與設計：學「平常的樣子」、抓異常

> **類型：原理 Explanation**　同主題：[規格](../../engine/configs/baseline/reference.md)・[操作](../../engine/configs/baseline/README.md)・[實測](../../engine/configs/baseline/verification.md)

一句話：**每條時間序列（服務 × API × 指標），取過去 4 週同一個星期幾、同一個小時的中位數與波動範圍當「平常」；之後的值超出範圍就記成異常。**

這是 [docs/rca/README.md](README.md) 的「方法 A」，屬於四種 baseline 裡的「數字 × 時段」（其他三種：集合、關係、趨勢與標註，見[不同用途要的 baseline](README.md#不同用途要的-baseline-不一樣)）：不需要 GPU、不需要付費授權，結果好解釋（「比過去四週同時段高 5 倍」）。一台 16GB 記憶體的機器就跑得動。

這份講方法為什麼這樣設計、各種偵測的道理、方法本身抓不到什麼；怎麼跑、參數、欄位、實測結果在 `engine/configs/baseline/`：[規格](../../engine/configs/baseline/reference.md)・[操作](../../engine/configs/baseline/README.md)・[實測](../../engine/configs/baseline/verification.md)。

**目錄**

<!-- toc -->
- [做什麼](#做什麼)
- [怎麼判斷](#怎麼判斷)
- [學出來長什麼樣子](#學出來長什麼樣子)
  - [為什麼用中位數和 MAD，不用平均和標準差](#為什麼用中位數和-mad不用平均和標準差)
  - [MAD 一步一步算](#mad-一步一步算)
  - [兩個條件都成立才算異常](#兩個條件都成立才算異常)
  - [跳一次是 warning，連續才是 anomaly](#跳一次是-warning連續才是-anomaly)
  - [baseline 會跟著時間更新](#baseline-會跟著時間更新)
  - [定義的原則與限制](#定義的原則與限制)
- [自動調整：粒度、低頻事件、借用 baseline](#自動調整粒度低頻事件借用-baseline)
  - [(2) 很稀少的事件：卜瓦松](#2-很稀少的事件卜瓦松)
  - [(3) 自己沒有 baseline：借同類的](#3-自己沒有-baseline借同類的)
- [抓不到的](#抓不到的)
- [trace 每一段的 baseline](#trace-每一段的-baseline)
- [規則：狀態變化、離極限多遠](#規則狀態變化離極限多遠)
- [Pod 同儕橫向比對：同一個服務的 Pod 互相比](#pod-同儕橫向比對同一個服務的-pod-互相比)
- [餵給 baseline 之前：擋了什麼、還缺什麼](#餵給-baseline-之前擋了什麼還缺什麼)
- [調整：哪些資料不學、從哪裡重新學、哪些時段放寬](#調整哪些資料不學從哪裡重新學哪些時段放寬)
- [事故紀錄與值班回饋](#事故紀錄與值班回饋)
- [慢性病清單、行事曆](#慢性病清單行事曆)
- [資料品質檢查：資料本身怪不怪](#資料品質檢查資料本身怪不怪)
- [還沒做](#還沒做)
<!-- /toc -->

## 做什麼

> 另見：[規格](../../engine/configs/baseline/reference.md#做什麼)・[操作](../../engine/configs/baseline/reference.md#做什麼)

```
ES（log、metrics、trace）
   ↓ train：每天一次。預設 30 秒一點，量不夠的序列自己往粗調（1 分鐘～1 小時）
   ↓        每條序列 × 每個時段（星期幾 × 幾點）算中位數、波動（MAD）
   → aiops-baseline（每條序列 168 個時段）
   ↓ detect：每 30 秒一次，讀最近 30 分鐘，跟同時段的 baseline 比
   → aiops-anomalies（跳一次 warning、連續兩個時間窗 anomaly）
```

**流量、錯誤率、延遲從 metrics 算，不從 trace、log 算**（分工見 [docs/rca/README.md「分工」](README.md#分工metrics-決定什麼時候告警trace-和-log-說明原因)）：

- 來源是 `http.server.request.duration` 直方圖（ES 欄位 `metrics.http.server.request.duration`；Java agent 經 Prometheus 進來的名字由 node Collector 改成這個，跟 .NET 一樣）。每個請求都算到；Collector 把 Prometheus 的累加值換成「每 30 秒多少」，所以任一段時間都能直接加總。
- trace 有取樣（調到一成就只看到一成的請求），而且只留 7 天，不適合學 4 週的 baseline。
- log 只學筆數（`log.lines`、`log.errors`）：抓得到 metrics 看不到的問題，例如背景工作一直失敗、但 API 都正常。

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

- **沒資料的時段**：筆數類的序列補 0（沒有請求就是 0），但兩種情況當成「不知道」：服務第一次出現之前，以及整個 index（不加序列的 filter）都沒有資料的時段（收集系統本身停了）。偵測時，有 baseline 但整段都沒資料的服務補 0，這樣「服務整個停掉」也抓得到；只補收集系統有在送資料的時間窗，收集中斷超過偵測的 30 分鐘也不會把每個服務報成停掉。

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

程式在 [detector/model.py](../../engine/detector/model.py) 的 `train`（每個時段算中位數、MAD）和 `score`（算 σ、z、判斷）。Pod 同儕橫向比對（peer）、trace 每一段的 baseline 用的是同一套。

### 兩個條件都成立才算異常

1. 離中位數超過 `z` 倍（預設 5）的穩健標準差（1.4826 × MAD）。
2. 差距超過 `min_abs`。量很小的序列需要這條：例如凌晨的錯誤數平常是 1、MAD 是 0，只看倍數的話，1 變 2 就告警；`min_abs: 5` 表示至少多 5 筆才算。

### 跳一次是 warning，連續才是 anomaly

不要跳一點就算異常。超標的點分兩級（`status` 欄位）：

**時間上的效果**（預設 30 秒一點，見「自動調整 (1)」）：30 秒一點的序列，偵測每 30 秒跑一次，**跳一次約 30 秒後 warning、連續兩個 30 秒（約 1 分鐘）就是 anomaly**；粒度被調粗的序列（量少、或使用者、feedback 調過的）照它自己的時間窗算。

### baseline 會跟著時間更新

每天重新 `train`，用的永遠是最近 4 週：

- **慢慢成長**（例如新產線上線、流量逐月增加）：baseline 跟著慢慢調高，不會一直告警。
- **突然永久改變**（例如換版後每個請求都變慢）：沒處理的話，前一、兩週會持續告警，直到新的狀況佔了歷史的一半以上，中位數才跟上。用 `reset_point` 從換版那刻重新學（見「調整」）。

### 定義的原則與限制

**原則**：統計公式（168 格中位數＋MAD）維持簡單，只抓一般異常。學錯、報錯的情況不改公式，用「調整資料」解：哪些資料不學、從哪裡重新學、哪些時段放寬（見「調整」）。調整由人寫，或由 learning loop（值班回饋、CI/CD）提出、人核准，都寫進同一個 `aiops-tuning`。**不做**：在公式裡加月份、季度的時段；自己訓練模型（[feedback/llm-mlops.md](../feedback/llm-mlops.md)）。

## 自動調整：粒度、低頻事件、借用 baseline

> 另見：[規格](../../engine/configs/baseline/reference.md#自動調整粒度低頻事件借用-baseline)

所有序列用同一個粒度不合適：很多 API、背景工作、外部呼叫都是低頻，一個時間窗只有幾筆，p95 是雜訊、筆數大多是 0、MAD 變 0；新 API 沒有歷史就完全不判斷。（工廠的設備告警、SECS 事件也一樣；工廠不在目前範圍。）改成**每條序列自己決定**，分三個方向，彼此獨立：

```
原始資料（30 秒）
      │
      ▼
(1) 粒度：看每個時間窗裡「底下的事件」夠不夠多
      ├─ 夠        → 30 秒 / 1 分鐘 / 5 分鐘
      ├─ 中        → 15 / 30 分鐘
      ├─ 少        → 1 小時
      └─ 1 小時還不夠 → 筆數類改用卜瓦松；比例、延遲類沒有自己的 baseline
      │
      ▼
(2) 自己的歷史（季節性，維持現狀）：dow_hour → hour → all
      │ 自己不夠
      ▼
(3) 借用：同類 API → 同領域（只限比例、延遲類，門檻調高）
      │ 還不夠
      ▼
   不判斷，只靠規則
```

### (2) 很稀少的事件：卜瓦松

1 小時還是大多是 0 的筆數（例如設備告警一小時 0～2 次），中位數和 MAD 都是 0，MAD 方法失效。改用卜瓦松分布：

- 學每個時段的**平均次數 λ**。歷史裡一次爆量 50 筆會把平均拉高好幾倍，所以先把「在這個平均下幾乎不可能」的值壓到上限再平均，重複到穩定；某個時段 4 週都沒發生過，λ 也不當成 0（至少 0.5 ÷ 值的個數），不然出現 1 次就報。
- 判斷：平常每小時 λ = 0.5 次，這小時出現 6 次，機率 P(X ≥ 6) ≈ 0.00002，小於門檻（預設 0.001）就算異常；也要跟 λ 差至少 `min_abs`。
- 變少也能判斷，但 λ 很小時「0 次」本來就很常見，自然不會報。

### (3) 自己沒有 baseline：借同類的

新 API、量太少的 API，借**同一組**其他成員的 baseline：

## 抓不到的

這是方法本身的限制，不是程式的 bug。要靠其他種類的 baseline 補（見 [docs/rca/README.md 的「不同用途要的 baseline」](README.md#不同用途要的-baseline-不一樣)）：

| 抓不到的狀況 | 例子 | 原因 | 要怎麼補 |
|---|---|---|---|
| **慢慢惡化** | 記憶體每天漲一點，三週後才爆 | 每天都在範圍內，baseline 也跟著漲上去 | 「趨勢」baseline：看長期斜率、預測什麼時候會到上限 |
| **指標之間的關係** | 流量正常、CPU 卻很高（程式出問題，不是忙） | 一次只看一個數字，兩個各自都在範圍內 | 「關係」baseline：學平常 CPU 跟流量的比例 |
| **特殊日子** | 過年、年度大保養、盤點 | 過去 4 週沒有這種日子，會誤報 | 行事曆：那段時間不判斷，或用去年同期當 baseline（要先有 13 個月的資料，見 [docs/retention.md](../retention.md)） |
| **量太少的序列** | 一小時才幾個請求的 API | 跳動相對太大，範圍很寬，小變化抓不到 | 自動放大粒度、卜瓦松（見「自動調整」） |
| **沒見過的東西** | 新出現的錯誤訊息、新出現的呼叫關係 | 這套只看數字大小，不看「有沒有見過」 | 「集合」baseline：log 樣式字典、呼叫關係圖 |
| **新服務、新 API** | 剛上線的服務 | 沒有歷史就沒有 baseline，前幾天不判斷 | 借同類 API 的 baseline（只限比例、延遲類，見「自動調整」） |

## trace 每一段的 baseline

> 另見：[規格](../../engine/configs/baseline/reference.md#trace-每一段的-baseline)・[操作](../../engine/configs/baseline/reference.md#trace-每一段的-baseline)・[實測](../../engine/configs/baseline/verification.md#trace-每一段的-baseline)

上面是「每個服務一條線」。很多故障這樣看不到（[docs/rca/cases.md](cases.md)）：N+1、重試風暴、快取穿透時，每一次呼叫都很快，服務的延遲只動一點點，真正變的是**每個請求呼叫某一段幾次**。所以另外學 trace 每一段的「平常」：

**尾端取樣之後只用隨機那份：** trace-sampler 把錯的、慢的全留，正常的只留 5%；直接拿留下來的算平均，錯誤率、時間都會偏高。所以抽樣只抽 `tailsampling.policy = random` 的請求（[trace.yaml](../../engine/configs/baseline/trace.yaml) 的 `sample_policy`）；隨機那 5% 錯的慢的也一樣抽，統計跟「全部都記」一樣。

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

## 規則：狀態變化、離極限多遠

> 另見：[規格](../../engine/configs/baseline/reference.md#規則狀態變化離極限多遠)・[操作](../../engine/configs/baseline/reference.md#規則狀態變化離極限多遠)・[實測](../../engine/configs/baseline/verification.md#規則狀態變化離極限多遠)

有些問題不用學「平常」：發生了就是問題（容器重啟、副本不夠），或有明確的上限（記憶體、磁碟）。這些用規則判斷，**不用歷史，裝上去第一天就有用**。規則在 [rules.yaml](../../engine/configs/baseline/rules.yaml)：

## Pod 同儕橫向比對：同一個服務的 Pod 互相比

> 另見：[規格](../../engine/configs/baseline/reference.md#pod-同儕橫向比對同一個服務的-pod-互相比)・[操作](../../engine/configs/baseline/reference.md#pod-同儕橫向比對同一個服務的-pod-互相比)・[實測](../../engine/configs/baseline/verification.md#pod-同儕橫向比對同一個服務的-pod-互相比)

10 個 Pod 只有 1 個壞掉（錯誤率 100%、或只有它記憶體一直漲），整個服務平均起來只差一點，跟平常比（baseline）可能還在範圍內。同一個服務的 Pod 跑同一份程式、分到差不多的流量，應該長得差不多，所以**拿其他 Pod 當標準**：不用歷史，裝上去第一天就有用。設定在 [peer.yaml](../../engine/configs/baseline/peer.yaml)，程式在 [detector/peer.py](../../engine/detector/peer.py)。

## 餵給 baseline 之前：擋了什麼、還缺什麼

> 另見：[規格](../../engine/configs/baseline/reference.md#餵給-baseline-之前擋了什麼還缺什麼)

baseline 看過去 4 週學「平常」，那 4 週混了壞資料就學錯：天天噴 1,000 個錯沒人修，會學成「噴 1,000 個是正常」；上週的事故把平常拉歪；壓測、維護、批次的數字本來就不同。中位數＋MAD 只擋得住「少數」怪的點，所以統計之外還有幾層。

## 調整：哪些資料不學、從哪裡重新學、哪些時段放寬

> 另見：[規格](../../engine/configs/baseline/reference.md#調整哪些資料不學從哪裡重新學哪些時段放寬)・[操作](../../engine/configs/baseline/reference.md#調整哪些資料不學從哪裡重新學哪些時段放寬)・[實測](../../engine/configs/baseline/verification.md#調整哪些資料不學從哪裡重新學哪些時段放寬)

統計公式不動；baseline 學錯、報錯的情況，用「調整資料」修正（原則與限制見「[定義的原則與限制](../../engine/configs/baseline/reference.md#定義的原則與限制)」）。程式在 [detector/adjust.py](../../engine/detector/adjust.py)，跟 `tune` 寫同一個 index `aiops-tuning`（`kind` 區分）；長期的規則寫 [adjustments.yaml](../../engine/configs/baseline/adjustments.yaml)。

## 事故紀錄與值班回饋

> 另見：[規格](../../engine/configs/baseline/reference.md#事故紀錄與值班回饋)・[實測](../../engine/configs/baseline/verification.md#事故紀錄與值班回饋)

PDCA 的 Check：告警出去之後，人判斷對不對，才知道門檻準不準、哪裡要調。**先只收集，不自動生效**；單次回饋不動門檻。程式在 [detector/feedback.py](../../engine/detector/feedback.py)（計算）和 `__main__.py`（指令）。

## 慢性病清單、行事曆

> 另見：[規格](../../engine/configs/baseline/reference.md#慢性病清單行事曆)・[操作](../../engine/configs/baseline/reference.md#慢性病清單行事曆)・[實測](../../engine/configs/baseline/verification.md#慢性病清單行事曆)

**慢性病清單**：天天噴錯、沒人修的。baseline 跟自己的歷史比，天天都有的錯會被學成「平常」、永遠不報，所以另外列出來讓人修。設定在 [chronic.yaml](../../engine/configs/baseline/chronic.yaml)，程式在 [detector/chronic.py](../../engine/detector/chronic.py)。

## 資料品質檢查：資料本身怪不怪

> 另見：[規格](../../engine/configs/baseline/reference.md#資料品質檢查資料本身怪不怪)

baseline 用中位數＋MAD，只擋得住「少數」怪的點。資料本身壞掉時，學出來的「平常」就是錯的，或根本學不到。這幾種壞法不用學，用規則看得出來。設定在 [quality.yaml](../../engine/configs/baseline/quality.yaml)，程式在 [detector/quality.py](../../engine/detector/quality.py)。每個檢查看最近 1 小時，跟 24 小時前同一段比。

## 還沒做

1. **告警**：現在只寫進 `aiops-anomalies`。接 Kibana 的告警規則、或推到通訊軟體，要再決定。
2. **合併**：同一個服務的流量、錯誤、延遲同時異常時，現在是好幾筆，之後依服務與時間合併成一件事（[docs/rca/README.md](README.md) 的步驟 2）。
3. **排除已知事件、換版**：程式有了（`adjust`、行事曆，見「調整」），但條目要人寫；測試流量沒有自動辨識（見「[餵給 baseline 之前](../../engine/configs/baseline/reference.md#餵給-baseline-之前擋了什麼還缺什麼)」）。
4. **指標、借同組**：p99、慢請求比例、往外呼叫耗時還沒有；`groups` 沒定義（見「[定義的原則與限制](../../engine/configs/baseline/reference.md#定義的原則與限制)」）。
5. **不是 Java、.NET 的服務**：流量、錯誤率、延遲來自 `http.server.request.duration`（Java agent、.NET 自動埋點都有，見 [docs/metrics/dotnet.md](../metrics/dotnet.md)）。其他語言要用 OpenTelemetry 開出同名的直方圖，才學得到。
6. **更準的方法**：用一陣子、收集到誤報和漏報後，再考慮方法 B（拆週期）、C（預測模型）。
