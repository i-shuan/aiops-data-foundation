# AIOps baseline：學「平常的樣子」、抓異常

一句話：**每條時間序列（服務 × API × 指標），取過去 4 週同一個星期幾、同一個小時的中位數與波動範圍當「平常」；之後的值超出範圍就記成異常。**

這是 [docs/rca/README.md](../../../docs/rca/README.md) 的「方法 A」，屬於四種 baseline 裡的「數字 × 時段」（其他三種：集合、關係、趨勢與標註，見[不同用途要的 baseline](../../../docs/rca/README.md#不同用途要的-baseline-不一樣)）：不需要 GPU、不需要付費授權，結果好解釋（「比過去四週同時段高 5 倍」）。一台 16GB 記憶體的機器就跑得動。

## 做什麼

```
ES（log、metrics、trace）
   ↓ train：每天一次。預設 30 秒一點，量不夠的序列自己往粗調（1 分鐘～1 小時）
   ↓        每條序列 × 每個時段（星期幾 × 幾點）算中位數、波動（MAD）
   → aiops-baseline（每條序列 168 個時段）
   ↓ detect：每 30 秒一次，讀最近 30 分鐘，跟同時段的 baseline 比
   → aiops-anomalies（跳一次 warning、連續兩個時間窗 anomaly）
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

**流量、錯誤率、延遲從 metrics 算，不從 trace、log 算**（分工見 [docs/rca/README.md「分工」](../../../docs/rca/README.md#分工metrics-決定什麼時候告警trace-和-log-說明原因)）：

- 來源是 `http.server.request.duration` 直方圖（ES 欄位 `metrics.http.server.request.duration`；Java agent 經 Prometheus 進來的名字由 node Collector 改成這個，跟 .NET 一樣）。每個請求都算到；Collector 把 Prometheus 的累加值換成「每 30 秒多少」，所以任一段時間都能直接加總。
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

程式在 [detector/model.py](../../detector/model.py) 的 `train`（每個時段算中位數、MAD）和 `score`（算 σ、z、判斷）。跟兄弟比（peer）、trace 每一段的 baseline 用的是同一套。

### 什麼時候開始、多久訓練一次

| | 時間 |
|---|---|
| 每條序列多細 | 預設 30 秒一點（跟收值一樣），量不夠的自己往粗調到 1 分鐘～1 小時（見「自動調整」(1)）；每條序列切成 168 個時段（星期幾 × 小時）各自學 |
| 最快多久開始判斷 | 50 個點（`all` 層）：30 秒一點約 25 分鐘、5 分鐘一點約 4 小時；3 天後有「每天同一小時」；2 週後有「同星期幾 × 同小時」 |
| 訓練（`train`） | 建議每天一次；5 分鐘以上的粒度用最近 4 週，30 秒、1 分鐘用最近 14 天（跟 metrics 原始資料留 14 天一致，見 [retention.md](../../../docs/retention.md)） |
| 偵測（`detect`） | 建議每 30 秒一次，看最近 30 分鐘、只看已經結束（再退 30 秒確定收齊）的時間窗 |
| 現況 | ⚠️ 正式環境還沒排程，目前是測試環境手動跑（排程範例見「怎麼跑」） |

### 兩個條件都成立才算異常

1. 離中位數超過 `z` 倍（預設 5）的穩健標準差（1.4826 × MAD）。
2. 差距超過 `min_abs`。量很小的序列需要這條：例如凌晨的錯誤數平常是 1、MAD 是 0，只看倍數的話，1 變 2 就告警；`min_abs: 5` 表示至少多 5 筆才算。

### 跳一次是 warning，連續才是 anomaly

不要跳一點就算異常。超標的點分兩級（`status` 欄位）：

| 狀況 | status | 會怎樣 |
|---|---|---|
| 超標，但前一個時間窗正常 | `warning` | 寫進 `aiops-anomalies`，看得到「正在變壞」；**不觸發根因分析**、不通知 |
| 同一條序列、同一個方向，**連續 2 個時間窗**超標 | `anomaly` | 正式異常，根因分析會挑它 |
| 連續超標的時間合計 **≥ 30 分鐘** | `anomaly` | 粗粒度的序列不用等兩個點：1 小時一點的，一個點就是一小時；30 分鐘一點的也是一個點就夠；15 分鐘一點的要兩個 |
| 中間有一個時間窗正常、沒資料、或方向反過來 | 從頭算 | |
| **這條序列設了 `confirm_points: 1`**（例如 `http.error_rate`：一跳就是出事） | `anomaly` | 一次就算，不等第二個時間窗 |
| **偏太多**：一個點 \|z\| ≥ `instant_z`（預設 15，正常門檻 5 的 3 倍） | `anomaly` | 任何序列都適用，擋沒想到的暴衝 |

- **不用另外存狀態**：每次偵測多查前一個時間窗，從資料重算「連續第幾個」（`streak` 欄位），程式重啟也一樣。
- **不降級**：上一輪已判成 anomaly 的時間窗，這一輪就算查的範圍沒包含它的前一個時間窗，也維持 anomaly。
- 設定在 `series.yaml` 的 `status: {confirm_points: 2, confirm_span: 30m, instant_z: 15}`。每條序列可以自己設 `confirm_points`、`instant_z`（`http.latency_p95_s` 設 `null`：不直接 anomaly；寫 0 會讓每個超標點都直接 anomaly）；`tune --confirm-points 1 --instant-z 10` 可以再依對象調（寫進 `aiops-tuning`，之後回饋的建議也能調）。優先順序：對象 > 序列 > 全域（[pdca/phase2-anomaly/2026-10-instant-anomaly.md](../../../docs/pdca/phase2-anomaly/2026-10-instant-anomaly.md)）。
- 規則（`rules.yaml`）另外用 `for`（要持續多久）處理；重啟、Pod Failed 這類 critical 規則一次就算，不等。

**時間上的效果**（預設 30 秒一點，見「自動調整 (1)」）：30 秒一點的序列，偵測每 30 秒跑一次，**跳一次約 30 秒後 warning、連續兩個 30 秒（約 1 分鐘）就是 anomaly**；粒度被調粗的序列（量少、或使用者、feedback 調過的）照它自己的時間窗算。

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
- **突然永久改變**（例如換版後每個請求都變慢）：沒處理的話，前一、兩週會持續告警，直到新的狀況佔了歷史的一半以上，中位數才跟上。用 `reset_point` 從換版那刻重新學（見「調整」）。

### 定義的原則與限制

**原則**：統計公式（168 格中位數＋MAD）維持簡單，只抓一般異常。學錯、報錯的情況不改公式，用「調整資料」解：哪些資料不學、從哪裡重新學、哪些時段放寬（見「調整」）。調整由人寫，或由 learning loop（值班回饋、CI/CD）提出、人核准，都寫進同一個 `aiops-tuning`。**不做**：在公式裡加月份、季度的時段；自己訓練模型（[feedback/llm-mlops.md](../../../docs/feedback/llm-mlops.md)）。

| 限制 | 原因 | 影響 | 現況 |
|---|---|---|---|
| 訓練資料有壞資料（長期事故、壓測、維護） | 天天發生、或連續一兩週的壞資料會變成中位數本身 | 平常被學歪 | `exclude_window`、行事曆、慢性病清單（見「[餵給 baseline 之前](#餵給-baseline-之前擋了什麼還缺什麼)」） |
| 換版後一階跳變、停在新位置 | 新值要佔歷史一半（約 2 週）中位數才跟上 | 這段期間一直報 | `reset_point` |
| 比一週長的週期（月底、季底批次） | 168 格只涵蓋一週 | 月底誤報 | `recurring_override` |
| 指標不夠：沒有 p99、慢請求比例、往外呼叫耗時 | `series.yaml` 只有 7 條序列 | 只有 p95，看不到尾端 | 待排：加序列（改設定，不改公式） |
| 借同組實際不會發生 | `series.yaml` 的 `groups` 是註解掉的範例，沒有 `groups` 就不借 | 新 API 沒有 baseline 可比 | 要依公司服務命名定義 `groups`（要別人配合） |
| p95 從直方圖算，值落在桶中點 | MAD＝0，σ 只剩中位數的 5%；多兩個請求跨到下一格 z 約 23 | 已設 `instant_z: null`、`min_events.rate: 100`：合成資料一週 p95 anomaly 3.52% → 0.016%、warning 0.84% | 跨一格 z 仍大；待改程式（σ 至少半個桶寬），[threshold-tune](../../../docs/pdca/phase2-anomaly/2026-10-baseline-threshold-tune.md) |
| 錯誤率一個 5xx 就到門檻 | `min_events` 20、`min_abs` 5%、`confirm_points` 1 | 已設 `min_events.rate: 100`：合成資料一週 0.34% → 0% | — |
| 比例、百分位每窗至少 100 個請求 | 每 30 秒 40 個請求的 API 也湊不到三成時間窗，錯誤率、p95 變 5 分鐘一點 | 持續 5 分鐘的變慢只有 warning；30 秒的錯誤率尖峰被平均掉（33% → 3.3% 不報）；anomaly 最快 10 分鐘 | 待決定：退回 20、只留 `instant_z: null` 再量，[threshold-tune](../../../docs/pdca/phase2-anomaly/2026-10-baseline-threshold-tune.md) |
| `hour` 層混平日與週末 | 每天同一小時不分星期幾 | 某個週一收集中斷後兩週，週一那幾小時流量掉九成只有 z −4；剛上線 3 天週一會 warning | 待評估：hour 層分平日／週末 |
| 尖峰不對齊整點 | 168 格以整點切；08:45 起的尖峰只佔 8 點那格的四分之一 | 每個平日 08:45～09:00 報 anomaly | `recurring_override`；或待評估邊界比相鄰時段 |

## 自動調整：粒度、低頻事件、借用 baseline

> 狀態：(1)(2)(3) 都做好了，`adaptive_e2e` 驗證過（見「驗證」）。設定在 `series.yaml` 的 `adaptive`、`groups`、`borrowing`。

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

### (1) 每條序列自己選粒度

- **預設 30 秒一點**（跟收值頻率一樣），量不夠才往粗調：**看底下的事件數，不是看有幾個點**——p95 由 3 個請求算出來就是雜訊。每條序列算「每個時間窗的事件數」，從 30s、1m、5m、15m、30m、1h 裡挑**最細、而且至少三成（`coverage: 0.3`）的時間窗夠多**的。不用中位數（一半）：很多 API 只有白天有人用，晚上、週末的時間窗本來就少，用中位數會整條不判斷。

  | 指標類型 | 事件數是什麼 | 預設門檻（每窗） |
  |---|---|---|
  | 比例（錯誤率）、百分位（p95） | 這段時間的請求數 | ≥ 100（1 個 5xx 最多 1%） |
  | 筆數（請求數、log 數、事件數） | 筆數本身 | ≥ 10 |
  | 量測值（CPU、記憶體） | 不檢查，固定最細（30 秒） | — |

- **有的序列不能細到 30 秒**（`min_resolution`）：請求數是「每 30 秒收一次的增量」，收值時間有抖動，30 秒窗偶爾收到 0 筆或 2 筆，看起來像歸零、翻倍，所以 `http.requests` 最細 1 分鐘。錯誤率、p95（比例、百分位不受影響）、CPU、記憶體（平均）、log 筆數（每筆有自己的時間）可以 30 秒。
- **比原始資料細的粒度不選**：資料 5 分鐘一筆，就不會選 30 秒。
- **細粒度只學最近 14 天**（`fine_history`）：30 秒一點 4 週是 8 萬點；14 天還夠「同星期幾 × 同小時」（每個時段 2 天 × 120 點）。5 分鐘以上照樣學 4 週。
- **偵測往前退 30 秒**（`lag`）：剛結束的 30 秒可能還沒收齊（Collector 批次送、ES refresh），看起來像驟降。

- **事件數不夠的時間窗不判斷**（比例、百分位類）：選了 1 小時之後，晚上那幾個小時可能還是只有幾個請求，那幾窗不學、也不判斷（是雜訊，不是「平常」）。筆數類不受影響（0 筆本身就有意義，例如服務停了）。
- 粗的粒度要重新跟 ES 查（百分位不能從 5 分鐘的值合併），只對需要的序列查。
- 沒出現的時間窗算 0 個事件（沒有請求），但只從這條序列第一次出現算起；收集系統停機的時段當成「不知道」。
- 預先統計好的資料（例如 exporter 每 5 分鐘吐一筆「請求數、平均延遲」）：用 `events: {sum: requests}` 指定事件數在哪個欄位、`kind: rate`；值本身就是筆數的寫 `events: value`。
- **粗粒度自然會退到粗的季節性**：1 小時一點時，「同星期幾 × 同小時」4 週只有 4 個值，不夠（每個時段至少要 6 個值），自動退到「每天同一小時」。
- **代價是慢**：1 小時一點，最慢 1 小時後才報。要馬上知道的（設備停機、重大告警）本來就該用規則，有就報。

### 使用者、feedback 調整

自動選的不一定合適（例如報表 API 本來就跳動大，30 秒太敏感）。可以針對某條序列、某些對象調**粒度、z、min_abs**：

| 從哪裡調 | 怎麼調 | 什麼時候生效 |
|---|---|---|
| 使用者設定 | `series.yaml` 的 `overrides`（`series` 可用正規表示式、`match` 依 keys 比對） | 粒度：下次 train；z、min_abs：下次 detect |
| 指令 | `python3 -m detector tune --series http.latency_p95_s --match service=^mes-report --resolution 5m --z 7 --reason 誤報多 --by 王小明`，寫進 `aiops-tuning`（`--list` 列出、`--remove` 刪掉） | 同上 |
| feedback（📝） | 之後的 feedback 流程寫同一個 `aiops-tuning`：同一條序列誤報累積多次 → 建議粒度往粗一級或調高 z，回測通過才寫入（見 [docs/feedback/README.md「對了怎麼強化、錯了怎麼 feedback」](../../../docs/feedback/README.md#對了怎麼強化錯了怎麼-feedback)） | 同上 |

套用順序：自動選 → `series.yaml` 的 `overrides`（檔案順序）→ `adjustments.yaml` → `aiops-tuning`（依 `at` 由舊到新），同一欄位後面的蓋前面的，不同欄位各自生效；不看 `match` 寫得多細（較晚寫的寬鬆調整會蓋掉較早的精細調整）。每筆調整記 `reason`、`by`、`at`，查得到誰、為什麼、什麼時候改的。

### (2) 很稀少的事件：卜瓦松

1 小時還是大多是 0 的筆數（例如設備告警一小時 0～2 次），中位數和 MAD 都是 0，MAD 方法失效。改用卜瓦松分布：

- 學每個時段的**平均次數 λ**。歷史裡一次爆量 50 筆會把平均拉高好幾倍，所以先把「在這個平均下幾乎不可能」的值壓到上限再平均，重複到穩定；某個時段 4 週都沒發生過，λ 也不當成 0（至少 0.5 ÷ 值的個數），不然出現 1 次就報。
- 判斷：平常每小時 λ = 0.5 次，這小時出現 6 次，機率 P(X ≥ 6) ≈ 0.00002，小於門檻（預設 0.001）就算異常；也要跟 λ 差至少 `min_abs`。
- 變少也能判斷，但 λ 很小時「0 次」本來就很常見，自然不會報。

### (3) 自己沒有 baseline：借同類的

新 API、量太少的 API，借**同一組**其他成員的 baseline：

- **組要事先定義**（系統不會自己知道誰是「Query API」）：`series.yaml` 的 `groups`，依 keys 的正規表示式比對，由細到粗排，例如先 `mes-query`（服務 `mes-*`、路徑查詢類），再 `mes`（服務 `mes-*`）。之後可改從 K8s label 或服務清單讀。⚠️ 目前 `series.yaml` 的 `groups` 只是註解掉的範例，**實際不會借**。
- **只借比例、延遲類**（序列設 `borrow: true`）：錯誤率在同類 API 間大致可比；p95 勉強可比；**請求數、事件數不借**，新 API 的流量跟別人無關。
- 組的 baseline：`train` 時從成員自己的 baseline 算（至少 3 個成員），每個時段取成員中位數的中位數、成員 MAD 的中位數，跟成員的 baseline 存在一起（`baseline_source: group:<組名>`）。一個特別慢的成員不會把組拉偏。
- `detect` 時，`borrow: true` 的序列裡**沒有自己 baseline** 的，依 `groups` 的順序找第一個有組 baseline 的組來比。
- 借來的門檻調高（預設 z 從 5 變 7），一律 1 小時一點（本來就是可信度低的判斷，用粗一點的窗比較穩）。

```yaml
groups:                      # 由細到粗；match 的每個欄位都要符合（正規表示式）
  - name: mes-query
    match: {service: "^mes-", route: "query|search"}
  - name: mes
    match: {service: "^mes-"}
borrowing: {min_members: 3, z: 7}
series:
  - name: http.latency_p95_s
    borrow: true             # 只有比例、延遲類設
```

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
| `resolution` | 30s / 1m / 5m / 15m / 30m / 1h |
| `method` | `mad`（中位數＋MAD）／`poisson` |
| `level` | dow_hour / hour / all / group |
| `baseline_source` | `self` 或 `group:<組名>` |
| `dept`、`group` | 這筆異常屬於哪個部門、哪個 group（告警依它分送、人只看自己部門的）。寫入時依 keys 裡的 namespace（優先）或服務，對回資料裡的 `resource.attributes.dept`、`.group`；對不到的 `dept` 空、`group` 是 `unassigned`（跟 Collector 對沒有 dept 的處理一樣）。程式 `detector/owner.py` |
| `confidence` | dow_hour → high；hour → medium；all、借來的 → low |

卜瓦松的異常另外記 `p`（機率）。根因報告的異常表多一欄「可信度」（例如 `low、1h、借 query-apis`），有 low 時加註「用的是不分時段、或借同類 API 的 baseline（不是自己的歷史），先確認是不是真的變了」。

## 抓不到的

這是方法本身的限制，不是程式的 bug。要靠其他種類的 baseline 補（見 [docs/rca/README.md 的「不同用途要的 baseline」](../../../docs/rca/README.md#不同用途要的-baseline-不一樣)）：

| 抓不到的狀況 | 例子 | 原因 | 要怎麼補 |
|---|---|---|---|
| **慢慢惡化** | 記憶體每天漲一點，三週後才爆 | 每天都在範圍內，baseline 也跟著漲上去 | 「趨勢」baseline：看長期斜率、預測什麼時候會到上限 |
| **指標之間的關係** | 流量正常、CPU 卻很高（程式出問題，不是忙） | 一次只看一個數字，兩個各自都在範圍內 | 「關係」baseline：學平常 CPU 跟流量的比例 |
| **特殊日子** | 過年、年度大保養、盤點 | 過去 4 週沒有這種日子，會誤報 | 行事曆：那段時間不判斷，或用去年同期當 baseline（要先有 13 個月的資料，見 [docs/retention.md](../../../docs/retention.md)） |
| **量太少的序列** | 一小時才幾個請求的 API | 跳動相對太大，範圍很寬，小變化抓不到 | 自動放大粒度、卜瓦松（見「自動調整」） |
| **沒見過的東西** | 新出現的錯誤訊息、新出現的呼叫關係 | 這套只看數字大小，不看「有沒有見過」 | 「集合」baseline：log 樣式字典、呼叫關係圖 |
| **新服務、新 API** | 剛上線的服務 | 沒有歷史就沒有 baseline，前幾天不判斷 | 借同類 API 的 baseline（只限比例、延遲類，見「自動調整」） |

## trace 每一段的 baseline

上面是「每個服務一條線」。很多故障這樣看不到（[docs/rca/cases.md](../../../docs/rca/cases.md)）：N+1、重試風暴、快取穿透時，每一次呼叫都很快，服務的延遲只動一點點，真正變的是**每個請求呼叫某一段幾次**。所以另外學 trace 每一段的「平常」：

| 數字 | 意思 | 抓什麼 |
|---|---|---|
| `trace.segment.calls` | 每個請求呼叫這一段幾次 | N+1（1 次 → 100 次）、重試風暴（1 → 4）、快取穿透（0.05 → 1）；變少也報（功能可能沒跑） |
| `trace.segment.time_ms` | 每個請求在這一段花多久 | 下游拖累（簡訊廠商掛了）、熱點鎖、慢查詢 |
| `trace.segment.error_rate` | 這一段出錯的請求比例 | 某個呼叫開始失敗 |
| `trace.segment.new` | 這支 API 從來沒出現過的呼叫 | 換版後開始直連另一個資料庫、多呼叫一個外部系統（規則：這支 API 有 baseline、這一段沒有，每個請求至少 0.5 次） |
| `trace.segment.swallowed_rate` | 這一段丟了例外、請求卻沒失敗的比例（這一段沒標錯誤；或這一段標了錯誤、入口沒有） | 吞掉的例外（catch 掉、回空值或 200）變多 |
| `trace.clock_skew_ms` | 被呼叫方比呼叫方早開始（或晚結束）多少 | 時鐘不同步（規則，不用學：超過 500ms 就記） |
| `trace.segment.repeated` | 同一個請求裡，同一個往外的呼叫平均幾次 | **N+1、迴圈裡打 API**（規則，不用學：≥ 20 次）。從上線第一天就有的 N+1，baseline 會學成平常，所以要有固定門檻 |
| `trace.segment.swallowed` | 同上的吞掉例外比例 | **吞掉的例外**（規則，不用學：≥ 5% 的請求）。訊息帶例外種類，例如「5% 的請求丟了 System.TimeoutException，但沒標成錯誤」 |

**訊息消費者（Kafka）：** 入口除了 HTTP 請求（Server），也算訊息消費者（Consumer）和「沒有上一層的 span」。消費者多一段「`服務 › 排隊（topic）← 生產者`」＝拿到訊息的時間 − 生產者送出的時間（生產者是上一層，或用 link 找回來）。.NET 自動埋點的消費者處理時查的 DB 自成一個 trace，當成自己的入口算，所以看得到「這個消費者寫 DB 變慢、出錯」。實測見 [docs/trace/scenarios.md 場景四](../../../docs/trace/scenarios.md#四非同步訊息佇列)。

吞掉的例外要靠 Collector：ES 不存 span 的例外事件，node Collector 先把例外的種類、訊息抄到 span 的屬性（`exception.type`、`exception.message`，[node.yaml](../../../telemetry/collectors/node.yaml) 的 `transform/exceptions`）。兩種樣子都算吞掉：這一段有例外、status 沒設；或這一段有例外又標了錯誤，但這個服務的入口沒標錯誤（Java agent 的 JDBC、HTTP client 丟例外一定標錯誤，程式 catch 掉、回 200 就是這樣）。錯誤傳到入口（回 500）的算錯誤，不算吞掉。

往外呼叫的對象依序取 `peer.service`、`db.system`、`db.system.name`（新版 DB 語意）、`server.address`；都沒有的 Client span 段名沒有「→ 對象」，`repeated`（N+1）規則看不到。

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
python3 -m detector trace-collect --config configs/baseline/trace.yaml   # 第一次：補過去 7 天的統計（trace 只留 7 天）
python3 -m detector trace-train   --config configs/baseline/trace.yaml   # 每天
python3 -m detector trace-detect  --config configs/baseline/trace.yaml   # 每 5 分鐘（同時累積統計）
```

剛上線時只有 7 天的統計，學出來是 `hour` 或 `all` 層級；累積到 2 週以上才有「同星期幾 × 同小時」。

**實測**（`e2e/local-k3s/run-trace-baseline-e2e.sh`，兩個模擬服務走真正的路線：Collector → ES；測試用 1 分鐘一點、學 20 分鐘）：

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

邊緣情境（`tests/trace_quality_edge_e2e.py`，自己的 ES 灌合成 trace，照正式規則做尾端取樣）見 [trace-quality-verify](../../../docs/pdca/phase2-anomaly/2026-10-trace-quality-verify.md)：
- 只讀 random：錯誤率 4.3%（真實 4.0%）；混進全留的那份是 34.6%。
- 抓不到的：只有 10% 請求慢 2 秒；每 5 分鐘不到 100 個請求的 API 學不到。

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
| `kafka.consumer_lag`、`kafka.consumer_lag_growing` | 消費落後筆數；5 分鐘淨增加多少（`delta`） | ≥ 10,000，3 分鐘；≥ 5,000 | warning |
| `eap.secs_disconnected`、`eap.plc_scan_slow`、`eap.secs_retransmits` | 機台 SECS 通訊、PLC 掃描週期、重傳（依 `equipment_id`） | 斷線 1 分鐘；≥ 規格 50ms，2 分鐘；5 分鐘重傳 ≥ 10 次 | critical／warning |

四個領域的案例與實測見 [docs/metrics/cases.md](../../../docs/metrics/cases.md)。

寫法：`value`（欄位跟門檻比）、`compare`（兩個欄位相減）、`ratio`（欄位 ÷ 分母）、`increase`（累計值增加多少，歸零重來也算得對；只能用在計數器）、`delta`（量測值淨增加多少，最後 − 最前；例如 Kafka 落後）、`increase_ratio`（兩個累計值增加量的比例）、`rate`（加總 ÷ 秒數）、`total`（加總；`hist_count` 數直方圖的次數）。分母在另一種文件時用 `join` 補進來。`for` 是「這段時間都成立才算」（對象要在 for 開頭之前就有資料：新建的 Pod、Deployment 等滿 for 才報），`min_ratio` 放寬成「這段時間裡至少多少比例成立」。對象不在了（Pod 刪掉、節點拿掉，最新的資料超過 2 分鐘）就不報；只有容器重啟、Job 失敗設了 `report_gone: true`，刪掉了也照報。要加規則，在 rules.yaml 加一段，不用改程式。一條規則查詢失敗（或核准的草稿缺欄位）只跳過那一條、其他照跑，指令最後 exit 1。核准的規則草稿（`aiops-tuning` 的 `rule_suggestion`）`expires_at` 到了就不載入。邊緣案例見 `tests/rules_edge_e2e.py`。

觸發的寫進 `aiops-anomalies`：`series` 是 `rule.<名稱>`、`level` 是 `rule`，另外有 `severity`、`message`（例如「容器重啟：rules-test、crash-app-…、app（1，門檻 >= 1）」）、`since`（從什麼時候開始成立）。同一條規則、同一個對象，1 分鐘只記一筆。

```bash
python3 -m detector rules --config configs/baseline/rules.yaml      # 每分鐘
```

**還沒加：** 趨勢（例如老年代照這個速度幾小時後會滿）、網卡丟包（要 node_exporter）、資料庫自己的指標（長交易、慢查詢，要 DB 的 exporter）。

## 跟兄弟比：同一個服務的 Pod 互相比

10 個 Pod 只有 1 個壞掉（錯誤率 100%、或只有它記憶體一直漲），整個服務平均起來只差一點，跟平常比（baseline）可能還在範圍內。同一個服務的 Pod 跑同一份程式、分到差不多的流量，應該長得差不多，所以**拿其他 Pod 當標準**：不用歷史，裝上去第一天就有用。設定在 [peer.yaml](peer.yaml)，程式在 [detector/peer.py](../../detector/peer.py)。

| 檢查 | 每個 Pod 算什麼（最近 10 分鐘） | 分組 | 觸發（跟其他 Pod 的中位數比） |
|---|---|---|---|
| `peer.http_error_rate` | 錯誤率（5xx ÷ 全部） | namespace × 服務 | 高 5 倍穩健標準差，而且多 5 個百分點以上 |
| `peer.http_latency_p95_s` | p95 延遲 | namespace × 服務 | 同上，而且慢 100ms 以上、是別人的 2 倍以上 |
| `peer.pod_cpu` | CPU 平均 | namespace × Deployment | 同上，而且多 0.2 核以上、是別人的 3 倍以上 |
| `peer.pod_memory_bytes` | 記憶體（working set）最高 | namespace × Deployment | 同上，而且多 100MB 以上、是別人的 1.5 倍以上 |

怎麼比：每個 Pod 跟「**其他** Pod」的中位數、MAD 比（跟 baseline 一樣的穩健統計；比的時候不含自己，壞掉的那個不會把標準拉過去）。另外：
- **至少 3 個 Pod 才比**：只有 2 個分不出誰才是怪的。
- **只比現在還在的 Pod**（最近 2 分鐘有資料）：換版、縮容刪掉的不算兄弟。換版途中新舊 Pod 會一起比，新版的 Pod 都比舊的差也會報，這正好是想知道的。
- **剛起來的 Pod 不比**（`warmup` 5 分鐘：window 開頭到 5 分鐘前之間沒有資料的不比）：啟動時 CPU 高、記憶體還在長，會誤報新 Pod 自己，或在擴容時讓舊 Pod 的記憶體看起來偏高。一起來就壞的 Pod 5 分鐘後照樣抓。
- **流量太少的 Pod 不比錯誤率、延遲**（10 分鐘不到 20 個請求）：一兩個錯就跳很高。
- **全部一起壞不報**：例如下游 DB 掛了，每個 Pod 都錯，那不是「某個 Pod」的問題，交給 baseline 和規則。其他 Pod 的中位數已經到規則門檻（錯誤率 5%、p95 2 秒，`max_peer_median`）時，最嚴重的那個也不單獨挑出來。
- 門檻比 baseline 寬（`rel_floor` 10%，再加上至少差多少、至少幾倍）：同一個服務的 Pod 本來就不會一模一樣。

觸發的寫進 `aiops-anomalies`：`series` 是檢查名稱（`peer.…`）、`level` 是 `peer`、`median` 是其他 Pod 的中位數，`message` 例如「這個 Pod 的錯誤率比同服務其他 Pod 高：metrics-cases、dotnet-app、dotnet-app-…（100%，其他 2 個 Pod 中位數 0%，z=…）」。

```bash
python3 -m detector peer --config configs/baseline/peer.yaml      # 每分鐘
```

**實測**（[e2e/local-k3s/run-peer-e2e.sh](../../../e2e/local-k3s/run-peer-e2e.sh)，2026-10-10，全部通過）：真的 .NET 應用（自動埋點）開 3 個副本，每次只弄壞一個。

| 案例 | 怎麼弄壞 | 抓到的 | 其他 2 個 Pod |
|---|---|---|---|
| 正常 | — | 不報 | 不報 |
| 連線池漏光 | 一個 Pod 拿了連線不還，之後的請求等 2 秒後 500 | 4 分鐘：錯誤率 **13%**（其他 0%）、p95 **1,750ms**（其他 2ms） | 不報 |
| 記憶體洩漏 | 一個 Pod 每個請求 32KB 放進不會清的 list | 第 5 分鐘：**294MB**（其他 163MB） | 不報 |
| 無窮迴圈 | 一個 Pod 裡有程式佔滿 1 核 | 第 3 分鐘：CPU **0.65 核**（其他 0.21 核，10 分鐘平均） | 不報 |

副本數 1～5、Pod 換掉、擴縮容、全壞、同名服務的邊緣情境：[phase2-anomaly/2026-10-peer-verify.md](../../../docs/pdca/phase2-anomaly/2026-10-peer-verify.md)。

同一段時間整個服務的錯誤率是 **9%**（3 個 Pod 裡 1 個壞）。這次服務平常是 0%，跟平常比（baseline）也會報，但只知道「服務錯誤率高」、不知道是哪個 Pod；Pod 越多稀釋越多：10 個 Pod 壞 1 個大約剩 1%，就低於 baseline 的門檻（多 5 個百分點）了。

## 餵給 baseline 之前：擋了什麼、還缺什麼

baseline 看過去 4 週學「平常」，那 4 週混了壞資料就學錯：天天噴 1,000 個錯沒人修，會學成「噴 1,000 個是正常」；上週的事故把平常拉歪；壓測、維護、批次的數字本來就不同。中位數＋MAD 只擋得住「少數」怪的點，所以統計之外還有幾層。

| 層 | 擋什麼 | 在哪 |
|---|---|---|
| 統計本身 | 中位數＋MAD：少數幾次事故拉不歪平常 | `model.py` 的 `train`（「為什麼用中位數和 MAD」） |
| | 卜瓦松：稀少筆數裡的爆量先壓掉再平均 | 「自動調整」(2) |
| | `mask_thin`：事件數不夠的時間窗（比例、百分位類）當「不知道」，不學也不判斷 | 「自動調整」(1) |
| | 歷史不夠往下退層，再不夠就不給 baseline | 「怎麼判斷」 |
| | 收集系統停機（整個 index 沒資料）的時段當「不知道」，不當成零流量 | 「怎麼判斷」 |
| | `lag` 30 秒：還沒收齊的時間窗不看 | 「怎麼跑」 |
| 資料本身壞掉 | 路由沒參數化、身分欄位缺、資料停了、落到 unassigned 變多、被 ES 拒收（`quality.ingest_rejected`）、收資料延遲、時鐘偏差 | 「[資料品質檢查](#資料品質檢查資料本身怪不怪)」 |
| 已知干擾 | 事故、維護、壓測不學（`exclude_window`；確認過的事故可用 `incident add --exclude-training` 提）；換版重新學（`reset_point`）；月底批次放寬（`recurring_override`） | 「[調整](#調整哪些資料不學從哪裡重新學哪些時段放寬)」 |
| 歲修、機房維護 | 行事曆一筆變成一個 `exclude_window` | 「[慢性病清單、行事曆](#慢性病清單行事曆)」 |
| 天天噴錯、沒人修 | 不排除，每週列成慢性病清單讓人修（天天發生的就是中位數本身，排除也學不到平常） | 同上 |

**還缺的**：

- **都沒排程**：`quality`、`chronic` 在 `deploy/cronjobs-stage2.yaml`（第二階段），`adjust`、行事曆在 `train` 時生效但條目要人寫。
- **測試流量沒有自動辨識**：壓測要人手動寫 `exclude_window`；沒有依 label（loadgen、測試 namespace）自動不學。
- **默默掉資料看不到**：ES `_otlp` 入口回 200 卻整批拒掉時（權限不夠、資料流名稱錯），Collector 沒 log、沒指標；要有 Collector 送出筆數跟 ES 實際寫入筆數的對照（可放進 `quality` 的 `stopped` 類）。
- **慢性病清單沒送到負責人手上**；`owners` 目前是假資料（`fake-data/owners.json`）。
- 資料品質檢查的門檻是估的（見「[門檻怎麼校、誰來調](#門檻怎麼校誰來調)」）。

## 調整：哪些資料不學、從哪裡重新學、哪些時段放寬

統計公式不動；baseline 學錯、報錯的情況，用「調整資料」修正（原則與限制見「[定義的原則與限制](#定義的原則與限制)」）。程式在 [detector/adjust.py](../../detector/adjust.py)，跟 `tune` 寫同一個 index `aiops-tuning`（`kind` 區分）；長期的規則寫 [adjustments.yaml](adjustments.yaml)。

| kind | 用途 | train | detect |
|---|---|---|---|
| `override` | 固定粒度、改 z、`min_abs`（`tune` 指令；舊資料沒有 `kind` 也是） | 粒度 | z、`min_abs` |
| `exclude_window` | 事故、維護、壓測 | `start`～`end` 的點不學 | `apply_to` 含 `detect` 時這段不報 |
| `reset_point` | 換版、架構調整 | `reset_at` 之前的歷史不學 | 之後 `warmup`（預設 24 小時）內不報；之後 baseline 要是重置後重新學的才判斷 |
| `recurring_override` | 月底批次、固定排程 | 預設照學；`effect.train: exclude` 不學 | 符合 `schedule` 的時段：`mute` 不報，或 z、`min_abs` 放寬（只會變少報，不會變嚴） |

- **對象**：`series`（正規表示式）、`match`（正規表示式，欄位可用別名：`service` 同時對到 `service`、`service.name`、`resource.attributes.service.name`、`entry.service`；另有 `namespace`、`pod`、`deployment`、`route`、`method`）。`tune` 的 `match` 也能用別名。
- **週期**：`schedule: {cron: "0 20 * * *", duration: 4h, days_of_month: [-1]}`：台北時間每天 20:00 起 4 小時，只限每月最後一天（cron 寫不出「最後一天」）。
- **時間**：沒寫時區的（`--start 2026-10-20T02:00`、`adjustments.yaml` 的 `start`）是台北時間（`train.timezone`），跟行事曆一樣。
- **learning loop 的接口**：`status: proposed` 不生效，`adjust approve` 才生效；`source` 記誰寫的（`manual`／`feedback`／`cicd`）；`expires_at` 到期自動失效（以這輪 `train --end`、`detect --at` 的時間點判斷，回放時看得到當時的設定）。讀不到 `aiops-tuning` 時印警告、只用設定檔的；缺欄位、正規表示式錯的調整略過那一筆並印出，不讓整輪失敗。

```bash
python3 -m detector adjust add --kind exclude_window --match service=^mes-api$ \
    --start 2026-10-20T02:00:00+08:00 --end 2026-10-20T05:00:00+08:00 --reason "DB 鎖表事故"
python3 -m detector adjust add --kind reset_point --match service=^mes-api$ --at 2026-10-22T10:00:00+08:00 \
    --source cicd --status proposed --id mes-api-v2 --reason "v2 上線"
python3 -m detector adjust add --kind recurring_override --match service=^mes-report --series "log\..*" \
    --cron "0 20 * * *" --duration 4h --days-of-month -1 --z 8 --reason "月底報表批次"
python3 -m detector adjust list
python3 -m detector adjust approve --id mes-api-v2 --by bob
python3 -m detector adjust remove  --id mes-api-v2
```

**實測**（2026-10-23，ES 9.5.5，log 8 天）：

| 案例 | 沒有調整 | 有調整 |
|---|---|---|
| `long`：8 天裡 5 天是長事故（log 3 倍） | 平常學成 13.57（每 5 分鐘；正常是 5）：事故變成平常 | `exclude_window` 排掉事故 → 15.00（每 15 分鐘，即每 5 分鐘 5）：回到正常 |
| `ver`：3 天前換版，log 變 2 倍 | 平常學成 5.36（舊版） | `reset_point`（`proposed` 時不變；`approve` 後）→ 10.00（新版） |
| `long` 最近 40 分鐘 log 爆 10 倍 | 報 | 週期時段 `mute` → 不報；`reset_point` 後 24 小時暖機 → 不報 |
| 舊的 `tune` 調整 | — | 照常生效，`adjust list` 一起列出 |

## 事故紀錄與值班回饋

PDCA 的 Check：告警出去之後，人判斷對不對，才知道門檻準不準、哪裡要調。**先只收集，不自動生效**；單次回饋不動門檻。程式在 [detector/feedback.py](../../detector/feedback.py)（計算）和 `__main__.py`（指令）。

| 指令 | 做什麼 |
|---|---|
| `feedback pending` | 最近還沒人標記的告警（只列 `anomaly`；`warning` 還沒通知任何人）和它們的 id |
| `feedback add --anomaly <id> --verdict …` | `true_positive` 真的、`false_positive` 誤報、`wrong_root_cause` 真的但根因錯（`--root-cause` 寫正確的）、`missed` 漏報（沒有 id，給 `--series --match --at`）。同一個人對同一筆再標會覆蓋 |
| `feedback summary` | 每條序列的告警數、已標記比例、誤報率；誤報累積多次的對象；漏報 |
| `feedback suggest` | (1) 同一個對象誤報 ≥ 3 次 → 提一筆放寬 z（× 1.4）的建議；(2) 漏報 → 那條序列、那個對象降 z（× 0.8）；(3) 同一個服務、同一個根因分類被老師傅確認 ≥ 3 次（`--min-recurring`）→ 提「升級成規則」：**門檻算好**（那幾件事故的延遲峰值取最小值，三件都抓得到）、查法跟那條 baseline 序列一樣、只看這個服務（三件同一支 API 就只看它）；`adjust approve` 後 **`rules` 自動載入**，不用改 `rules.yaml`；算不出門檻的不能核准。都是 `status: proposed`；override 要先 `backtest` 再 `adjust approve` |
| `backtest --id <建議> --weeks 4` | 改門檻前先回測：用「現行」和「現行＋這筆建議」各重跑過去 N 週的 detect（不寫 ES），比告警少幾個、已確認的事故（`aiops-incidents` confirmed／corrected／resolved）還抓不抓得到、漏報抓到沒；少抓一件已確認的事故就「不建議」。結果寫回建議的 `backtest` 欄位，`adjust list` 看得到 |
| `adjust approve --id … --by …` | override 類的建議**沒回測、或回測不建議的不能核准**（要跳過用 `--force`）；記 `approved_by`、`approved_at` |
| `metrics --from … --to … [--compare-from … --compare-to …]` | 量化：件數（每天、依 dept）、有回饋的比例、誤報率、根因錯的比例、漏報、從告警到第一筆回饋的中位時間、老師傅確認的事故數、「怎麼看出來的」有填的比例；兩段並排就是「學之前 vs 學之後」。只印 Markdown（`--out` 存檔），畫圖交給 Grafana |
| `python3 -m feedback_web` | 老師傅的回饋網頁（通知裡的連結）：一鍵真的／誤報／根因錯，多填根因分類、根因、處置、**怎麼看出來的**、查了哪裡、下次怎麼更快；寫 `aiops-feedback`（一件的每筆異常各一筆）和 `aiops-incidents`（`confirmed`／`corrected`／`false_positive`） |
| `incident add --title --start --end --service …` | 事故紀錄：時間、影響的服務、根因、處置、相關異常。`--exclude-training` 同時提「這段不學」（`exclude_window`，proposed，只排除列出的服務；沒列就全部） |
| `incident list`／`incident resolve --id` | 列出／結案（補根因、處置） |

寫進 `aiops-feedback`、`aiops-incidents`；建議寫進 `aiops-tuning`（跟 `tune`、`adjust` 同一處）。

**測資與結果**（2026-10-24，ES 9.5.5；14 筆異常：同一個對象 6 次、另一條路由 1 次、錯誤率 4 次＋1 個 warning、log 1 次、規則 1 次）：

| 情境 | 結果 |
|---|---|
| `pending` | ✅ 13 筆（warning 不列；規則沒有 `status` 也算告警） |
| 同一個對象標 4 次誤報、1 次真的 | ✅ 誤報率 80%；列為候選 |
| 同一個人對同一筆先標誤報、改標真的 | ✅ 覆蓋，只算一次 |
| 兩個人對同一筆都標誤報 | ✅ 只算一筆（不會因為重複點而湊滿 3 次） |
| 漏報（沒有 id） | ✅ 列在漏報、對象不重複 |
| `wrong_root_cause` 附正確根因 | ✅ 記錄、不算誤報 |
| `suggest` | ✅ 只對誤報 ≥ 3 次的那個對象提一筆（z 5 → 7）；誤報 1 次的不提；再跑一次不重複 |
| `proposed` 時 | ✅ 不生效（`override_for` 回空） |
| `approve` 後 | ✅ 只對那個對象生效；同服務的 `/health`、名稱相近的 `mesXapi` 不受影響 |
| 事故 `--exclude-training` | ✅ proposed 時 `train` 不排除；`approve` 後排除那段（7,200 點）；沒列服務的建議是全部 |
| 缺參數、不存在的 id、錯的 verdict | ✅ 清楚的錯誤訊息，不寫入 |

**2026-10-25 補的**（docker ES、模擬資料實測，見 [pdca/phase5-learning-loop/2026-10-loop-update.md](../../../docs/pdca/phase5-learning-loop/2026-10-loop-update.md)、[2026-10-expert-feedback.md](../../../docs/pdca/phase5-learning-loop/2026-10-expert-feedback.md)）：回饋網頁；誤報 3 次 → 建議 → `backtest`（告警 3 → 1、已確認事故不受影響）→ `approve` → 同樣的批次不再報；同根因 3 次 → 規則草稿；漏報 → 降 z 的建議；`metrics` 兩段並排。

**還沒做**：每筆異常記是哪一版產生的、評測集（要真實事故）；`suggest` 只調 z，不調 `min_abs`、粒度，也不會提「排除時段」（月底批次更該用 `adjust add --kind recurring_override`，現在要人手動）；規則核准前沒有回測（用 `rules --at <事故時間>` 看會不會觸發）。

## 慢性病清單、行事曆

**慢性病清單**：天天噴錯、沒人修的。baseline 跟自己的歷史比，天天都有的錯會被學成「平常」、永遠不報，所以另外列出來讓人修。設定在 [chronic.yaml](chronic.yaml)，程式在 [detector/chronic.py](../../detector/chronic.py)。

| 來源 | 看什麼 | 算慢性病 |
|---|---|---|
| `chronic.log_errors` | 錯誤 log（error、fatal），依服務 × `error.type`（沒有的歸成「（無）」） | 最近 7 個完整的日子裡至少 6 天、每天至少 20 筆 |
| `chronic.anomalies` | `aiops-anomalies` 裡的規則、跟兄弟比（N+1、吞掉的例外、CPU 被限流、只有一個 Pod 壞…） | 同一條、同一個對象，7 天裡至少 6 天觸發 |

每個來源排前 10 名（天數多的先，再看量），依 `owners`（match 可用別名）附負責人，寫進 `aiops-chronic`（每週一份），並印成報告。修掉的下週自然從清單消失。

```bash
python3 -m detector chronic --config configs/baseline/chronic.yaml   # 每週一次（例如週一早上）
```

**資料來源**：負責人（`owners`）和行事曆（`calendar`）是要別人提供的資料，程式只認格式，來源可以換（`file`、`http`，行事曆另有 `taiwan`）。**接口**在 [detector/sources.py](../../detector/sources.py) 和 [interfaces/](../../interfaces/README.md)，**假資料**在 [fake-data/](../../fake-data/README.md)，兩邊分開；換成真的只改設定。

**行事曆**：歲修、機房維護、大型活動，寫在 [calendar.yaml](calendar.yaml)，**一次歲修一筆、不合併**（`match` 限定哪個廠、哪些服務）。工廠 24×7，國定假日跟平日一樣，**不列**（列了會把假日的資料從 baseline 挖掉）；只有週休的辦公室系統才打開 `taiwan` provider（[TaiwanCalendar](https://github.com/ruyut/TaiwanCalendar)，含補班日，抓到的存 `.cache/calendar`，失敗用快取），**而且來源要寫 `match`** 限定那些系統：來源的 `match`、`series`、`detect` 套到它的每一天，不寫就是假日那天所有服務都不學、不判斷，24×7 的服務當天真的故障也被吞掉（靜態臨界值規則不看行事曆，照報）。每一筆變成 `exclude_window`：預設不學也不報；`detect: true` 時只是不學、照常判斷（例如活動日流量本來就高）。日期是當地的一整天（`date`），或一段（`start`、`end`，可帶時間）。`adjust list` 會一起列出。

**實測**（2026-10-24，ES 9.5.5）：

| 放進去的 | 結果 |
|---|---|
| `mes-api` 逾時錯誤天天 60 筆 | ✅ 列入（7 天），負責人依別名對到「MES 團隊」 |
| `wip-worker` 7 天裡 6 天、沒有 `error.type` | ✅ 列入 |
| `pay` 只有 2 天、`tiny` 每天 5 筆、今天（還沒過完）的 999 筆 | ✅ 都不算 |
| N+1 規則天天觸發；CPU 限流只觸發一次 | ✅ N+1 列入、限流不列 |
| 行事曆寫前天是假日 | ✅ `train` 排除那天（台北時間一整天）的點 |
| 抓台灣行事曆（2026-10-24 實抓） | ✅ 2026、2027 共 46 筆（開國紀念日、春節、補假…）；週六日不列；斷網用快取；斷網又沒快取只剩假資料，指令不掛 |
| 負責人改走介面（`fake-data/owners.json`） | ✅ `mes-api` 對到「【假】MES 團隊」，沒對到的「未指定」；檔案壞掉只警告、清單照出 |

## 資料品質檢查：資料本身怪不怪

baseline 用中位數＋MAD，只擋得住「少數」怪的點。資料本身壞掉時，學出來的「平常」就是錯的，或根本學不到。這幾種壞法不用學，用規則看得出來。設定在 [quality.yaml](quality.yaml)，程式在 [detector/quality.py](../../detector/quality.py)。每個檢查看最近 1 小時，跟 24 小時前同一段比。

| 檢查 | 看什麼 | 觸發 |
|---|---|---|
| `quality.route_cardinality` | 每個服務有幾條路由（metrics 的 `http.route`）；抽最多 500 個值看像不像帶了 ID（數字、UUID、長 hex） | 超過 500 條；或比昨天多 3 倍且多 50 條以上；或至少 50 條且一半以上像 ID（路由沒參數化：`/orders/123` 沒換成 `/orders/{id}`） |
| `quality.trace_name_cardinality` | 同上，看 trace 入口 span 的名稱（自己用 SDK 埋點、把真實路徑放進名稱時，trace 每一段的 baseline 每個名稱都不到 `min_requests`，整個服務學不到） | 同上 |
| `quality.log_missing_identity` | log 缺 `service.name`、`kubernetes.pod.name`（依 namespace） | 缺 5% 以上（至少 100 筆才看） |
| `quality.metrics_missing_identity` | 應用 metrics 缺 Pod、namespace（依服務） | 缺 5% 以上 |
| `quality.metrics_missing_service` | 應用 metrics 缺 `service.name`（依 namespace：依服務分的話，缺服務名的那組看不出來源） | 缺 5% 以上（至少 20 筆） |
| `quality.log_trace_id_drop` | 帶 `trace.id` 的 log（依服務） | 缺的比例比昨天多 30 個百分點以上（本來就不是每行都有，只看變化） |
| `quality.log_stopped`、`quality.metrics_stopped`、`quality.trace_stopped` | 之前 1 小時有資料、最近 15 分鐘（metrics 5 分鐘）一筆都沒有；trace 只看入口 span | 之前至少 30 筆（metrics 20 筆） |
| `quality.log_unassigned_share`、`quality.metrics_unassigned_share` | 落到 `unassigned` 的比例（namespace 沒標 dept、`dept` 清單漏同步） | 比昨天多 5 個百分點以上 |
| `quality.log_ingest_lag` | 收資料延遲：Collector 讀到（`event.created`）→ 寫進 ES（`event.ingested`），依服務、最近 15 分鐘（用寫進 ES 的時間篩） | p95 ≥ 60 秒 |
| `quality.log_clock_skew` | 應用的時間（`@timestamp`）跟節點（`event.created`）差多少，只看時間是應用自己寫的（用 Collector 讀到的時間篩） | 中位數快或慢 ≥ 5 分鐘（時鐘偏了、時區寫錯） |

觸發的寫進 `aiops-anomalies`：`level` 是 `quality`、`median` 放昨天的值、`keys` 缺欄位時多一個 `field`。

### 門檻怎麼校、誰來調

- 門檻都是先估的。正式環境排程後先只寫入、不通知，跑 2～4 週，看每個檢查觸發幾次、哪些是真的，再由平台的人手動改 [quality.yaml](quality.yaml)。
- `quality` 不讀 `aiops-tuning`：不能用 `tune`、`adjust` 依對象調，回饋建議（`feedback suggest`）也不會動它。
- 只跟昨天比的檢查（`log_trace_id_drop`、`*_unassigned_share` 的增量、`*_cardinality` 的倍數）第一天沒有比較對象，不會報；要第一天就有用的設 `max_share` 這類絕對上限。`*_cardinality` 第一天只看上限 500 條和「一半以上像 ID」；昨天有資料、只是沒有這個服務的（新服務），倍數照樣從 0 算。

```bash
python3 -m detector quality --config configs/baseline/quality.yaml   # 每 10 分鐘
```

**實測**（2026-10-23，ES 9.5.5 跑過 `elasticsearch-setup.sh`；metrics 走 `/_otlp/v1/metrics`，log 直接寫 `k8s-*`）：

| 放進去的壞資料 | 結果 |
|---|---|
| `order` 有 80 條 `/orders/<數字>` 路由 | ✅ 報：從 0 變 80 條；抽樣 100% 像 ID |
| `pay` 3 條樣板路由（對照組） | ✅ 不報 |
| `mes` namespace 三成 log 沒有 `service.name` | ✅ 報 30% |
| `nopod` 的 metrics 沒有 Pod | ✅ 報 100% |
| `legacy` 10 分鐘前起沒有 metrics | ✅ 報「之前 40 筆，最近沒有」 |
| `tr` 昨天九成 log 有 `trace.id`、今天兩成 | ✅ 報「缺的比例從 10% 變 80%」 |
| 落到 unassigned 的 log 佔 13%（沒有昨天的資料） | 不報（沒有比較對象）；加上 `max_share: 0.1` 會報 |

沒驗證：正式環境的量下查詢要多久（`cardinality` 是近似值，3,000 以下準確）。

邊緣情境（新的三個檢查、第一天、failure store、缺 namespace）見 [trace-quality-verify](../../../docs/pdca/phase2-anomaly/2026-10-trace-quality-verify.md)。抓不到的一種：Pod 沒有 `app.kubernetes.io/name` 時，log pipeline 用 container 名稱當服務名，`log_missing_identity` 不算它缺。

## 怎麼跑

```bash
cd engine                           # baseline、根因分析是同一個 Python 專案
pip install -r requirements.txt     # pandas、numpy、pyyaml、requests

export ES_URL=https://<es>:9200
export ES_API_KEY=<elasticsearch-setup.sh 發的「AIOps baseline key」>   # 只能讀資料、寫 aiops-*
# export ES_CA=/path/to/ca.crt      # ES 用內部 CA 的話

python3 -m detector train  --config configs/baseline/series.yaml   # 每天跑一次（例如凌晨）
python3 -m detector detect --config configs/baseline/series.yaml --window 10m   # 每 1 分鐘跑一次，看最近 10 分鐘（重跑會覆蓋）
python3 -m detector tune   --series ... --match ... --resolution 5m   # 調整某些對象的粒度、門檻
python3 -m detector quality --config configs/baseline/quality.yaml  # 每 10 分鐘，資料品質檢查
python3 -m detector chronic --config configs/baseline/chronic.yaml  # 每週一次，慢性病清單
```

正式環境用 K8s CronJob：[engine/deploy/](../../deploy/README.md)（rules／detect／alert 每 1 分鐘、train 每天）。單機的 crontab 例子：

```
15 3 * * *  cd /opt/aiops && python3 -m detector train  --config configs/baseline/series.yaml >> train.log 2>&1
* * * * *   cd /opt/aiops && python3 -m detector detect --config configs/baseline/series.yaml >> detect.log 2>&1
* * * * *   sleep 30; cd /opt/aiops && python3 -m detector detect --config configs/baseline/series.yaml >> detect.log 2>&1
# crontab 最細 1 分鐘：要每 30 秒就用兩行（一行延後 30 秒）；deploy/ 的 CronJob 是每 1 分鐘 --window 10m
```

回測（拿過去某個時間點當「現在」）：`train --end 2026-10-01T00:00:00Z`、`detect --at 2026-10-01T10:00:00Z --window 1D`。

detect 只看**已經結束**的時間窗（再往前退 `lag` 30 秒）：還沒收齊的那一段筆數會偏低，看起來像驟降。重跑同一段時間不會重複寫（異常的 `_id` 由序列、維度、時間決定）。

## 結果在哪裡

| index | 內容 |
|---|---|
| `aiops-baseline` | `series`、`keys`（例如 `keys.service.name`）、`dow`（0 = 週一）、`hour`、`level`、`median`、`mad`、`p05`、`p95`、`n`、`trained_at` |
| `aiops-anomalies` | `@timestamp`、`series`、`keys`、`value`、`median`、`expected_low`／`expected_high`（正常範圍）、`z`（偏離幾倍）、`level`、`direction`、`dept`、`group` |

在 Kibana 建一個 `aiops-anomalies` 的 data view，就能依 `series`、`keys` 看哪個服務、哪個時間點異常。

## 驗證

```bash
python3 -m unittest discover -s tests -t .              # 在 aiops 底下；單元測試，不需要 ES
ES_URL=... ES_AUTH=... python3 -m tests.synth_e2e      # 模擬資料，需要 ES
ES_URL=... ES_AUTH=... python3 -m tests.adaptive_e2e   # 自動選粒度、卜瓦松、借 baseline，需要 ES
ES_URL=... ES_AUTH=... python3 -m tests.fine_e2e       # 30 秒粒度、連續才算、tune，需要 ES
```

`synth_e2e`：寫 4 週＋1 天的模擬資料進 ES（3 個服務，有每日、每週的週期和雜訊），最後一天插入 4 個異常，只用前 4 週訓練、偵測最後一天：

| 插入的異常 | 結果 |
|---|---|
| mes-api 10:00 流量暴增 3 倍（只有一個點） | ✅ 抓到（z = 20.5），**warning**（跳一次） |
| wip-worker 14:30 延遲變 4 倍（只有一個點） | ✅ 抓到（z = 43.8），**warning** |
| eap-gw 凌晨 2:00 出現白天的流量（只有一個點） | ✅ 抓到（z = 53.7），**warning**：同樣的值在白天是正常的 |
| mes-api 16:00 錯誤數暴增（只有一個點） | ✅ 抓到（z = 28.3），**warning** |
| mes-api 18:00 起連續 3 個 5 分鐘錯誤數暴增 | ✅ 18:00 **warning**、18:05、18:10 **anomaly** |
| 正常的點 | **誤報 0 個／2,592 個** |

寫入 2.5 萬筆＋訓練＋偵測共 15 秒。

`fine_e2e`：2 週＋1 天、每 30 秒一筆的模擬資料：

| 情況 | 結果 |
|---|---|
| 忙的 API（每 30 秒約 60 個請求） | ✅ 自動用 30 秒 |
| 少人用的 API（每 30 秒約 1 個請求） | ✅ 自動往粗調（15 分鐘） |
| 最後一天 10:00:00 起延遲連續 90 秒變 3 倍 | ✅ 10:00:00 warning、10:00:30 **anomaly**（約 1 分鐘確認）、10:01:00 anomaly |
| 14:00:00 只尖 30 秒 | ✅ 只有 warning |
| 正常的點 | ✅ anomaly 誤報 0、warning 0／11,520 個點 |
| `tune` 把忙的 API 固定成 1 分鐘 | ✅ 重新 train 後是 1 分鐘；刪掉調整後回到 30 秒 |

訓練 2 週 × 30 秒（約 14 萬筆）5 秒、偵測一天 1 秒。k3s 真實資料（30 多條序列）用正式設定 train 20 秒、detect 2 秒。

`adaptive_e2e`（`ES_URL=... ES_AUTH=... python3 -m tests.adaptive_e2e`）：4 週＋1 天的模擬資料，同一份資料分別用「固定 5 分鐘」和「自己選粒度」跑：

| 情況 | 固定 5 分鐘 | 自己選粒度 |
|---|---|---|
| 忙的 API（每 5 分鐘 100 個請求）10～11 點延遲變 3 倍 | ✅ 抓到 | ✅ 抓到，維持 5 分鐘、可信度 high |
| 少人用的 API（每 5 分鐘約 2 個請求）同一段變慢 | 抓到 3 點、**誤報 2 個**（2 個請求的平均延遲是雜訊） | ✅ 自動改 1 小時，抓到 10 點那一小時，可信度 medium，**誤報 0** |
| 設備告警（平常每小時 0.3 次），EQP-02 14 點那一小時多 8 次 | 碰巧抓到（剛好有 3 次擠在同一個 5 分鐘；分散一點就抓不到） | ✅ 卜瓦松、1 小時一點抓到，其他設備 0 誤報 |
| 最後一天才上線的 new-query（沒有歷史）同一段變慢 | 沒有 baseline，不判斷 | ✅ 借 `query-apis` 組的 baseline 抓到，可信度 low，**誤報 0** |

測試環境的真實資料只有幾個小時，筆數類的序列都還「資料不夠，先不判斷」，CPU、記憶體用 `all` 這一層。這是預期的：歷史不夠時寧可不報。

## 還沒做

1. **告警**：現在只寫進 `aiops-anomalies`。接 Kibana 的告警規則、或推到通訊軟體，要再決定。
2. **合併**：同一個服務的流量、錯誤、延遲同時異常時，現在是好幾筆，之後依服務與時間合併成一件事（[docs/rca/README.md](../../../docs/rca/README.md) 的步驟 2）。
3. **排除已知事件、換版**：程式有了（`adjust`、行事曆，見「調整」），但條目要人寫；測試流量沒有自動辨識（見「[餵給 baseline 之前](#餵給-baseline-之前擋了什麼還缺什麼)」）。
4. **指標、借同組**：p99、慢請求比例、往外呼叫耗時還沒有；`groups` 沒定義（見「[定義的原則與限制](#定義的原則與限制)」）。
5. **不是 Java、.NET 的服務**：流量、錯誤率、延遲來自 `http.server.request.duration`（Java agent、.NET 自動埋點都有，見 [docs/metrics/dotnet.md](../../../docs/metrics/dotnet.md)）。其他語言要用 OpenTelemetry 開出同名的直方圖，才學得到。
6. **更準的方法**：用一陣子、收集到誤報和漏報後，再考慮方法 B（拆週期）、C（預測模型）。
