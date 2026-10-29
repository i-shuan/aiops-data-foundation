# 保留天數與容量

一句話：**原始資料只留查問題需要的天數；AIOps 動態基準線學習 (Dynamic Baseline Learning) 需要的長期資料，用降解析度的 metrics 和統計值留，便宜很多。**

✅ **已套用**（2026-10-13，k3s 驗過）。天數都在 [telemetry/scripts/retention.conf](../telemetry/scripts/retention.conf)，改那裡、重跑 `elasticsearch-setup.sh` 就生效。

## 現在的設定

| 資料 | 資料流 | ILM | 保留 |
|---|---|---|---|
| **metrics** | `metrics-*.otel-*` | `metrics-otel` | 原始 30 秒一點 **14 天** → 降成 **5 分鐘**一點 → 第 **90 天**降成 **1 小時**一點 → 第 **400 天**刪 |
| **log 原文** | `k8s-*` | `k8s-logs` | **30 天** |
| **log 統計** | `aiops-log-stats`（transform 每 5 分鐘寫） | `aiops-log-stats` | **400 天**：每 5 分鐘、每個 group × cluster × namespace × 服務 × level × `error.type` 的筆數 |
| **trace：隨機留下的 5%** | `traces-generic.otel-*` | `traces-otel` | **7 天**（給 baseline、拓撲用） |
| **trace：有問題的**（錯誤、5xx、例外、慢） | `traces-problem.otel-*` | `traces-problem-otel` | **30 天**（事故檢討常常一兩週後才開） |
| failure store（ES 拒收的資料） | 各資料流的 failure store | ES 預設 | **30 天**（ES 9 的預設，不用另外設） |
| AIOps 自己的（baseline、異常、trace 段統計） | `aiops-*` | — | 不會自己長大：baseline 每次訓練覆蓋；異常、段統計量很小，之後再定 |

- 天數從「換到下一個 index（rollover）」那一刻起算；rollover 每天一次，所以實際最多多留約 1 天。
- 30 秒、1 分鐘粒度的 baseline 只學最近 14 天（`fine_history`），剛好對齊 metrics 原始資料的 14 天；5 分鐘以上的粒度學 4 週，用降解析度後的資料也夠。

## 怎麼做的（[telemetry/scripts/elasticsearch-setup.sh](../telemetry/scripts/elasticsearch-setup.sh)）

| 項目 | 做法 |
|---|---|
| 天數 | `retention.conf` 的值填進 ILM 的 JSON（`storage/ilm-*.json` 裡的 `__變數名__`） |
| metrics、trace 掛上自己的 ILM | ES 9 內建模板預留的 `metrics-otel@custom`、`traces-otel@custom` 加 `index.lifecycle.name`；已經存在的 index 也一起換 |
| 有問題的 trace 分開存 | trace 的 pipeline（`traces-otel`）看 `scope.attributes.tailsampling.policy`（trace-sampler 記的是哪條規則留下的）：不是 `random` 的轉到 `traces-problem.otel-<group>`。Collector 不用改；寫入 key 的權限 `traces-*.otel-<group>` 本來就涵蓋；讀的人（根因分析、唯讀角色）查 `traces-*.otel-*` 兩邊都看得到 |
| `traces-problem.otel-*` 的模板 | 每次從內建的 `traces-otel@template` 複製組成、多疊 `traces-problem-otel@custom`（換 ILM），ES 升級改了內建組成也會跟上 |
| log 統計 | transform `aiops-log-stats`：每 5 分鐘把新的 log 統計一次，寫進別名 `aiops-log-stats`（背後的 index 每 30 天換一個） |
| 舊政策還在跑的 | ILM 會把「正在進行的階段」暫存起來：寫入中的 index 還在用 ES 內建政策（30 天才 rollover、永遠不刪）的 hot 階段，換政策要等它走完。所以腳本會把這種資料流先 rollover 一次 |

### 實測（k3s，ES 9.5.5，免費授權）

