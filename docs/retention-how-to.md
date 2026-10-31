# 保留天數與容量：怎麼做的

> **類型：操作 How-To**　同主題：[原理（為什麼這樣定）](retention.md)・[規格（現在的設定）](retention-reference.md)

怎麼套用、怎麼改。各資料掛 ILM 的做法見 [metrics](metrics/README.md#怎麼套用)、[trace](trace/README.md#怎麼套用)、[log](log/README.md#多久壓縮)。

## 怎麼做的（[elasticsearch/code/scripts/elasticsearch-setup.sh](../elasticsearch/code/scripts/elasticsearch-setup.sh)）

| 項目 | 做法 |
|---|---|
| 天數 | `retention.conf` 的值填進 ILM 的 JSON（`storage/ilm-*.json` 裡的 `__變數名__`） |
| 舊政策還在跑的 | ILM 會把「正在進行的階段」暫存起來：寫入中的 index 還在用 ES 內建政策（30 天才 rollover、永遠不刪）的 hot 階段，換政策要等它走完。所以腳本會把這種資料流先 rollover 一次 |

### 實測

k3s、ES 9.5.5、免費授權。

| 測什麼 | 結果 |
|---|---|
| ILM 照時間刪除 | ✅ 測試政策（1 分鐘 rollover、1 分鐘刪）：rollover 後約 1 分鐘舊 index 刪掉 |
| 重跑設定腳本 | ✅ 第二次不會重複 rollover，transform 走更新 |

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

用這兩個數字乘上保留天數，就是正式環境要的容量。
