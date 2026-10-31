# 保留天數與容量：怎麼做的

> **類型：操作 How-To**　同主題：[原理（為什麼這樣定）](retention.md)・[規格（現在的設定）](retention-reference.md)

怎麼套用、怎麼改。

## 怎麼做的（[elasticsearch/code/scripts/elasticsearch-setup.sh](../elasticsearch/code/scripts/elasticsearch-setup.sh)）

| 項目 | 做法 |
|---|---|
| 天數 | `retention.conf` 的值填進 ILM 的 JSON（`storage/ilm-*.json` 裡的 `__變數名__`） |
| metrics、trace 掛上自己的 ILM | ES 內建模板預留的 `metrics-otel@custom`、`traces-otel@custom` 加 `index.lifecycle.name`；已經存在的 index 也一起換 |
| 有問題的 trace 分開存 | trace 的 pipeline（`traces-otel`）看 `scope.attributes.tailsampling.policy`（trace-sampler 記的是哪條規則留下的）：不是 `random` 的轉到 `traces-problem.otel-<group>`。Collector 不用改；寫入 key 的權限 `traces-*.otel-<group>` 本來就涵蓋；讀的人（根因分析、唯讀角色）查 `traces-*.otel-*` 兩邊都看得到 |
| 應用的直方圖不降解析度 | `metrics-generic.otel-*` 另一個模板 `metrics-hist-otel`：從內建的 `metrics-otel@template` 複製組成、多疊 `metrics-hist-otel@custom`（ILM `metrics-hist-otel`，只有 rollover、刪除）；已經存在的也換政策、寫入中的 rollover 一次 |
| `traces-problem.otel-*` 的模板 | 每次從內建的 `traces-otel@template` 複製組成、多疊 `traces-problem-otel@custom`（換 ILM），ES 升級改了內建組成也會跟上 |
| log 統計 | transform `aiops-log-stats`：每 5 分鐘把新的 log 統計一次，寫進別名 `aiops-log-stats`（背後的 index 每 30 天換一個） |
| 舊政策還在跑的 | ILM 會把「正在進行的階段」暫存起來：寫入中的 index 還在用 ES 內建政策（30 天才 rollover、永遠不刪）的 hot 階段，換政策要等它走完。所以腳本會把這種資料流先 rollover 一次 |

### 實測

除了第一列，都是 k3s、ES 9.5.5、免費授權。

| 測什麼 | 結果 |
|---|---|
| 直方圖降解析度（ES 8.19.23，k3d） | ❌ 所以 `metrics-generic` 不降：time series index 放 2 小時、每 30 秒一筆 `histogram`（共 7,911 個請求），`_downsample` 成 1 小時一點後只剩 **70 個、p95 1.75 → 1.13 秒**——每個小時只留一筆的 values／counts，沒有合併。`histogram` 不能設 `time_series_metric`（mapping 直接報錯）。同一份資料的 `_sum`（gauge）降完是 `aggregate_metric_double`，總和一樣。ES 9.5.5 的 `exponential_histogram` 降完請求數、p95 不變 |
| 套用不降解析度的政策（ES 8.19.23，k3d） | ✅ `elasticsearch-setup.sh` 跑完：`metrics-generic.otel-*` 的模板是 `metrics-hist-otel`、每個 index 都是 `metrics-hist-otel`，寫入中的 rollover 一次；`kubeletstatsreceiver` 這類照舊 `metrics-otel` |
| ILM 照時間降解析度 | ✅ 時間範圍已過的 index 套「馬上降成 1 小時」的測試政策，約 30 秒換成 `downsample-1h-…` |
| ILM 照時間刪除 | ✅ 測試政策（1 分鐘 rollover、1 分鐘刪）：rollover 後約 1 分鐘舊 index 刪掉 |
| 有問題的 trace 分流 | ✅ 寫一筆 `tailsampling.policy: error` 的 span，進到 `traces-problem.otel-<group>`，自動套 `traces-problem-otel` 模板與 30 天政策；`random`、沒有 policy 的留在 `traces-generic` |
| 真的 trace-sampler 取樣後分流 | ✅ 尾端取樣壓測一輪（正式比例：正常的隨機留 5%；每秒 100 個請求、2 分鐘）：錯誤、慢、吞掉例外的 1,256 個 trace（5,024 段）全進 `traces-problem.otel-imc`（policy 是 slow、error、exception）；隨機留下的 615 個 trace（2,460 段）全在 `traces-generic.otel-imc`。兩邊合起來跟壓測統計的「留下」數字一致，沒有漏 |
| 根因分析、trace baseline 回歸 | ✅ k3s 回歸 11 項、9 項全過（讀 `traces-*.otel-*`，兩個資料流都看得到） |
| log 統計 | ✅ 65 萬筆 log 統計成 2,192 筆（約 1/300） |
| 重跑設定腳本 | ✅ 第二次不會重複 rollover，transform 走更新 |

## 量太大時怎麼降（依效果排序）

| 做法 | 效果 |
|---|---|
| **EAP 不在 INFO 寫完整的 SML 內容**：只寫摘要（機台、訊息種類、事件編號、結果），完整內容放 DEBUG 或另外存 | EAP log 可能減到 1/5 以下，**最有效** |
| **trace 尾端取樣**（已做：trace-sampler，正常的留 5%、有問題的全留） | 實測總量剩約 15%；長期的請求數、延遲來自應用的 metrics，不受取樣影響。還要再降就調低 `RANDOM_PCT`，**不要在應用裡調 `sampleRatio`**（出事的請求會在一開始就被丟掉） |
| **吵的服務把不必要的 INFO log 降成 DEBUG** | 常見的「每個請求印好幾行」可以省一大半 |
| **高流量的原始 log 留短一點**（例如 14 天），長期改留統計值 | 依 group 設；資料流本來就依 group 分開 |

## 怎麼量正式環境的量

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
