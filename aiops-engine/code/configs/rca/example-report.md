# 根因分析：rca-mes-api（GET /lots/{id}），10-10 10:26～10-10 10:31（Asia/Taipei）

## 1. Metrics：發現異常

| 時間 | 指標 | 哪一段 | 現在 | 平常 | 偏離 | 可信度 |
|---|---|---|---|---|---|---|
| 10-10 10:26 | `http.latency_p95_s` |  | 1.55 | 0.08 | 41.2 倍 |  |

## 2. Trace：這個服務收到的請求

| | 現在 | 平常（出事前一段時間） |
|---|---|---|
| 請求數（trace，有取樣） | 146 | 510 |
| 錯誤 | 27 | 0 |
| p50 | 1.54 s | 72 ms |
| p95 | 1.55 s | 82 ms |

## 3. Trace：慢在哪一段

比較最慢、出錯的 146 個請求和平常的 200 個請求，每個請求在每一段**自己**花的時間（扣掉等下游的時間，所以慢的那段不會一路算到最上層）：

| 段 | 現在 | 平常 | 多了 | 每個請求呼叫幾次 |
|---|---|---|---|---|
| rca-wip-worker › SELECT LOT_HISTORY → oracle | 1.50 s | 33 ms | **+1.47 s** | 1.0（平常 1.0） |
| rca-wip-worker › GET /wip | 17 ms | 16 ms | **+1.6 ms** | 1.0（平常 1.0） |
| rca-mes-api › GET /lots/{id} | 3.0 ms | 3.0 ms | **+0.0 ms** | 1.0（平常 1.0） |
| rca-mes-api › GET → rca-wip-worker | 18 ms | 18 ms | **-0.2 ms** | 1.0（平常 1.0） |

錯誤最早出在哪一段（上層只是把錯誤往上傳）：

| 段 | 現在 | 平常 |
|---|---|---|
| rca-wip-worker › SELECT LOT_HISTORY → oracle | 27 個請求 | 0 |

## 4. 最慢的那個請求（trace_id `f4ecb341a979db76a3820050df0ce846`）

```
rca-mes-api › GET /lots/{id}                                 |████████████████████████████████████████|   1572.0 ms（自己 3.0）
  rca-mes-api › GET → rca-wip-worker                         |████████████████████████████████████████|   1569.0 ms（自己 9.0）
    rca-wip-worker › GET /wip                                |████████████████████████████████████████|   1561.0 ms（自己 21.0）
      rca-wip-worker › SELECT LOT_HISTORY → oracle           | ███████████████████████████████████████|   1540.0 ms（自己 1540.0）
```

## 5. Log：那幾段發生了什麼事

看 rca-wip-worker, rca-mes-api 這段時間的 warn／error，跟出事前一段時間比；新出現的排前面：

| 服務 | 樣式 | 現在 | 平常 | 例子 |
|---|---|---|---|---|
| rca-wip-worker | 🆕 `Connection pool timeout waiting for connection to <ip> after <n>ms, lot L<n>` | 145 | 0 | Connection pool timeout waiting for connection to 10.0.1.5:5432 after 1510ms, lot L100849 |
| rca-mes-api | 🆕 `lot L<n> failed: wip-worker returned error` | 27 | 0 | lot L100850 failed: wip-worker returned error |

最慢那個請求的 log（依 trace_id 從各服務撈出來，2 行）：

```
2026-10-10T02:29:48.000Z  rca-wip-worker error  Connection pool timeout waiting for connection to 10.0.1.5:5432 after 1540ms, lot L100816
2026-10-10T02:29:48.000Z  rca-mes-api    info   lot L100816 handled in 1570ms
```

## 6. 這些請求經過的服務

```mermaid
graph LR
  rca_mes_api["rca-mes-api"] -->|"146 次，p95 1531 ms，錯誤 27"| rca_wip_worker["rca-wip-worker"]
  rca_wip_worker["rca-wip-worker"] -->|"146 次，p95 1510 ms，錯誤 27"| oracle["oracle"]
```

## 結論（給人確認）

- rca-mes-api（GET /lots/{id}）變慢，主要多在 **rca-wip-worker › SELECT LOT_HISTORY → oracle**：每個請求多花 1.47 s（平常 33 ms）。
- 錯誤最早出在 **rca-wip-worker › SELECT LOT_HISTORY → oracle**（27 個請求，平常 0）。
- 同時間 rca-wip-worker 新出現 log「Connection pool timeout waiting for connection to 10.0.1.5:5432 after 1510ms, lot L100849」，145 次。
- 同時間 rca-mes-api 新出現 log「lot L100850 failed: wip-worker returned error」，27 次。
