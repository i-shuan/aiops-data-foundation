# 動態基準線補序列：p99、慢請求比例、往外呼叫耗時；一階跳變提 reset_point（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/baseline-more-series`；來源 [open-issues 9.5 ④](../meta/2026-10-open-issues.md)、[baseline reference「限制」](../../../aiops-engine/code/configs/baseline/reference.md)
**模組**：④ 判斷（動態基準線學習）

## Plan：這一次要達到什麼

- **需求**：`series.yaml` 只有 7 條序列：延遲只有 p95，看不到尾端（p99）、看不到「幾成請求超過 2 秒」；服務往外呼叫（下游 API、外部系統）的耗時沒有序列，只能等 trace 每一段。另外換版後延遲一階跳到新水準時，基準線要約 2 週才跟上，這段一直報，`reset_point` 要人想到才下。
- **目的**：
  - 多三條序列，改設定不改公式：`http.latency_p99_s`、`http.slow_ratio`（超過 2 秒的請求比例）、`http.client_p95_s`（往外呼叫，每個服務 × 對方位址）。
  - 一階跳變當輔助：每天看一次，哪條序列最近 24 小時幾乎每點都往同一邊超標、而且新水準很穩，就提一筆 `reset_point`（`status: proposed`，人核准才生效）。
- **範圍**：
  - 做：`es.py` 新的 value 寫法 `hist_ratio_above`（直方圖裡超過門檻的次數比例，用 `percentile_ranks`）；三條序列；`detector shift`（排程每天）；單元測試；對測試 ES 實跑一次。
  - 不做：ES 的 `change_point` 聚合（basic 授權不能用，實測 `license is non-compliant for [change-point-agg]`）；趨勢（慢慢惡化）另案；`groups` 依公司命名（要別人配合）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/es.py` | 新的 value 寫法 `hist_ratio_above: {field, gt}`：聚合用 `percentile_ranks`（給「≤ gt 的百分比」），值 = 1 − 它；事件數用 `value_count`；算 rate 類（事件數不夠的窗不學、不判斷） | 直方圖欄位不能用 query 篩「超過 2 秒的次數」，ES 對 histogram 型別支援 `percentile_ranks`，桶之間的值會內插（估的） |
| `configs/baseline/series.yaml` | 三條序列：`http.latency_p99_s`（p99，`min_abs` 0.1、`buckets`、`instant_z: null`）、`http.slow_ratio`（超過 2 秒的比例，`min_abs` 0.02、`min_count` 3）、`http.client_p95_s`（`http.client.request.duration` 的 p95，服務 × `server.address`） | 同 Plan；p99 跟 p95 一樣從直方圖來、有跳桶問題，沿用半個桶寬；2 秒跟靜態規則同一個門檻 |
| `detector/cmd_shift.py`（新）、`__main__.py`、`schedule.yaml`（每天 04:00） | `find_shifts`：最近 24 小時每條序列×對象的異常點（anomaly、warning 都算，規則的不算）≥ 20 個、≥ 90% 往同一邊、新水準的 MAD ÷ |新水準 − 平常| ≤ 0.3 → `proposal`：`reset_point`（`reset_at` = 第一個異常點、`match` 完全符合、`status: proposed`、`source: shift`）；同對象已有 reset_point 或提過就不重提 | ES 的 `change_point` 聚合 basic 授權不能用；一階跳變的特徵就是「每點都超標、但停在新水準」，直接從判出的點看得出來。只提案不生效，人核准前先確認那時有沒有換版 |
| `grafana/code/dashboards/aiops-feedback.json` | 漏報面板的序列選單加三條 | `test_grafana_form` 要求選單＝`series.yaml` 的序列 |
| `tests/test_values.py`、`tests/test_shift.py`（新）、`tests/test_scheduler.py` | 單元測試；排程表 11 → 12 個 | |
| `configs/baseline/reference.md`、`aiops-engine/README.md` | 序列表三列、排程表 `shift` 一列 | 跟著程式 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| `hist_ratio_above` 的值算對（≤ 門檻的百分比 → 超過的比例、沒事件給 None） | `tests/test_values.py` | 通過 |
| 三條新序列對真資料學得出來、判得出來 | 測試 ES（30 天資料）跑 `train`、`detect --window 10m` | `http.latency_p99_s`、`http.slow_ratio`：298 條序列、24 萬個點 → 2016 個時段的 baseline；detect 30 秒粒度 2 條序列 40 個點，各判出 3 個異常（跟 p95 同一批慢請求）。`http.client_p95_s`：3 條序列、286 個點 → 0 個 baseline（**資料不夠**：只有 dotnet-app 10/31 送過 512 筆往外呼叫） |
| 一階跳變的判斷：跳上去停住要提、亂跳不提、方向不一不提、點太少不提、往下也提 | `tests/test_shift.py`（合成資料） | 通過 |
| 提案長得對：`reset_point`、`proposed`、`match` 完全符合且跳脫、`reset_at` 當地時間 | `tests/test_shift.py`，並拿 `adjust.match_keys` 驗 `match` 真的只對到那個對象 | 通過 |
| `detector shift` 對真 ES 跑得起來 | 測試 ES 最近 24 小時 | 跑完：0 條像一階跳變、提 0 筆（測試環境這一天沒有換版，合理；**沒有正向樣本的實測**） |
| 漏報面板的序列選單跟 `series.yaml` 一致 | `tests/test_grafana_form.py` | 通過；全部單元測試通過 |

**沒測到的**：`shift` 沒有在真資料上碰過一階跳變，門檻（20 點、90%、離散 0.3）是從合成資料訂的；`http.client_p95_s` 沒有活資料，沒驗過判斷；排程器上 `shift` 每天 04:00 沒跑過。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `http.client_p95_s` 沒有活資料 | 測試環境只有 dotnet-app 送過一次往外呼叫 | 這條序列在測試環境永遠「資料不夠，先不判斷」 | 等有會往外呼叫的服務（.NET、Kafka app）常駐再看；不是程式問題 |
| `shift` 的門檻沒有真案例校過 | 測試環境最近一天沒有換版 | 門檻太鬆會把持續惡化也當跳變、太緊永遠不提 | 正式環境第一次換版後看它提不提、提得準不準，再調 `shift:` 的 `min_points`／`min_share`／`max_spread`（都在 `series.yaml` 可以改） |
| `slow_ratio` 是從直方圖內插估的 | 設計時 | 2 秒剛好落在桶內時，比例是桶內線性內插，不是真數 | 門檻跟桶邊界對齊（Collector 的直方圖邊界有 2 秒）就是真數；沒對齊時誤差在一個桶內 |
| ES 的 `change_point` 聚合 | 實測 basic 授權擋 | 沒有用 ES 內建的變點偵測 | 不做；`shift` 用已判出的點看就夠 |
