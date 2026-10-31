# baseline 實測與驗證結果

> **類型：案例 Cases**　同主題：[原理](../../../docs/rca/baseline.md)・[規格](reference.md)・[操作](README.md)

各功能實際跑出來的結果：測資、預期、實測。做法見 [README.md](README.md)，規格見 [reference.md](reference.md)，原理見 [docs/rca/baseline.md](../../../docs/rca/baseline.md)。

**目錄**

<!-- toc -->
- [trace 每一段的 baseline](#trace-每一段的-baseline)
- [規則：狀態變化、離極限多遠](#規則狀態變化離極限多遠)
- [Pod 同儕橫向比對：同一個服務的 Pod 互相比](#pod-同儕橫向比對同一個服務的-pod-互相比)
- [調整：哪些資料不學、從哪裡重新學、哪些時段放寬](#調整哪些資料不學從哪裡重新學哪些時段放寬)
- [事故紀錄與值班回饋](#事故紀錄與值班回饋)
- [慢性病清單、行事曆](#慢性病清單行事曆)
- [資料品質檢查：資料本身怪不怪](#資料品質檢查資料本身怪不怪)
  - [門檻怎麼校、誰來調](#門檻怎麼校誰來調)
- [驗證](#驗證)
<!-- /toc -->

## trace 每一段的 baseline

> 另見：[原理](../../../docs/rca/baseline.md#trace-每一段的-baseline)・[規格](reference.md#trace-每一段的-baseline)・[操作](reference.md#trace-每一段的-baseline)

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

> 另見：[原理](../../../docs/rca/baseline.md#規則狀態變化離極限多遠)・[規格](reference.md#規則狀態變化離極限多遠)・[操作](reference.md#規則狀態變化離極限多遠)

四個領域的案例與實測見 [docs/metrics/cases.md](../../../docs/metrics/cases.md)。

## Pod 同儕橫向比對：同一個服務的 Pod 互相比

> 另見：[原理](../../../docs/rca/baseline.md#pod-同儕橫向比對同一個服務的-pod-互相比)・[規格](reference.md#pod-同儕橫向比對同一個服務的-pod-互相比)・[操作](reference.md#pod-同儕橫向比對同一個服務的-pod-互相比)

**實測**（[e2e/local-k3s/run-peer-e2e.sh](../../../e2e/local-k3s/run-peer-e2e.sh)，2026-10-10，全部通過）：真的 .NET 應用（自動埋點）開 3 個副本，每次只弄壞一個。

| 案例 | 怎麼弄壞 | 抓到的 | 其他 2 個 Pod |
|---|---|---|---|
| 正常 | — | 不報 | 不報 |
| 連線池漏光 | 一個 Pod 拿了連線不還，之後的請求等 2 秒後 500 | 4 分鐘：錯誤率 **13%**（其他 0%）、p95 **1,750ms**（其他 2ms） | 不報 |
| 記憶體洩漏 | 一個 Pod 每個請求 32KB 放進不會清的 list | 第 5 分鐘：**294MB**（其他 163MB） | 不報 |
| 無窮迴圈 | 一個 Pod 裡有程式佔滿 1 核 | 第 3 分鐘：CPU **0.65 核**（其他 0.21 核，10 分鐘平均） | 不報 |

副本數 1～5、Pod 換掉、擴縮容、全壞、同名服務的邊緣情境：[phase2-anomaly/2026-10-peer-verify.md](../../../docs/pdca/phase2-anomaly/2026-10-peer-verify.md)。

同一段時間整個服務的錯誤率是 **9%**（3 個 Pod 裡 1 個壞）。這次服務平常是 0%，跟平常比（baseline）也會報，但只知道「服務錯誤率高」、不知道是哪個 Pod；Pod 越多稀釋越多：10 個 Pod 壞 1 個大約剩 1%，就低於 baseline 的門檻（多 5 個百分點）了。

## 調整：哪些資料不學、從哪裡重新學、哪些時段放寬

> 另見：[原理](../../../docs/rca/baseline.md#調整哪些資料不學從哪裡重新學哪些時段放寬)・[規格](reference.md#調整哪些資料不學從哪裡重新學哪些時段放寬)・[操作](reference.md#調整哪些資料不學從哪裡重新學哪些時段放寬)

**實測**（2026-10-23，ES 9.5.5，log 8 天）：

| 案例 | 沒有調整 | 有調整 |
|---|---|---|
| `long`：8 天裡 5 天是長事故（log 3 倍） | 平常學成 13.57（每 5 分鐘；正常是 5）：事故變成平常 | `exclude_window` 排掉事故 → 15.00（每 15 分鐘，即每 5 分鐘 5）：回到正常 |
| `ver`：3 天前換版，log 變 2 倍 | 平常學成 5.36（舊版） | `reset_point`（`proposed` 時不變；`approve` 後）→ 10.00（新版） |
| `long` 最近 40 分鐘 log 爆 10 倍 | 報 | 週期時段 `mute` → 不報；`reset_point` 後 24 小時暖機 → 不報 |
| 舊的 `tune` 調整 | — | 照常生效，`adjust list` 一起列出 |

## 事故紀錄與值班回饋

> 另見：[原理](../../../docs/rca/baseline.md#事故紀錄與值班回饋)・[規格](reference.md#事故紀錄與值班回饋)

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

## 慢性病清單、行事曆

> 另見：[原理](../../../docs/rca/baseline.md#慢性病清單行事曆)・[規格](reference.md#慢性病清單行事曆)・[操作](reference.md#慢性病清單行事曆)

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

> 另見：[原理](../../../docs/rca/baseline.md#資料品質檢查資料本身怪不怪)・[規格](reference.md#資料品質檢查資料本身怪不怪)

### 門檻怎麼校、誰來調

**實測**（2026-10-23，ES 9.5.5 跑過 `elasticsearch-setup.sh`；當時 metrics 走 ES 9 的 `/_otlp/v1/metrics`，log 直接寫 `k8s-*`；8.19 改成 Collector 的 `elasticsearch` 寫入元件，文件形狀一樣，沒重測）：

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

## 驗證

> 另見：[操作](verification.md#驗證)

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