| 測什麼 | 結果 |
|---|---|
| metrics 降解析度後直方圖還在不在 | ✅ 真的 .NET 應用 metrics：5.6 萬筆降成 1 小時一點剩 3,474 筆；**請求數（208,693）、p95（1.75 秒）完全一樣**。直方圖保留成 `exponential_histogram`；量測值（gauge）變成最小、最大、總和、筆數，平均照樣算得出來 |
| ILM 照時間降解析度 | ✅ 時間範圍已過的 index 套「馬上降成 1 小時」的測試政策，約 30 秒換成 `downsample-1h-…` |
| ILM 照時間刪除 | ✅ 測試政策（1 分鐘 rollover、1 分鐘刪）：rollover 後約 1 分鐘舊 index 刪掉 |
| 有問題的 trace 分流 | ✅ 寫一筆 `tailsampling.policy: error` 的 span，進到 `traces-problem.otel-<group>`，自動套 `traces-problem-otel` 模板與 30 天政策；`random`、沒有 policy 的留在 `traces-generic` |
| 真的 trace-sampler 取樣後分流 | ✅ 尾端取樣壓測一輪（正式比例：正常的隨機留 5%；每秒 100 個請求、2 分鐘）：錯誤、慢、吞掉例外的 1,256 個 trace（5,024 段）全進 `traces-problem.otel-imc`（policy 是 slow、error、exception）；隨機留下的 615 個 trace（2,460 段）全在 `traces-generic.otel-imc`。兩邊合起來跟壓測統計的「留下」數字一致，沒有漏 |
| 根因分析、trace baseline 回歸 | ✅ k3s 回歸 11 項、9 項全過（讀 `traces-*.otel-*`，兩個資料流都看得到） |
| log 統計 | ✅ 65 萬筆 log 統計成 2,192 筆（約 1/300） |
| 重跑設定腳本 | ✅ 第二次不會重複 rollover，transform 走更新 |

## 每筆資料佔多少（測試環境實測）

| 資料 | 實測每筆 | 估算用 | 一筆是什麼 |
|---|---|---|---|
| log | 135 bytes | **250 bytes** | 一行 log。測試資料每行很像，壓縮特別好；真實 log 內容較雜，保守用 250 |
| Pod 資源（kubelet） | 57 bytes | 60 bytes | 一個 Pod 或容器的一組數值，每個 Pod 每分鐘約 13 筆 |
| K8s 物件狀態 | 47 bytes | 50 bytes | 每個 Pod 每分鐘約 5 筆 |
| 應用自己的 metrics | 78 bytes | 80 bytes | 一組維度的一個時間點。Java agent 預設會吐 JVM、HTTP 等幾十到上百組，**每個 Pod 每分鐘約 100～300 筆，要用真的 Java 服務量過** |
| trace | 276 bytes | **400 bytes** | 一段（span）。測試的 span 很簡單；真實的帶 HTTP、SQL 等欄位，保守用 400 |

都是 ES 壓縮後、**不含副本**的大小。正式環境有一份副本的話，**乘以 2**。

## 為什麼這樣定

### 原則

1. **原始資料留「查問題」需要的天數**：出事時要看到每一行 log、每一個 span、每 30 秒的數值，通常最近 1～4 週就夠。
2. **AIOps 的長期資料用「降解析度」和「統計值」**：動態基準線學習需要的是每 5 分鐘或每小時的數值，不需要每一行原始資料。
3. **至少留 13 個月的統計**：看得到年度的週期（年度保養、旺季、月底）。

### 天數與理由

| 資料 | 原始 | 降解析度後 | 刪除 |
|---|---|---|---|
| **metrics** | 30 秒一點，留 **14 天** | 第 14 天起降成 **5 分鐘一點**；第 90 天起降成 **1 小時一點** | **400 天**（約 13 個月） |
| **log** | 原文留 **30 天**（現在的設定） | 每 5 分鐘統計一次「每個服務各 level 的筆數、各 `error.type` 的筆數」，存成另一個小 index（ES 內建的 transform，免費），留 **400 天** | 原文 30 天 |
| **trace** | 隨機留下的 span 留 **7 天**，有問題的留 **30 天** | 不另外留：每個服務、每個 API 的請求數、錯誤數、延遲分布，應用的 metrics（`http.server.request.duration`）已經有，跟著 metrics 留 **400 天** | 7 天／30 天 |
| failure store（ES 拒收的資料） | 留 **30 天**，夠查原因 | — | 30 天 |

