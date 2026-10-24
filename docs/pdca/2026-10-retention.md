# 保留天數（2026-10）

**狀態**：已實作、已合進 master　　**日期**：2026-10-13　　**相關**：commit `3a38430`、`187d1c8`、[retention.md](../retention.md)
**模組**：③ 存

> 這份是我讀 commit 與 diff 整理的，沒有重跑它的測試；「Check」欄的結果來自 commit 說明與 [retention.md](../retention.md)。

## Plan：這一次要達到什麼

- **需求**：metrics、trace 只有 rollover、不會刪，硬碟遲早滿；log 只有 30 天，看不到年度週期。
- **目的**：每種資料有明確的保留天數，天數集中一處設定，改了重跑腳本就生效（舊資料也套用）；長期需要的東西（年度週期）仍然留得住。
- **範圍**：log、metrics、trace 的保留。容量與天數要用正式環境的量重算，這次不做。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `platform/central/retention.conf`（新） | 所有天數放這裡 | 天數常會調，不要散在 JSON 裡 |
| `elasticsearch/ilm-*.json`（新、改） | ILM 政策用 `__變數__` 代入天數；少一個變數腳本直接報錯 | 避免悄悄套錯天數 |
| `ilm-metrics-otel.json` | 14 天降成 5 分鐘 → 90 天降成 1 小時 → 400 天刪 | 長期看年度週期，但不用一直存 30 秒一點 |
| `ilm-policy.json`（log）、`ilm-aiops-log-stats.json`、`template-aiops-log-stats.json`、`transform-log-stats.json` | log 原文 30 天；transform 每 5 分鐘統計「服務 × level × error.type」筆數，存 400 天 | 原文很大，只留短的；年度週期靠統計 |
| `ilm-traces-otel.json`、`ilm-traces-problem-otel.json`、`traces-problem-otel-custom.json` | 隨機留下的 trace 7 天；有問題的 30 天 | 事故檢討常一兩週後才開，有問題的要留久一點 |
| `pipeline-traces-otel.json` | `reroute`：trace-sampler 標了非 random 規則的，轉到 `traces-problem.otel-<group>` | 兩種 trace 要放不同 index 才能設不同天數 |
| `metrics-otel-custom.json`、`traces-otel-custom.json` | 在 component template 指定 `index.lifecycle.name` | 新的 backing index 一開始就用新政策 |
| `elasticsearch-setup.sh` | 套用 ILM；舊 index 換政策；寫入中的 index 卡在舊政策時先 rollover；建 transform | 只改 template 對已存在的資料沒作用 |
| `docs/trace/README.md` | 保留天數狀態更新 | — |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果（來源：commit 說明、retention.md） |
|---|---|---|
| 有問題的 trace 真的會分流到 `traces-problem` | 用真的 trace-sampler 取樣後送進 ES | 有問題的進 `traces-problem`，隨機的留在 `traces-generic` |
| 降成 5 分鐘、1 小時後，延遲分布還能用 | 降解析度後查請求數、p95 | 直方圖照樣保留，請求數、p95 不變 |
| AIOps 讀 trace 會不會漏（我加的，讀設定判斷） | 看 `aiops/baseline/trace.yaml`、`aiops/rca/rca.yaml` 的 index 樣式 | 都讀 `traces-*.otel-*`，兩種 index 都會讀到 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [④] trace baseline 合併後會讀到「全留的壞 trace」 | 檢視這個分支時，發現 trace baseline 讀 `traces-*.otel-*`，而 `traces-problem` 也符合 | baseline 的樣本偏向異常，平常值被拉歪 | **合併時已改**：`aiops/baseline/trace.yaml` 的 `traces_index` 改成 `traces-generic.otel-*`。只改了正式設定，**沒用真實資料驗證**；k3s 測試用的 `aiops/tests/trace_k3s.yaml` 沒動，也沒重跑 |
| [④] 隨機 trace 只留 7 天，trace baseline 設定 `min_weeks: 2` | 對照 `aiops/baseline/trace.yaml` | 若要學「同星期幾」的樣式，7 天不夠 | 確認 trace baseline 實際要幾天 |
| [④] metrics 降成 5 分鐘後，30 秒粒度的 baseline 只剩 `fine_history: 14d` | 對照 `series.yaml` | 兩個數字要一起維護，只改一個會出問題 | 文件註明；改任一邊時同步檢查 |
| log 的 `max_primary_shard_size` 寫死 30gb，其他政策用 `retention.conf` 的 50gb | 讀 diff | 設定不一致 | 統一進 `retention.conf` |
| `traces-problem` 的 template 是從內建 `traces-otel@template` 複製出來的 | 讀 `elasticsearch-setup.sh` | ES 升級後內建組成改了，要重跑腳本才會跟上 | README 註明 |
| 天數是暫定值 | [retention.md](../retention.md) | 容量可能不夠或浪費 | 用正式環境的量重算 |

**下一輪**：用真實的 trace 驗證 trace baseline 只讀 `traces-generic` 後，樣本量夠不夠（隨機只留 5%、7 天）。
