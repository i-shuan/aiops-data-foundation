# 時間對齊（2026-10）

**狀態**：已完成（已合進 master，PR #11）　　**日期**：2026-10-24　　**相關**：分支 `claude/collect-time-mq`
**模組**：② 清資料（時間欄位）、① 收值（主機時鐘）

## Plan：這一次要達到什麼

- **需求**：④ 判斷和 ⑦ 根因分析都靠「同一個時間點，各服務、各資料發生了什麼」排先後；時間錯了，就會把結果當原因。
  1. **應用 log 的時間沒帶時區**：中央 pipeline 用應用自己寫的時間（`_app_ts`）覆蓋 `@timestamp`，`date` processor 沒設 timezone。
  2. **收資料延遲看不到**：Collector 卡住、ES 變慢時，最新幾分鐘的資料還沒進來，看起來像流量掉。
     - ES 已記 `event.ingested`（寫進 ES 的時間），但沒記「Collector 讀到的時間」。
  3. **主機時鐘偏了沒人知道**：節點、DB 主機的時鐘偏移沒有監控；文件寫用 node_exporter，但收值線沒有 node_exporter。
- **目的**：
  1. 沒帶時區的 log 時間依設定的當地時區解讀，並標記哪些筆是推定的。
  2. 每筆 log 記下 Collector 讀到的時間；量得出「事件發生 → 寫進 ES」的延遲，太大時報（接 ② 的資料品質檢查）。
  3. 用 OpenTelemetry Collector 0.162.0 量主機時鐘偏移；沒有元件就記下替代方案。
