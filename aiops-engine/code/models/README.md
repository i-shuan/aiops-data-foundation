# 五個模型的輸入規格

> **類型：規格 Reference**　資料怎麼餵進來見 [sources/README.md](../sources/README.md)・方法為什麼這樣設計見 [docs/rca/README.md](../../../docs/rca/README.md)

`import models` 就能用，只吃 DataFrame／list，不碰 ES、不讀設定檔。演算法本體在 `detector/`、`investigator/`（沒搬，排程與既有測試照舊）；每個模組有 `check_*()`，欄位不對會說缺什麼。

| 模組 | 模型 | 標準輸入（一列是什麼） | 主要函式 |
|---|---|---|---|
| `models.baseline` | M1 動態基準線學習 (Dynamic Baseline Learning) | `keys… + ts + value + n`：一條序列在一個時間窗的值；`n` 是底下的事件數，量測值是 NaN | `train`、`score`、`score_poisson`、`mark_status`、`pick_resolution`、`fit_and_score`（一次做完） |
| `models.peer` | M2 Pod 同儕橫向比對 (Peer Comparison) | `group… + member + value [+ volume]`：一個 Pod 在這個 window 的值 | `compare`、`compare_checked` |
| `models.rules` | M3 靜態臨界值規則 (Static Threshold Rules) | `{by 的值元組: [(ts, 文件), …]}`，文件是攤平的 dict；window 型用 `by… + value + n` | `evaluate`、`evaluate_window`、`points_of`（原始列 → points） |
| `models.trace` | M4 trace 分段 | span：`{trace_id, span_id, parent, service, name, kind, target, start_ms, dur_ms, error, …}`，一列一段 | `group_traces`、`compare_segments`、`summarize`、`segment`、`self_times` |
| `models.similar` | M5 相似事故檢索 | 新事故的 `alert`、`findings`；候選 `[(id, 事故文件), …]`，文件有 `fingerprint`、`status` | `fingerprint`、`rank`、`confirmed_only`、`format_similar` |

## 欄位

| 欄位 | 型別 | 說明 |
|---|---|---|
| `ts` | 帶時區的時間（UTC） | 時間窗的開始 |
| `value` | float | 這個窗的值；單位是序列的單位（秒、比例、次數） |
| `n` | float | 底下的事件數（請求數），`pick_resolution`、`mask_thin`、`min_count` 用；量測值 NaN |
| keys | 任意 | 序列的識別，例如 `service`、`route`；`train` 的 `keys` 參數要跟欄位名一樣 |

## 最小的一條路（M1）

```python
import pandas as pd
from models import baseline

hist = pd.DataFrame({"service": "a", "route": "r", "ts": [...], "value": [...], "n": [...]})   # 過去幾週
cur = pd.DataFrame({"service": "a", "route": "r", "ts": [...], "value": [...], "n": [...]})    # 現在
b, scored = baseline.fit_and_score(hist, cur, ["service", "route"], direction="up", min_abs=0.05, step="1h")
scored[scored["anomaly"]]
```

指令（`detector train`／`detect`）做的比這多：自動挑粒度、借組 baseline、排除時段、`aiops-tuning` 的 override。要那些就走指令＋[sources](../sources/README.md)。
