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

| 序列 | 依什麼切 | 什麼算異常 |
|---|---|---|
| `trace.requests` | 服務 × API | 請求數變多或變少 |
| `trace.error_rate` | 服務 × API | 錯誤率變高（至少多 5 個百分點） |
| `trace.latency_p95_ns` | 服務 × API | 延遲 p95 變高（至少慢 50 毫秒） |
| `log.lines` | 服務 | log 筆數變多或變少 |
| `log.errors` | 服務 | error／fatal 的 log 變多 |
| `pod.cpu`、`pod.memory_bytes` | namespace × Deployment | 用量變高 |

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

## 怎麼跑

```bash
cd aiops/baseline
pip install -r requirements.txt     # pandas、numpy、pyyaml、requests

export ES_URL=https://<es>:9200
export ES_API_KEY=<elasticsearch-setup.sh 發的「AIOps baseline key」>   # 只能讀資料、寫 aiops-*
# export ES_CA=/path/to/ca.crt      # ES 用內部 CA 的話

python3 -m aiops_baseline train  --config series.yaml          # 每天跑一次（例如凌晨）
python3 -m aiops_baseline detect --config series.yaml          # 每 5 分鐘跑一次，看最近 30 分鐘
```

排程例子（crontab）：

```
15 3 * * *  cd /opt/aiops/baseline && python3 -m aiops_baseline train  --config series.yaml >> train.log 2>&1
*/5 * * * * cd /opt/aiops/baseline && python3 -m aiops_baseline detect --config series.yaml >> detect.log 2>&1
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
python3 -m unittest discover -s tests -t .              # 單元測試，不需要 ES（11 項）
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
5. **trace 的長期指標**：現在直接從 trace 算；trace 只留 7 天、而且有取樣，之後改用 Collector 的 `spanmetrics`（見 [docs/retention.md](../../docs/retention.md)）。
6. **更準的方法**：用一陣子、收集到誤報和漏報後，再考慮方法 B（拆週期）、C（預測模型）。