- **範圍**：只做 log 的時間欄位與延遲、主機時鐘。trace 的時鐘差規則已有（`trace.clock_skew_ms`），每日報表不在這次。不改應用端。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `telemetry/storage/pipeline.json` | 應用的時間蓋掉 `@timestamp` 之前，先把原本的時間存成 `event.created` | `event.created`（container runtime 寫下這行的時間）是量延遲、時鐘偏差的基準 |
| 同上 | `date` processor 加 `timezone`（沒帶時區的才用） | 沒帶時區的原本被當成 UTC（差 8 小時） |
| 同上 | 加格式：空白分隔、逗號毫秒、沒有毫秒、epoch 毫秒 | 這些原本解析失敗、被默默換成 Collector 的時間 |
| 同上 | 格式都不認得時記 `log.timestamp_source: collector_unparsed` | 不再默默換掉 |
| 同上 | 解析後的時間換回 UTC 字串 | `date` 依時區輸出成 `+08:00` 字串（時間點對，但文件說存 UTC） |
| `telemetry/storage/pipeline-tz.painless`（新） | 判斷應用的時間有沒有帶時區（`Z`、`±hh:mm`、`±hhmm`；日期裡的 `-` 不算） | `date` processor 自己分不出「有沒有帶時區」 |
| 同上 | epoch 10 位是秒、13 位是毫秒 | `date` 會把 10 位的 epoch 秒當毫秒（變 1970 年） |
| 同上 | 記 `log.timestamp_source`、`event.timezone` | 看得出時間從哪來 |
| `telemetry/scripts/elasticsearch-setup.sh` | `put_pipeline` 也代入 `__TZ_SCRIPT__`、`__LOG_DEFAULT_TIMEZONE__` | 時區寫在設定，不寫死在 pipeline 裡 |
| 同上 | 新變數 `LOG_DEFAULT_TIMEZONE`（預設 Asia/Taipei） | 同上 |
| `telemetry/storage/component-mappings.json` | 加 `event.created`（date）、`event.timezone`、`log.timestamp_source`（keyword） | mapping 是 `dynamic: false`，沒加就查不到 |
| `e2e/pipeline/timestamp-cases.json`、`run-timestamp-cases.sh`（新） | 12 種時間寫法的案例測試 | 時間解析以後改壞要能馬上發現 |
| `engine/detector/quality.py`、`__main__.py` | 新檢查類型 `lag`（兩個時間欄位相減的百分位） | 量收資料延遲、應用時鐘偏差 |
| 同上 | 檢查可以指定 `time_field` 篩「最近這段」 | 見 Check「設計上的坑」 |
| `engine/configs/baseline/quality.yaml` | `quality.log_ingest_lag`（p95 ≥ 60 秒）、`quality.log_clock_skew`（中位數快或慢 ≥ 5 分鐘） | — |
| `telemetry/collectors/node.yaml` | 新增 `ntp` receiver（30 分鐘一次）、pipeline `metrics/ntp` | 主機時鐘偏移 |
| 同上 | 新增 `resource/node`（補節點名稱） | `ntp` 的資料只帶 NTP 伺服器名稱，不補不知道是哪台節點 |
| `telemetry/collectors/kustomization.yaml`、`e2e/local-k3s/otel-collector/kustomization.yaml` | 加 `NTP_SERVER` | 沒設 Collector 起不來（`missing port in address`）；測試的 overlay 整份取代設定，也要加 |
| `engine/detector/rules.py`、`__main__.py` | 規則可以 `abs: true`（取絕對值） | 時鐘快、慢都不對 |
| 同上 | 規則各自設 `lookback` | 30 分鐘才量一次，預設 10 分鐘的 lookback 看不到 |
| 同上 | `show: ns_as_ms` | 奈秒的數字人看不懂 |
| `engine/configs/baseline/rules.yaml` | `node.clock_offset`（≥ 100ms warning）、`node.clock_offset_critical`（≥ 1 秒） | — |
| `engine/tests/test_quality.py`、`test_rules.py` | `lag`、`abs`、顯示的單元測試 | — |
| `docs/log/README.md`、`docs/metrics/README.md`、`docs/architecture.md` | 欄位、狀態、檢查與規則 | — |
| `engine/configs/baseline/README.md` | 同上 | — |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 改之前，沒帶時區的時間被怎麼解讀 | 原本的 pipeline，`_simulate` 丟 6 種寫法（Collector 讀到的是台北 10:00:05） | `2026-10-24T10:00:00.123` 存成 10:00Z，**差 8 小時** |
| 同上 | 同上 | 空白分隔、逗號毫秒、epoch 都**解析失敗、默默換成 Collector 的時間** |
| 改之後，各種寫法都存成正確的 UTC | `run-timestamp-cases.sh` 12 種（見下） | 12 種都存成正確的 UTC |
| 看得出時間從哪來 | 同上 | 沒帶時區的標 `app_assumed_tz`＋`event.timezone: Asia/Taipei`；不認得的保留 Collector 時間並標 `collector_unparsed` |
| 有沒有影響 level 判斷 | 原本的 `run-level-cases.sh` | 照常 |
| 經過真的 pipeline 寫進 ES 後欄位對不對 | bulk 寫進 `k8s-*`（走預設 pipeline），查三個新欄位 | 都在 |
| 同上 | 同上 | 主機用 UTC 但沒帶時區的服務，`@timestamp` 比 `event.created` 早 8 小時（就是要抓的情況） |
| 收資料延遲抓不抓得到 | 五個服務（見下） | 3 分鐘（p95 180 秒）、20 分鐘（1,200 秒）都報 |
| 應用時鐘偏差抓不抓得到 | 同上 | +28,800 秒（時區）、-600 秒（快 10 分鐘）都報；正常的不報 |
| Collector 0.162.0 能不能量主機時鐘偏移 | 看 `components`；假 NTP 伺服器故意快 2.5 秒，讓 `ntp` receiver 去問 | 有 `ntp` receiver；`ntp.offset` = 2,500,460,925 奈秒（+2.5 秒） |
| 同上 | 同上 | 寫進 `metrics-ntpreceiver.otel-*` |
| 時鐘規則：快慢都算、門檻對、知道是哪台 | 三台「節點」問三個假 NTP：快 2.5 秒、慢 0.3 秒、差 0.05 秒 | 2.5 秒：warning＋critical；0.3 秒（慢）：warning；0.05 秒：不報 |
| 同上 | 同上 | 訊息顯示「2,500 毫秒」 |
| 改過的 node.yaml 能不能載入 | `otelcol validate`（`common.yaml`＋`node.yaml`） | 能；沒設 `NTP_SERVER` 時報 `receivers::ntp: missing port in address` |