**為什麼這樣分：**

- **metrics 降解析度**：實測免費授權可用，直方圖（延遲分佈）照樣保留。每小時一點的量測值只保留那一小時的最小、最大、總和、筆數，動態基準線學習夠用。
- **log 不留更久，改留統計**：log 原文是最佔空間的。AIOps 看 log 主要是「哪種錯誤突然變多」，留每 5 分鐘的筆數就夠，原文 30 天用來查細節。
- **trace 原文只留 7 天**：trace 主要用來查「最近這次出事」。長期要的是每個 API 的延遲、錯誤率，直接用應用的 metrics（Java agent 的 `http.server.request.duration` 直方圖）。**它每個請求都算，不受 trace 取樣影響**，就算 trace 只記一成，請求數和延遲還是準的。
  - ⚠️ **不用 Collector 的 `spanmetrics`**：取樣是在應用裡決定的，沒被取樣的 span 根本不會送到 Collector，`spanmetrics` 只看得到被取樣的那些，請求數會少算。

### 為什麼 trace 留最短：「log 像日記，trace 像錄影帶」

業界常見的說法（2026-10-10 討論時整理，括號裡是對照我們實際的數字與做法）：

**1. 量差很多。** log 通常只記關鍵事件，一個請求印一兩行；trace 在微服務裡，一個請求會分裂成很多段（閘道 → 認證 → 購物車 → 庫存 → 幾次 Redis → 幾次 SQL），一個請求就是幾十段。
（我們的估算見下面「容量估算」：trace 全部記的話，每天的量是 log 的 **8 倍**（低流量服務，每個請求 5 段）到 **13 倍**（高流量，8 段）。trace 改成尾端取樣、只留約 15% 之後，log 反而是最大宗。）

**2. 每一段都是一筆文件。** 要看出「慢在哪、誰呼叫誰」，每一段都要帶 trace_id、parent、開始時間、標籤。
（修正：ES 裡**沒有**「樹狀關聯索引」。一段就是一筆普通文件，`trace_id`、`parent_span_id` 只是一般欄位，樹是查詢時才依 trace_id 撈出來接起來的。trace 貴的真正原因是**筆數多**，一個請求 5～8 筆，每一筆都有固定開銷。log 的訊息要做全文檢索，索引也不小；兩者 ES 都會壓縮，壓縮率不是主因。）

**3. 時間價值遞減得快。** log 有稽核、合規、歷史追溯的價值（例如登入、交易紀錄依法規留 6 個月到 1 年）；trace 主要用來即時排查，「三天前每一次正常請求花 20ms 還是 22ms」沒人在乎，在乎的是「最近慢在哪」和「出事當下的完整呼叫鏈」。
（補充：長期的趨勢**有人在乎**，只是不靠 trace 原文。每支 API 的延遲、錯誤率趨勢來自應用的 metrics，每個請求都算；每支 API 每一段平常呼叫幾次、花多久，來自 trace baseline 的統計 `aiops-trace-segments`。這兩個都很小，跟著 metrics 留 400 天。所以 trace 原文只留幾天能成立，前提是長期要用的已經另外存成統計值。稽核要用的 log（登入、交易）應該是另外的稽核紀錄，依法規另定天數，不跟一般應用 log 一起算。）

