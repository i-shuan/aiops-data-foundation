# baseline 規格：序列、狀態、參數、規則、欄位

> **類型：規格 Reference**　同主題：[原理](../../../../docs/rca/README.md)・[操作](README.md)・[實測](verification.md)

要查「這個參數是什麼、這條規則看什麼、欄位長怎樣」看這份。為什麼這樣設計見 [rca/README.md](../../../../docs/rca/README.md)，怎麼跑、怎麼調見 [README.md](README.md)，實測結果見 [verification.md](verification.md)。

**目錄**

<!-- toc -->
- [做什麼](#做什麼)
- [學出來長什麼樣子](#學出來長什麼樣子)
  - [什麼時候開始、多久訓練一次](#什麼時候開始多久訓練一次)
  - [跳一次是 warning，連續才是 anomaly](#跳一次是-warning連續才是-anomaly)
  - [定義的原則與限制](#定義的原則與限制)
- [自動調整：粒度、低頻事件、借用 baseline](#自動調整粒度低頻事件借用-baseline)
  - [(1) 每條序列自己選粒度](#1-每條序列自己選粒度)
  - [使用者、feedback 調整](#使用者feedback-調整)
  - [(3) 自己沒有 baseline：借同類的](#3-自己沒有-baseline借同類的)
  - [異常帶的欄位](#異常帶的欄位)
- [trace 每一段的 baseline](#trace-每一段的-baseline)
- [規則：狀態變化、離極限多遠](#規則狀態變化離極限多遠)
- [Pod 同儕橫向比對：同一個服務的 Pod 互相比](#pod-同儕橫向比對同一個服務的-pod-互相比)
- [餵給 baseline 之前：擋了什麼、還缺什麼](#餵給-baseline-之前擋了什麼還缺什麼)
- [調整：哪些資料不學、從哪裡重新學、哪些時段放寬](#調整哪些資料不學從哪裡重新學哪些時段放寬)
- [事故紀錄與值班回饋](#事故紀錄與值班回饋)
- [慢性病清單、行事曆](#慢性病清單行事曆)
- [資料品質檢查：資料本身怪不怪](#資料品質檢查資料本身怪不怪)
- [結果在哪裡](#結果在哪裡)
<!-- /toc -->

## 做什麼

> 另見：[原理](../../../../docs/rca/README.md)・[操作](reference.md#做什麼)

學哪些序列寫在 [series.yaml](series.yaml)，目前是每個服務的核心指標：

| 序列 | 來源 | 依什麼切 | 什麼算異常 |
|---|---|---|---|
| `http.requests` | 應用的 metrics | 服務 × method × API | 請求數變多或變少 |
| `http.error_rate` | 應用的 metrics | 服務 × method × API | 5xx 的比例變高（至少多 5 個百分點） |
| `http.latency_p95_s` | 應用的 metrics | 服務 × method × API | 延遲 p95 變高（至少慢 50 毫秒） |
| `http.latency_p99_s` | 應用的 metrics | 服務 × method × API | 尾端：p99 變高（至少慢 100 毫秒）；p95 看不到的少數很慢的請求 |
| `http.slow_ratio` | 應用的 metrics | 服務 × method × API | 超過 2 秒的請求佔幾成變高（至少多 2 個百分點、至少 3 個）；只有一部分請求變慢時 p95 不動、這條會動 |
| `http.client_p95_s` | 應用的 metrics（`http.client.request.duration`） | 服務 × 對方位址（`server.address`） | 往外呼叫（下游 API、外部系統）的 p95 變高 |
| `log.lines` | log | 服務 | log 筆數變多或變少 |
| `log.errors` | log | 服務 | error／fatal 的 log 變多 |
| `pod.cpu`、`pod.memory_bytes` | K8s metrics | namespace × Deployment | 用量變高 |

## 學出來長什麼樣子

> 另見：[原理](../../../../docs/rca/README.md)

### 什麼時候開始、多久訓練一次

| | 時間 |
|---|---|
| 每條序列多細 | 預設 30 秒一點（跟收值一樣），量不夠的自己往粗調到 1 分鐘～1 小時（見「自動調整」(1)）；每條序列切成 168 個時段（星期幾 × 小時）各自學 |
| 最快多久開始判斷 | 50 個點（`all` 層）：30 秒一點約 25 分鐘、5 分鐘一點約 4 小時；3 天後有「每天同一小時」；2 週後有「同星期幾 × 同小時」 |
| 訓練（`train`） | 建議每天一次；5 分鐘以上的粒度用最近 4 週，30 秒、1 分鐘用最近 14 天（OTel 版跟 metrics 原始資料留 14 天一致；Elastic APM 版是 1 分鐘一點，見 [../apm/series.yaml](../apm/series.yaml)） |
| 偵測（`detect`） | 建議每 30 秒一次，看最近 30 分鐘、只看已經結束（再退 30 秒確定收齊）的時間窗 |
| 現況 | ⚠️ 正式環境還沒排程，目前是測試環境手動跑（排程範例見「怎麼跑」） |

### 跳一次是 warning，連續才是 anomaly

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
- 設定在 `series.yaml` 的 `status: {confirm_points: 2, confirm_span: 30m, instant_z: 15}`。每條序列可以自己設 `confirm_points`、`instant_z`（`http.latency_p95_s` 設 `null`：不直接 anomaly；寫 0 會讓每個超標點都直接 anomaly）；`tune --confirm-points 1 --instant-z 10` 可以再依對象調（寫進 `aiops-tuning`，之後回饋的建議也能調）。優先順序：對象 > 序列 > 全域（[pdca/phase2-anomaly/2026-10-instant-anomaly.md](../../../../docs/pdca/phase2-anomaly/2026-10-instant-anomaly.md)）。
- 規則（`rules.yaml`）另外用 `for`（要持續多久）處理；重啟、Pod Failed 這類 critical 規則一次就算，不等。

### 定義的原則與限制

| 限制 | 原因 | 影響 | 現況 |
|---|---|---|---|
| 訓練資料有壞資料（長期事故、壓測、維護） | 天天發生、或連續一兩週的壞資料會變成中位數本身 | 平常被學歪 | `exclude_window`、行事曆、慢性病清單（見「[餵給 baseline 之前](#餵給-baseline-之前擋了什麼還缺什麼)」） |
| 換版後一階跳變、停在新位置 | 新值要佔歷史一半（約 2 週）中位數才跟上 | 這段期間一直報 | `reset_point` |
| 比一週長的週期（月底、季底批次） | 168 格只涵蓋一週 | 月底誤報 | `recurring_override` |
| 指標不夠：沒有 p99、慢請求比例、往外呼叫耗時 | `series.yaml` 只有 7 條序列 | 只有 p95，看不到尾端 | 待排：加序列（改設定，不改公式） |
| 借同組實際不會發生 | `series.yaml` 的 `groups` 是註解掉的範例，沒有 `groups` 就不借 | 新 API 沒有 baseline 可比 | 要依公司服務命名定義 `groups`（要別人配合） |
| p95 從直方圖算，值落在桶中點 | MAD＝0，σ 只剩中位數的 5%；多兩個請求跨到下一格 z 約 23 | 已設 `instant_z: null`（跨一格只算 warning、連續 2 窗才 anomaly）：合成資料一週 p95 anomaly 3.52% → 0.93%，但 warning 2.67% → 5.26%，超標的點數沒變 | 跨一格 z 仍大；待改程式（σ 至少半個桶寬），[threshold-tune](../../../../docs/pdca/phase2-anomaly/2026-10-baseline-threshold-tune.md) |
| 錯誤率一個 5xx 就到門檻 | `min_events` 20、`min_abs` 5%、`confirm_points` 1 | 合成資料一週 0.34% 的點報 anomaly（`min_events.rate` 100 試過：降到 0，但錯誤率、p95 變 5 分鐘一點，已退回） | 待改程式：至少 N 個 5xx 才判斷，[threshold-tune](../../../../docs/pdca/phase2-anomaly/2026-10-baseline-threshold-tune.md) |
| `hour` 層混平日與週末 | 每天同一小時不分星期幾 | 某個週一收集中斷後兩週，週一那幾小時流量掉九成只有 z −4；剛上線 3 天週一會 warning | 待評估：hour 層分平日／週末 |
| 尖峰不對齊整點 | 168 格以整點切；08:45 起的尖峰只佔 8 點那格的四分之一 | 每個平日 08:45～09:00 報 anomaly | `recurring_override`；或待評估邊界比相鄰時段 |

## 自動調整：粒度、低頻事件、借用 baseline

> 另見：[原理](../../../../docs/rca/README.md)

> 狀態：(1)(2)(3) 都做好了，`adaptive_e2e` 驗證過（見「驗證」）。設定在 `series.yaml` 的 `adaptive`、`groups`、`borrowing`。

### (1) 每條序列自己選粒度

- **預設 30 秒一點**（跟收值頻率一樣），量不夠才往粗調：**看底下的事件數，不是看有幾個點**——p95 由 3 個請求算出來就是雜訊。每條序列算「每個時間窗的事件數」，從 30s、1m、5m、15m、30m、1h 裡挑**最細、而且至少三成（`coverage: 0.3`）的時間窗夠多**的。不用中位數（一半）：很多 API 只有白天有人用，晚上、週末的時間窗本來就少，用中位數會整條不判斷。

  | 指標類型 | 事件數是什麼 | 預設門檻（每窗） |
  |---|---|---|
  | 比例（錯誤率）、百分位（p95） | 這段時間的請求數 | ≥ 20 |
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

| 從哪裡調 | 怎麼調 | 什麼時候生效 |
|---|---|---|
| 使用者設定 | `series.yaml` 的 `overrides`（`series` 可用正規表示式、`match` 依 keys 比對） | 粒度：下次 train；z、min_abs：下次 detect |
| 指令 | `python3 -m detector tune --series http.latency_p95_s --match service=^mes-report --resolution 5m --z 7 --reason 誤報多 --by 王小明`，寫進 `aiops-tuning`（`--list` 列出、`--remove` 刪掉） | 同上 |
| 查生效值 | `python3 -m detector tune --show --series http.latency_p95_s --match service=mes-report-api`（`--match` 給對象的實際值，不是正規表示式）：印 `resolution`、`z`、`min_abs`、`confirm_points`、`instant_z` 各自現在的值與來源（`aiops-tuning` 的 id、`series.yaml overrides#<第幾筆>`、或 `series.yaml 預設`）。值跟 detect 用的同一個函式算，不會兩套 | 不寫入 |
| feedback（📝） | 之後的 feedback 流程寫同一個 `aiops-tuning`：同一條序列誤報累積多次 → 建議粒度往粗一級或調高 z，回測通過才寫入（見 [docs/feedback/README.md「對了怎麼強化、錯了怎麼 feedback」](../../../../docs/feedback/README.md)） | 同上 |

套用順序：自動選 → `series.yaml` 的 `overrides`（檔案順序）→ `adjustments.yaml` → `aiops-tuning`（依 `at` 由舊到新），同一欄位後面的蓋前面的，不同欄位各自生效；不看 `match` 寫得多細（較晚寫的寬鬆調整會蓋掉較早的精細調整）。每筆調整記 `reason`、`by`、`at`，查得到誰、為什麼、什麼時候改的。

#### 調整和原始資料分開存

調整只在 `train`、`detect` 執行當下疊上去，不改原始資料，也不改學出來的基準線。

| 東西 | 放哪 | 誰寫 |
|---|---|---|
| 原始資料（每個時間點的值） | `metrics-*.otel-*`、`k8s-*`、`traces-*.otel-*` | Collector |
| 基準線（學出來的中位數與 MAD） | `aiops-baseline` | `train`；寫完會刪掉這條序列上一次留下、這次沒有的 |
| 預設參數（`z`、`confirm_points`…） | `series.yaml` | 改檔 |
| 調整 | `aiops-tuning`；另有 `series.yaml` 的 `overrides`、`adjustments.yaml` | `tune`、`adjust`、回饋網頁 |

- `override`（`z`、`min_abs`、`confirm_points`、`instant_z`）只改判斷用的門檻，下次 `detect` 就用，不動基準線。
- `exclude_window`、`reset_point` 作用在 `train`：先把那段歷史拿掉再算中位數與 MAD，所以要等下次 `train` 基準線才變；要馬上生效手動跑一次 `train`。被排除的原始資料還在 ES。
- 同一條序列加同一組 `match` 用 `tune` 再寫一次是覆蓋，不是新增一筆。
- `aiops-tuning` 裡的調整不會自動刪：`expires_at` 過了只是不再生效，文件還在；`adjust remove --id` 才刪。

### (3) 自己沒有 baseline：借同類的

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
| `dept`、`group` | 這筆異常屬於哪個部門、哪個 group（告警依它分送、人只看自己部門的）。寫入時依 keys 裡的 namespace（優先）或服務，對回資料裡的 `resource.attributes.dept`、`.group`；對不到的 `dept` 空、`group` 是 `unassigned`（跟 Collector 對沒有 dept 的處理一樣）。程式 `core/owner.py`（查歸屬）、`sources/base.py` 的 `Backend.write_anomalies`（寫入時補） |
| `confidence` | dow_hour → high；hour → medium；all、借來的 → low |
| `engine_version` | 哪一版 engine 寫的：chart 把 image tag 放進環境變數 `ENGINE_VERSION`（`core/version.py`），本機跑是 `dev`。規則、peer、quality、trace 的異常也有（都從 `detector/owner.py` 寫） |

卜瓦松的異常另外記 `p`（機率）。根因報告的異常表多一欄「可信度」（例如 `low、1h、借 query-apis`），有 low 時加註「用的是不分時段、或借同類 API 的 baseline（不是自己的歷史），先確認是不是真的變了」。

## trace 每一段的 baseline

> 另見：[原理](../../../../docs/rca/README.md)・[操作](reference.md#trace-每一段的-baseline)・[實測](verification.md#trace-每一段的-baseline)

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

**訊息消費者（Kafka）：** 入口除了 HTTP 請求（Server），也算訊息消費者（Consumer）和「沒有上一層的 span」。消費者多一段「`服務 › 排隊（topic）← 生產者`」＝拿到訊息的時間 − 生產者送出的時間（生產者是上一層，或用 link 找回來）。.NET 自動埋點的消費者處理時查的 DB 自成一個 trace，當成自己的入口算，所以看得到「這個消費者寫 DB 變慢、出錯」。

吞掉的例外要靠 span 上有例外的種類（屬性 `exception.type`）：OTel 版是 Collector 從例外事件抄到 span 屬性（Collector 已從這個 repo 移除）；Elastic APM 版的例外是 `logs-apm.error-*` 的獨立文件，還沒接回 span，這條不會觸發。兩種樣子都算吞掉：這一段有例外、status 沒設；或這一段有例外又標了錯誤，但這個服務的入口沒標錯誤（Java agent 的 JDBC、HTTP client 丟例外一定標錯誤，程式 catch 掉、回 200 就是這樣）。錯誤傳到入口（回 500）的算錯誤，不算吞掉。

往外呼叫的對象依序取 `peer.service`、`db.system`、`db.system.name`（新版 DB 語意）、`server.address`；都沒有的 Client span 段名沒有「→ 對象」，`repeated`（N+1）規則看不到。

學和判斷的方法跟上面一樣（中位數＋MAD、z 5 倍、`min_abs`），設定在 [trace.yaml](trace.yaml)。

剛上線時只有 7 天的統計，學出來是 `hour` 或 `all` 層級；累積到 2 週以上才有「同星期幾 × 同小時」。

## 規則：狀態變化、離極限多遠

> 另見：[原理](../../../../docs/rca/README.md)・[操作](reference.md#規則狀態變化離極限多遠)・[實測](verification.md#規則狀態變化離極限多遠)

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
| `container.memory_leak` | 最近 30 分鐘記憶體的趨勢（`slope`） | 一路往上（三段低點一段比一段高）、照這個速度 ≤ 6 小時到上限；要有滿 30 分鐘的資料 | warning |
| `node.memory_high` | 節點記憶體 ÷ 總量 | ≥ 90%，連續 5 分鐘 | warning |
| `node.disk_high`、`node.disk_full` | 節點磁碟用量 ÷ 容量 | ≥ 85%（5 分鐘）／≥ 95%（1 分鐘） | warning／critical |
| `container.cpu_throttled` | 被限流的 CPU 週期 ÷ 全部週期（cAdvisor） | ≥ 25%，5 分鐘 | warning |
| `jvm.old_gen_after_gc_high` | 老年代 GC 後用量 ÷ 上限 | ≥ 80%，2 分鐘 | warning |
| `jvm.major_gc_frequent` | major GC 次數 | 5 分鐘內 ≥ 5 次 | warning |
| `jvm.gc_overhead` | GC 花的時間 ÷ 經過的時間 | ≥ 10%，3 分鐘（heap 大的服務） | warning |
| `db.pool_exhausted`、`db.pool_waiting` | 連線池使用中 ÷ 上限；排隊中的請求 | ≥ 90%，1 分鐘；≥ 1，1 分鐘 | critical／warning |
| `kafka.consumer_lag`、`kafka.consumer_lag_growing` | 消費落後筆數；5 分鐘淨增加多少（`delta`） | ≥ 10,000，3 分鐘；≥ 5,000 | warning |
| `eap.secs_disconnected`、`eap.plc_scan_slow`、`eap.secs_retransmits` | 機台 SECS 通訊、PLC 掃描週期、重傳（依 `equipment_id`） | 斷線 1 分鐘；≥ 規格 50ms，2 分鐘；5 分鐘重傳 ≥ 10 次 | critical／warning |

寫法：`value`（欄位跟門檻比）、`compare`（兩個欄位相減）、`ratio`（欄位 ÷ 分母）、`increase`（累計值增加多少，歸零重來也算得對；只能用在計數器）、`delta`（量測值淨增加多少，最後 − 最前；例如 Kafka 落後）、`increase_ratio`（兩個累計值增加量的比例）、`rate`（加總 ÷ 秒數）、`total`（加總；`hist_count` 數直方圖的次數）、`slope`（最近 `for` 這段的 Theil–Sen 斜率；有 `den` 就換成「幾小時後到上限」，沒有就是每小時漲多少；三段的低點要一路墊高，GC 鋸齒不算）。分母在另一種文件時用 `join` 補進來。`for` 是「這段時間都成立才算」（對象要在 for 開頭之前就有資料：新建的 Pod、Deployment 等滿 for 才報），`min_ratio` 放寬成「這段時間裡至少多少比例成立」。對象不在了（Pod 刪掉、節點拿掉，最新的資料超過 2 分鐘）就不報；只有容器重啟、Job 失敗設了 `report_gone: true`，刪掉了也照報。要加規則，在 rules.yaml 加一段，不用改程式。一條規則查詢失敗（或核准的草稿缺欄位）只跳過那一條、其他照跑，指令最後 exit 1。核准的規則草稿（`aiops-tuning` 的 `rule_suggestion`）`expires_at` 到了就不載入。邊緣案例見 `tests/rules_edge_e2e.py`。

觸發的寫進 `aiops-anomalies`：`series` 是 `rule.<名稱>`、`level` 是 `rule`，另外有 `severity`、`message`（例如「容器重啟：rules-test、crash-app-…、app（1，門檻 >= 1）」）、`since`（從什麼時候開始成立）。同一條規則、同一個對象，1 分鐘只記一筆。

**還沒加：** JVM 老年代的趨勢（容器 working set 的有了；老年代要 prometheus 的 JVM 指標，欄位名依 agent 版本不同）、網卡丟包（要 node_exporter）、資料庫自己的指標（長交易、慢查詢，要 DB 的 exporter）。

## Pod 同儕橫向比對：同一個服務的 Pod 互相比

> 另見：[原理](../../../../docs/rca/README.md)・[操作](reference.md#pod-同儕橫向比對同一個服務的-pod-互相比)・[實測](verification.md#pod-同儕橫向比對同一個服務的-pod-互相比)

| 檢查 | 每個 Pod 算什麼（最近 10 分鐘） | 分組 | 觸發（跟其他 Pod 的中位數比） |
|---|---|---|---|
| `peer.http_error_rate` | 錯誤率（5xx ÷ 全部） | namespace × 服務 | 高 5 倍穩健標準差，而且多 5 個百分點以上 |
| `peer.http_latency_p95_s` | p95 延遲 | namespace × 服務 | 同上，而且慢 100ms 以上、是別人的 2 倍以上 |
| `peer.pod_cpu` | CPU 平均 | namespace × Deployment | 同上，而且多 0.2 核以上、是別人的 3 倍以上 |
| `peer.pod_memory_bytes` | 記憶體（working set）最高 | namespace × Deployment | 同上，而且多 100MB 以上、是別人的 1.5 倍以上 |

怎麼比：每個 Pod 跟「**其他** Pod」的中位數、MAD 比（跟 baseline 一樣的穩健統計；比的時候不含自己，壞掉的那個不會把標準拉過去）。z 的門檻依其他 Pod 數分級（`z_by_peers`）：其他 2 個用 3、3～4 個用 4、5 個以上 5——其他只有兩三個時它們之間的差距沒統計意義（滾動更新後新舊 Pod 的記憶體本來就差幾十 MB），主要靠 `min_abs`、`min_ratio` 擋。另外：
- **至少 3 個 Pod 才比**：只有 2 個分不出誰才是怪的。
- **只比現在還在的 Pod**（最近 2 分鐘有資料）：換版、縮容刪掉的不算兄弟。換版途中新舊 Pod 會一起比，新版的 Pod 都比舊的差也會報，這正好是想知道的。
- **剛起來的 Pod 不比**（`warmup` 5 分鐘：window 開頭到 5 分鐘前之間沒有資料的不比）：啟動時 CPU 高、記憶體還在長，會誤報新 Pod 自己，或在擴容時讓舊 Pod 的記憶體看起來偏高。一起來就壞的 Pod 5 分鐘後照樣抓。
- **流量太少的 Pod 不比錯誤率、延遲**（10 分鐘不到 20 個請求）：一兩個錯就跳很高。
- **全部一起壞不報**：例如下游 DB 掛了，每個 Pod 都錯，那不是「某個 Pod」的問題，交給 baseline 和規則。其他 Pod 的中位數已經到規則門檻（錯誤率 5%、p95 2 秒，`max_peer_median`）時，最嚴重的那個也不單獨挑出來。
- 門檻比 baseline 寬（`rel_floor` 10%，再加上至少差多少、至少幾倍）：同一個服務的 Pod 本來就不會一模一樣。

**誰跟誰是同儕**：不是 AIOps 判斷，是照資料裡的欄位分組（`namespace`、`service.name`、Deployment、`pod`）。OTel 版這些欄位是 Collector 依連線來源 IP 對到 Pod 後補上的；Elastic APM 版用 `service.name`、`service.node.name`（見 [../apm/peer.yaml](../apm/peer.yaml)）。

**限制：**
- 標籤錯了就分錯組：兩個不同的服務用了同一個 `service.name`，會被當成同儕互相比。
- 同組少於 3 個 Pod（單 Pod 的服務、剛縮容到 1～2 個）完全不比，這類服務沒有這層保護，單一 Pod 的問題只能靠 baseline 和規則抓。
- 換版途中新舊版本的 Pod 同組：新版整體比舊版差時，會把新版的每個 Pod 都報出來。

觸發的寫進 `aiops-anomalies`：`series` 是檢查名稱（`peer.…`）、`level` 是 `peer`、`median` 是其他 Pod 的中位數，`message` 例如「這個 Pod 的錯誤率比同服務其他 Pod 高：metrics-cases、dotnet-app、dotnet-app-…（100%，其他 2 個 Pod 中位數 0%，z=…）」。

## 餵給 baseline 之前：擋了什麼、還缺什麼

> 另見：[原理](../../../../docs/rca/README.md)

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

- **都沒排程**：`quality`、`chronic` 是排程表的第二階段（`configs/schedule/schedule.yaml` 的 `stage: 2`，預設不開），`adjust`、行事曆在 `train` 時生效但條目要人寫。
- **測試流量沒有自動辨識**：壓測要人手動寫 `exclude_window`；沒有依 label（loadgen、測試 namespace）自動不學。
- **默默掉資料看不到**：ES `_otlp` 入口回 200 卻整批拒掉時（權限不夠、資料流名稱錯），Collector 沒 log、沒指標；要有 Collector 送出筆數跟 ES 實際寫入筆數的對照（可放進 `quality` 的 `stopped` 類）。
- **慢性病清單沒送到負責人手上**；`owners` 目前是假資料（`fake-data/owners.json`）。
- 資料品質檢查的門檻是估的（見「[門檻怎麼校、誰來調](verification.md#門檻怎麼校誰來調)」）。

## 調整：哪些資料不學、從哪裡重新學、哪些時段放寬

> 另見：[原理](../../../../docs/rca/README.md)・[操作](reference.md#調整哪些資料不學從哪裡重新學哪些時段放寬)・[實測](verification.md#調整哪些資料不學從哪裡重新學哪些時段放寬)

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

## 事故紀錄與值班回饋

> 另見：[原理](../../../../docs/rca/README.md)・[實測](verification.md#事故紀錄與值班回饋)

| 指令 | 做什麼 |
|---|---|
| `feedback pending` | 最近還沒人標記的告警（只列 `anomaly`；`warning` 還沒通知任何人）和它們的 id |
| `feedback add --anomaly <id> --verdict …` | `true_positive` 真的、`false_positive` 誤報、`wrong_root_cause` 真的但根因錯（`--root-cause` 寫正確的）、`missed` 漏報（沒有 id，給 `--series --match --at`）。同一個人對同一筆再標會覆蓋。`--text "一句話"`（可加 `--alert <alert_id>` 帶入服務、序列）：LLM 抽成四欄（`extract.feedback_type`／`identified_cause`／`target_service`／`suggested_action`）存在回饋上，沒給 `--verdict` 就用抽出來的；`suggested_action` 對得到調整種類的寫一筆 `aiops-tuning`（下表）；抽不到只存原文 |
| `feedback summary` | 每條序列的告警數、已標記比例、誤報率；誤報累積多次的對象；漏報 |
| `feedback suggest` | (1) 同一個對象誤報 ≥ 3 次 → 依這些誤報選一種放寬：八成以上在同一個時段（每月最後一天的同一小時、或同一個星期幾的同一小時）→ 週期放寬（`recurring_override`，只有那 1 小時 z × 1.4）；每一筆的 z 都超過 z × 1.4（放寬 z 也壓不掉）→ `min_abs` 調到最大偏差的 1.2 倍；其他 → z × 1.4；(2) 漏報 → 那條序列、那個對象降 z（× 0.8）；(3) 同一個服務、同一個根因分類被老師傅確認 ≥ 3 次（`--min-recurring`）→ 提「升級成規則」：**門檻算好**（那幾件事故的延遲峰值取最小值，三件都抓得到）、查法跟那條 baseline 序列一樣、只看這個服務（三件同一支 API 就只看它）；`adjust approve` 後 **`rules` 自動載入**，不用改 `rules.yaml`；算不出門檻的不能核准。都是 `status: proposed`；要先 `backtest` 再 `adjust approve` |
| `backtest --id <建議> --weeks 4` | 改門檻前先回測：用「現行」和「現行＋這筆建議」各重跑過去 N 週的 detect（不寫 ES），比告警少幾個、已確認的事故（`aiops-incidents` confirmed／corrected／resolved）還抓不抓得到、漏報抓到沒；少抓一件已確認的事故就「不建議」。`rule_suggestion` 另算：過去 N 週每 5 分鐘（規則的 `lookback`）一點算規則的值，提這條規則的事故都要抓到、事故以外平均每週多報不超過 1 次（估的）才「建議」。結果寫回建議的 `backtest` 欄位，`adjust list` 看得到 |
| `adjust approve --id … --by …` | override、`recurring_override`、`rule_suggestion` **沒回測、或回測不建議的不能核准**（要跳過用 `--force`）；記 `approved_by`、`approved_at` |
| `metrics --from … --to … [--compare-from … --compare-to …]` | 量化：件數（每天、依 dept）、有回饋的比例、誤報率、根因錯的比例、漏報、從告警到第一筆回饋的中位時間、老師傅確認的事故數、「怎麼看出來的」有填的比例；兩段並排就是「學之前 vs 學之後」。只印 Markdown（`--out` 存檔），畫圖交給 Grafana |
| `python3 -m api` | 老師傅的回饋網頁（通知裡的連結）：一鍵真的／誤報／根因錯，多填根因分類、根因、處置、**怎麼看出來的**、查了哪裡、下次怎麼更快、兩題評分、**一句話**（LLM 抽成四欄，同 `feedback add --text`）；寫 `aiops-feedback`（一件的每筆異常各一筆）和 `aiops-incidents`（`confirmed`／`corrected`／`false_positive`）。Grafana 裡的表單欄位對照見 [configs/grafana/README.md](../../../../grafana/README.md#回饋表單預設用-engine-api-的網頁要在-grafana-裡直接填才裝外掛) |

一句話抽出來的 `suggested_action` 對到的調整（都 `status: proposed`、`source: feedback`、`reason` 帶原文；`adjust approve` 才生效）：

| `suggested_action` | 寫進 `aiops-tuning` 的 kind | 內容 | 核准前要做什麼 |
|---|---|---|---|
| `suppress_during_cron` | `recurring_override` | `match.service` 是抽出來的服務、`effect.mute`；**沒有 `schedule`**（文字通常沒寫 cron、時長，不猜） | `adjust list` 會標「cron 要 5 欄」略過：補 `schedule`（`cron`、`duration`、必要時 `days_of_month`）再回測、核准 |
| `widen_threshold` | `override` | 這件告警的序列、抽出來的服務，z × 1.4（跟誤報 3 次的建議一樣） | `backtest --id` 再 `adjust approve` |
| `add_rule` | `rule_suggestion` | `rule_draft.threshold` 是 None（一句話算不出門檻） | 看報告決定序列與門檻，寫進 `rules.yaml` 或改這筆；threshold 不是數字不能核准 |
| `none` | 不寫 | — | — |
| `incident add --title --start --end --service …` | 事故紀錄：時間、影響的服務、根因、處置、相關異常。`--exclude-training` 同時提「這段不學」（`exclude_window`，proposed，只排除列出的服務；沒列就全部） |
| `incident list`／`incident resolve --id` | 列出／結案（補根因、處置） |

寫進 `aiops-feedback`、`aiops-incidents`；建議寫進 `aiops-tuning`（跟 `tune`、`adjust` 同一處）。

**還沒做**：評測集（要真實事故）；`suggest` 不提粒度（改粒度要重新 train，回測比不了）；選放寬方式的比例（八成）、倍數（1.2）是估的；回饋表單的一句話抽成 `recurring_override` 時沒有 `schedule`，要人補。

## 慢性病清單、行事曆

> 另見：[原理](../../../../docs/rca/README.md)・[操作](reference.md#慢性病清單行事曆)・[實測](verification.md#慢性病清單行事曆)

| 來源 | 看什麼 | 算慢性病 |
|---|---|---|
| `chronic.log_errors` | 錯誤 log（error、fatal），依服務 × `error.type`（沒有的歸成「（無）」） | 最近 7 個完整的日子裡至少 6 天、每天至少 20 筆 |
| `chronic.anomalies` | `aiops-anomalies` 裡的規則、Pod 同儕橫向比對（N+1、吞掉的例外、CPU 被限流、只有一個 Pod 壞…） | 同一條、同一個對象，7 天裡至少 6 天觸發 |

每個來源排前 10 名（天數多的先，再看量），依 `owners`（match 可用別名）附負責人，寫進 `aiops-chronic`（每週一份），並印成報告。修掉的下週自然從清單消失。

**資料來源**：負責人（`owners`）和行事曆（`calendar`）是要別人提供的資料，程式只認格式，來源可以換（`file`、`http`，行事曆另有 `taiwan`）。**接口**在 [detector/sources.py](../../detector/sources.py) 和 [interfaces/](../../interfaces/README.md)，**假資料**在 [fake-data/](../../fake-data/README.md)，兩邊分開；換成真的只改設定。

**行事曆**：歲修、機房維護、大型活動，寫在 [calendar.yaml](calendar.yaml)，**一次歲修一筆、不合併**（`match` 限定哪個廠、哪些服務）。工廠 24×7，國定假日跟平日一樣，**不列**（列了會把假日的資料從 baseline 挖掉）；只有週休的辦公室系統才打開 `taiwan` provider（[TaiwanCalendar](https://github.com/ruyut/TaiwanCalendar)，含補班日，抓到的存 `.cache/calendar`，失敗用快取），**而且來源要寫 `match`** 限定那些系統：來源的 `match`、`series`、`detect` 套到它的每一天，不寫就是假日那天所有服務都不學、不判斷，24×7 的服務當天真的故障也被吞掉（靜態臨界值規則不看行事曆，照報）。每一筆變成 `exclude_window`：預設不學也不報；`detect: true` 時只是不學、照常判斷（例如活動日流量本來就高）。日期是當地的一整天（`date`），或一段（`start`、`end`，可帶時間）。`adjust list` 會一起列出。

## 資料品質檢查：資料本身怪不怪

> 另見：[原理](../../../../docs/rca/README.md)

| 檢查 | 看什麼 | 觸發 |
|---|---|---|
| `quality.route_cardinality` | 每個服務有幾條路由（metrics 的 `http.route`）；抽最多 500 個值看像不像帶了 ID（數字、UUID、長 hex） | 超過 500 條；或比昨天多 3 倍且多 50 條以上；或至少 50 條且一半以上像 ID（路由沒參數化：`/orders/123` 沒換成 `/orders/{id}`） |
| `quality.trace_name_cardinality` | 同上，看 trace 入口 span 的名稱（自己用 SDK 埋點、把真實路徑放進名稱時，trace 每一段的 baseline 每個名稱都不到 `min_requests`，整個服務學不到） | 同上 |
| `quality.log_missing_identity` | log 缺 `service.name`、`kubernetes.pod.name`（依 namespace）；`service.name` 等於 container 名稱也算缺 | 缺 5% 以上（至少 100 筆才看） |
| `quality.metrics_missing_identity` | 應用 metrics 缺 Pod、namespace（依服務） | 缺 5% 以上 |
| `quality.metrics_missing_service` | 應用 metrics 缺 `service.name`（依 namespace：依服務分的話，缺服務名的那組看不出來源）；`service.name` 等於 container 名稱也算缺 | 缺 5% 以上（至少 20 筆） |
| `quality.log_trace_id_drop` | 帶 `trace.id` 的 log（依服務） | 缺的比例比昨天多 30 個百分點以上（本來就不是每行都有，只看變化） |
| `quality.log_stopped`、`quality.metrics_stopped`、`quality.trace_stopped` | 之前 1 小時有資料、最近 15 分鐘（metrics 5 分鐘）一筆都沒有；trace 只看入口 span | 之前至少 30 筆（metrics 20 筆） |
| `quality.log_unassigned_share`、`quality.metrics_unassigned_share` | 落到 `unassigned` 的比例（namespace 沒標 dept、`dept` 清單漏同步） | 比昨天多 5 個百分點以上 |
| `quality.log_ingest_lag` | 收資料延遲：Collector 讀到（`event.created`）→ 寫進 ES（`event.ingested`），依服務、最近 15 分鐘（用寫進 ES 的時間篩） | p95 ≥ 60 秒 |
| `quality.log_clock_skew` | 應用的時間（`@timestamp`）跟節點（`event.created`）差多少，只看時間是應用自己寫的（用 Collector 讀到的時間篩） | 中位數快或慢 ≥ 5 分鐘（時鐘偏了、時區寫錯） |

`missing` 檢查的 `missing_if_equals: {欄位: 另一個欄位}`：欄位等於另一個欄位也算缺。Pod 沒有 service label 時 Collector 把 `service.name` 補成 container 名稱（例如 `app`），欄位有值但對不到服務，只看 `exists` 看不出來。

觸發的寫進 `aiops-anomalies`：`level` 是 `quality`、`median` 放昨天的值、`keys` 缺欄位時多一個 `field`。

## 結果在哪裡

| index | 內容 |
|---|---|
| `aiops-baseline` | `series`、`keys`（例如 `keys.service.name`）、`dow`（0 = 週一）、`hour`、`level`、`median`、`mad`、`p05`、`p95`、`n`、`trained_at` |
| `aiops-anomalies` | `@timestamp`、`series`、`keys`、`value`、`median`、`expected_low`／`expected_high`（正常範圍）、`z`（偏離幾倍）、`level`、`direction`、`dept`、`group`、`alert_id`（併進哪一件；`investigator alert` 併件時寫回，Grafana 的「這件事故的異常」依它撈） |

異常文件全部欄位的定義（誰寫、什麼意思）只有一處：程式 `core/anomaly.py` 的 `Anomaly`；上面的 [異常帶的欄位](#異常帶的欄位) 是 baseline 多記的那幾個。

在 Kibana 建一個 `aiops-anomalies` 的 data view，就能依 `series`、`keys` 看哪個服務、哪個時間點異常。