- 12 種寫法：帶 Z、+08:00、+0800、-05:00、沒帶時區（T、空白、逗號、沒毫秒）、epoch 毫秒、epoch 秒、不認得、沒寫。
- 延遲、偏差的五個服務：Collector 3 分鐘前讀到才進 ES、正常、主機用 UTC 沒帶時區、應用時鐘快 10 分鐘、卡 20 分鐘。

### 設計上的坑（已處理）

| 坑 | 處理 |
|---|---|
| 時鐘偏差的資料 `@timestamp` 本身就錯（8 小時前、在未來），用 `@timestamp` 篩「最近 1 小時」會篩掉，永遠不報 | 檢查可以指定 `time_field`：時鐘偏差用 `event.created` 篩 |
| 卡很久才進來的資料 `@timestamp` 不在最近 15 分鐘，**最嚴重的延遲反而漏掉** | 收資料延遲用 `event.ingested` 篩；卡 20 分鐘的也報 |
| 10 位的 epoch 秒被當成毫秒，變成 1970 年 | 依位數判斷 |
| `date` processor 依時區輸出成 `+08:00` 字串 | 解析後換回 UTC |
| `ntp` receiver 最短 30 分鐘一次（設 30 秒 Collector 拒絕啟動） | 規則加各自的 `lookback`、`fresh`（40 分鐘） |
| 規則預設只看最近 10 分鐘、最新的點要在 2 分鐘內 | 同上 |
| `ntp` 的資料資源屬性只有 `ntp.host`，不知道是哪台節點 | 加 `resource/node` 補節點名稱 |

**沒測到**：
- k3s 上用真的節點跑（只用 docker 模擬）。
- DB 主機（不在 K8s 裡）。
- Fluent Bit 那條路（邊緣 VM）送來的 log 是否也有 `@timestamp`（走同一個 pipeline，`event.created` 會是 Fluent Bit 給的時間）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 所有 cluster 共用一個 `LOG_DEFAULT_TIMEZONE` | 設計：時區是中央 pipeline 的一個設定 | 不同時區的 cluster（例如海外廠）會差幾小時 | 需要時依 `orchestrator.cluster.name` 對照時區 |
| 時區規範（寫 UTC 或帶時區）還沒寫進應用標準 | — | 新服務還是可能沒帶時區，只能靠 pipeline 推定 | 寫進 [apps/standards](../../../apps/standards/tracing.md) |
| 已經寫進 ES 的舊資料不會更正 | pipeline 只影響新進來的資料 | 改之前沒帶時區的 log 仍差 8 小時 | 需要的話對特定服務 reindex |
| [①] DB 主機（不在 K8s 裡）沒有時鐘監控 | 只做了 node Collector | DB 慢、DB 時鐘偏了時，根因分析順序可能錯 | DB 主機裝 Collector（或 node_exporter）＋同一條規則 |
| [①] `NTP_SERVER` 預設是佔位的 `ntp.example.internal:123` | 實作時 | 沒改的話 `ntp` receiver 每 30 分鐘報錯、沒有資料 | 部署時填公司的 NTP |
| 時鐘偏移 30 分鐘才量一次 | `ntp` receiver 的限制 | 最慢 30 分鐘才發現 | 時鐘是慢慢漂的，先接受 |
| 門檻（延遲 60 秒、偏差 5 分鐘、時鐘 100ms／1 秒）是估的 | 沒有真實資料可校 | — | 正式環境只記錄跑 2～4 週再校 |
| [④] trace 的時鐘差日報（每天前 10 對服務）沒做 | 不在這次範圍 | — | 待排 |

- 不同時區的 cluster：`quality.log_clock_skew` 會報。
- 時區規範：`event.timezone` 有值的服務列出來，請團隊改。