| 資料 | 存什麼 | 留多久 | 我們的方案 |
|---|---|---|---|
| metrics | 聚合後的數字（CPU、請求數、錯誤率、延遲分布） | 最長：數月到數年，看趨勢 | 原始 14 天、降解析度到 400 天 |
| log | 警告、錯誤常駐；正常的可以壓縮放冷儲存 | 中到長：30～180 天 | 原文 30 天＋每 5 分鐘的統計 400 天（沒有冷儲存） |
| trace | 正常的抽 5%、有問題的 100%（尾端取樣） | 最短：3～7 天，過期直接刪 | 隨機的 7 天、有問題的 30 天 |

這也是為什麼要做尾端取樣（[docs/trace/scenarios.md](trace/scenarios.md#橫跨所有場景的前提取樣)、[實測](../e2e/local-k3s/tail-sampling/README.md)）：trace 不能全部長存，有限的空間要留給「出問題的 100%」和「隨機抽的 5%」。實測每秒 500 個請求時，留下的量約是全部的 15%。

> 改成尾端取樣後，上面「不用 `spanmetrics`」的理由要重看：應用改成全部送，node Collector 看得到每一段，在**取樣之前**算的 spanmetrics 就不會少算。不過每個 API 的請求數、延遲應用的 metrics 已經有，目前不需要。

### 容量估算

> [!IMPORTANT]
> **下面「要準備的硬碟」都是「ES 上同時存著的總量」，不是每天、也不是每個月的量。**
> 資料有保留天數，過期的每天自動刪掉：trace 7 天、log 30 天就到頂，之後新寫入和刪除抵銷，總量大約**一個月後**就不再成長。
> 降解析度的 metrics 會慢慢多一點，但很小。所以「每月寫入」可以比「要準備的硬碟」還大。

公式：

```
每天寫入     = 每秒筆數 × 86,400 × 每筆大小 × （有副本就 × 2）
同時存著的量 = 每天寫入 × 保留天數（降解析度的部分另外算，通常很小）
要準備的硬碟 = 同時存著的量 × 1.2～1.3（ES 硬碟用到 85% 就不再放新的 shard、95% 整個唯讀）
```

以下都**含一份副本**，trace 依取樣比例分開列。

#### 低流量服務（一般的服務）

假設：2 個 Pod、每秒 5 個請求、每個請求 5 段 trace、每秒 5 行 log、每個 Pod 每分鐘 200 筆應用 metrics。

| 資料 | 每天寫入 | 同時存著的量 |
|---|---|---|
| log（原文 30 天） | 216MB | 6.5GB |
| Pod 資源與 K8s 狀態 | 6MB | 0.1GB |
| 應用 metrics（原始 14 天＋降解析度到 400 天） | 92MB | 2.2GB |
| trace 全部記（7 天） | 1.7GB | 12GB |
| trace 記一成（7 天） | 0.17GB | 1.2GB |
| **合計：trace 全部記** | **約 2GB** | **約 21GB** |
| **合計：trace 記一成** | **約 0.5GB** | **約 10GB** |

#### 高流量服務（MES 核心交易、派工這類）

假設：4 個 Pod、每秒 100 個請求、每個請求 8 段 trace、每秒 100 行 log、每個 Pod 每分鐘 300 筆應用 metrics。

| 資料 | 每天寫入 | 同時存著的量 |
|---|---|---|
| log（原文 30 天） | 4.3GB | 130GB |
| 應用 metrics | 0.3GB | 7GB |
| trace 全部記（7 天） | 55GB | 387GB |
| trace 記一成 | 5.5GB | 39GB |
| trace 記百分之一 | 0.55GB | 4GB |
| **合計：trace 全部記** | **約 60GB** | **約 520GB** |
| **合計：trace 記一成** | **約 10GB** | **約 175GB** |

一個高流量服務等於 15～50 個低流量服務。trace 取樣調低之後，**log 就是最大宗**。

#### EAP（設備通訊）

量取決於機台數，要另外算：

```
EAP log 每天寫入 = 機台數 × 每台每秒幾則訊息 × 每則 log 壓縮後大小 × 86,400 × 2（副本）
```

例：1,000 台機台、每台每秒 2 則、每則 log 原文 1.5KB（壓縮後約 300 bytes）→ **每天寫入約 100GB，留 30 天同時存著約 3TB**。

很多 EAP 會把整段 SML 內容寫進 log，這是最大的變數：**機台數、訊息頻率、log 寫了多少內容三個乘起來，可以差到十倍。先找一台 EAP 量一天實際的 log 量再算。**

#### 依服務數換算（低流量服務）

| 服務數 | 同時存著的量：trace 全部記 | trace 記一成 |
|---|---|---|
| 50 | 約 1TB | 約 0.5TB |
| 100 | 約 2.1TB | 約 1TB |
| 200 | 約 4.2TB | 約 2TB |
| 500 | 約 10.5TB | 約 5TB |

#### 混合例子：150 個低流量、20 個高流量、1,000 台機台的 EAP，trace 記一成

| 來源 | 每天寫入 | 每月寫入 | 同時存著的量 |
|---|---|---|---|
| 150 個低流量服務 | 約 72GB | 約 2.2TB | 約 1.5TB |
| 20 個高流量服務 | 約 200GB | 約 6TB | 約 3.5TB |
| EAP log（1,000 台） | 約 100GB | 約 3TB | 約 3TB |
| **合計** | **約 370GB** | **約 11TB** | **約 8TB → 要準備約 10TB 硬碟** |

#### 量太大時怎麼降（依效果排序）

| 做法 | 效果 |
|---|---|
| **EAP 不在 INFO 寫完整的 SML 內容**：只寫摘要（機台、訊息種類、事件編號、結果），完整內容放 DEBUG 或另外存 | EAP log 可能減到 1/5 以下，**最有效** |
| **trace 尾端取樣**（已做：trace-sampler，正常的留 5%、有問題的全留） | 實測總量剩約 15%；長期的請求數、延遲來自應用的 metrics，不受取樣影響。還要再降就調低 `RANDOM_PCT`，**不要在應用裡調 `sampleRatio`**（出事的請求會在一開始就被丟掉） |
| **吵的服務把不必要的 INFO log 降成 DEBUG** | 常見的「每個請求印好幾行」可以省一大半 |
| **高流量的原始 log 留短一點**（例如 14 天），長期改留統計值 | 依 group 設；資料流本來就依 group 分開 |

#### 不建議放進這套 ES 的：FDC 感測器資料

每台機台幾百個感測器、每秒一筆，1,000 台就是每秒 30 萬筆，是另一個等級的量，而且通常已經有專門的系統。AIOps 需要的話，只匯入每分鐘的統計值，或 FDC 判定的告警結果。

### 怎麼量正式環境的量

在正式的 ES 上查前一天每種資料各進來多少：

```
GET k8s-*,metrics-*.otel-*,traces-*.otel-*/_search
{"size": 0,
 "query": {"range": {"@timestamp": {"gte": "now-1d/d", "lt": "now/d"}}},
 "aggs": {"stream": {"terms": {"field": "data_stream.dataset", "size": 50}}}}
```

以及每個 index 的實際大小：

```
GET _cat/indices/.ds-k8s-*,.ds-metrics-*,.ds-traces-*?v&h=index,creation.date.string,docs.count,pri.store.size&s=creation.date
```

用這兩個數字乘上面的天數，就是正式環境要的容量。

## 還要決定的

1. **天數確認**：上面的 14 天／90 天／400 天、log 30 天、trace 7 天／30 天是建議值、已經套用。要改的話改 `retention.conf`。各部門（group）要不要不同？資料流本來就依 group 分開，可以各自設（目前全部一樣）。
2. **副本**：正式環境的 ES 要不要每份資料都有一份副本（容量 × 2）？三個節點以上建議要。目前沒設，用 ES 的預設。
3. **正式環境的量**：用「怎麼量正式環境的量」的查詢查一次，再用實際數字重算容量。
4. **`aiops-*` 的保留**：異常（`aiops-anomalies`）、trace 段統計（`aiops-trace-segments`）量很小，還沒設刪除；事故紀錄、回饋做出來後一起定（事故紀錄要長留）。
