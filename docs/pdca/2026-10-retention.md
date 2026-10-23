# 保留天數（2026-10）

**狀態**：已完成實作，**未合進 master**　　**日期**：2026-10-13　　**相關**：分支 `claude/retention`（commit `3a38430`、`187d1c8`）、[retention.md](../retention.md)

## Plan：要解決什麼

- **問題**：metrics、trace 只有 rollover、不會刪，硬碟遲早滿；log 只有 30 天，年度週期看不到。
- **目標／驗收**：天數集中一處設定；重跑 `elasticsearch-setup.sh` 就生效，舊資料也套用；金額不用先算死，用正式量再重算。
- **做法**：metrics 降解析度後長留；log 原文短留、另存統計長留；trace 分「隨機」和「有問題」兩種各留不同天數。

## Do：實際做了什麼

- `platform/central/retention.conf` 放天數，ILM 的 JSON 用 `__變數__` 代入，少一個變數腳本直接報錯。
- metrics：14 天降成 5 分鐘 → 90 天降成 1 小時 → 400 天刪（`ilm-metrics-otel.json`）。
- log：原文 30 天；transform 每 5 分鐘統計「服務 × level × error.type」筆數，存 400 天（`aiops-log-stats`）。
- trace：隨機留下的 7 天；錯誤、5xx、例外、慢的由 pipeline `reroute` 到 `traces-problem.otel-<group>`，30 天。
- 舊資料：腳本讓既有 index 換新政策，卡在舊政策的資料流自動 rollover 一次。
- 實測紀錄在 commit `187d1c8`：真的 trace-sampler 取樣後，有問題的全進 `traces-problem`，回歸全過。

## Check：結果對不對

| 項目 | 預期 | 實際 | 證據 |
|---|---|---|---|
| 有問題的 trace 單獨留 30 天 | 進 `traces-problem` | 進了 | commit 說明、`pipeline-traces-otel.json` |
| 降解析度後延遲分布還在 | 請求數、p95 不變 | 照樣保留 | [retention.md](../retention.md) 實測 |
| AIOps 讀 trace 不受影響 | 讀得到兩種 index | `traces_index: traces-*.otel-*` 兩種都讀 | `aiops/baseline/trace.yaml`、`aiops/rca/rca.yaml` |
| 天數符合 baseline 需要 | 夠學 | **未驗證**（見下） | — |

## Act：決定與後續

- **決定**：方向可行，待合併。
- **合併前要確認**：
  1. trace baseline 現在讀 `traces-*.otel-*`，合併後會混進「全留的壞 trace」，平常值會偏。建議改讀 `traces-generic.otel-*`（只有隨機的）。**未改、未驗證**。
  2. 隨機 trace 只留 7 天，而 trace baseline 的 `min_weeks: 2`；要看是否夠用。
  3. metrics 降成 5 分鐘後，30 秒粒度的 baseline 只剩 `fine_history: 14d`，兩個數字要一起維護。
- **已知小問題**：log 的 `ilm-policy.json` 的 `max_primary_shard_size` 寫死 30gb，其他政策用 `retention.conf` 的 50gb。`traces-problem` 的 template 是從內建 `traces-otel@template` 複製而來，ES 升級後要重跑腳本。
- **下一輪要驗證**：用正式環境的量重算容量與天數（[README.md](README.md) Plan）。
