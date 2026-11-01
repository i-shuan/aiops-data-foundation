# trace 每一段每點抽 100 個請求（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/trace-sample-100`；起因見 [trace-merge 的 Act](../phase1-collect/2026-10-trace-merge.md)「trace-quality 的 6 項失敗」、[open-issues 9.2](../meta/2026-10-open-issues.md)
**模組**：④ 判斷（trace 每一段的動態基準線、例外吞噬檢測）

## Plan：這一次要達到什麼

- **需求**：trace 改 100% 收之後，平常 3% 的慢請求（1.5～3 秒）也進了基準線。每點只抽 20 個請求時，平均值飄太大：只有 10% 請求變慢 2 秒報不出來；長期 2% 例外吞噬時，20 個裡 1 個就是 5%，剛好等於規則門檻，常誤報。使用者決定調高，但要先量 `trace-detect` 一輪的耗時。
- **目的**：每點抽 100 個請求；`trace-quality` 的 trace 那段兩個案例都對；耗時在排程時限（240 秒）內。
- **範圍**：只改抽樣數。不做 p90 或慢請求比例（另列在 open-issues）。不改 `tests/trace_k3s.yaml`（`run-trace-baseline-e2e.sh` 用，測試流量小，沒重跑）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `aiops-engine/code/configs/baseline/trace.yaml` | `sample_per_bucket` 20 → 100，註解寫上限與原因 | 見 Check。100 是 ES `top_hits` 的預設上限（`index.max_inner_result_window`），200 時 ES 直接回 400 |
| `aiops-engine/code/detector/trace.py` | 設定沒寫時的預設 20 → 100 | 跟設定檔一致 |
| `aiops-engine/code/tests/trace_edge.yaml` | 20 → 100 | 這份註明「跟 trace.yaml 一樣」 |
| `docs/troubleshooting.md` | 「只有一部分請求變慢」那列改寫 | 跟著設定 |

## Check：測試的目的與結果

環境：Rancher Desktop、ES 8.19.23（兩個資料節點加一個只投票節點）。

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 耗時（一輪 `trace-detect` 讀 raw 抽樣 → 讀回這些 trace 的所有段 → 算每段統計，不寫 ES） | 測試 ES 裡三段真實資料，每段 10 分鐘窗口（`--window 10m`、interval 5m），每種抽樣數跑 3 次取中位數 | 20：0.10～0.11 秒；50：0.24～0.26 秒；100：0.29～0.49 秒（抽到 800～1,200 個請求、讀回 1,609～2,617 段）。排程時限 240 秒 |
| ES 能抽多少 | 同上試 200 | ES 回 400：`top_hits` 的 from + size 不能超過 100（`index.max_inner_result_window`） |
| 偵測對不對 | `tests.trace_quality_edge_e2e a`，同一批合成資料，只換抽樣數 | 20：14 過、3 失敗（10% 請求多 2 秒沒報；2% 例外吞噬的規則與比例都誤報）；50：17 項全過；100：17 項全過 |
| 改完整支 | `tests.trace_quality_edge_e2e`（trace 與資料品質兩段） | 38 項全過 |
| 資料品質單獨跑（open-issues 記「B8 單獨跑沒過」） | `tests.trace_quality_edge_e2e b` | 21 項全過，B8 兩項都報了。之前單跑失敗時，測試 ES 剛跑完 durability 留著大量其他 log；現在過了，跟「分母被墊大」的推測一致，但沒有直接證明 |
| 單元測試 | `pytest` | 412 個通過 |

選 100 不選 50：兩個都只跑了一次固定的亂數種子。依二項分布估算（推論，沒實測），長期 2% 例外吞噬時，單一點到 5% 門檻的機率 50 個約 8%、100 個約 5%，100 的餘裕較大。

**沒測到的**：測試 ES 只有 4～6 個 API、每個請求約 2 段；耗時大致跟「API 數 × 每個請求的段數」成正比，正式環境 API 多、trace 長，沒實測。`run-trace-baseline-e2e.sh`（`trace_k3s.yaml` 仍是 20）沒重跑。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 正式環境耗時沒量 | 測試資料 API 少 | API 很多、trace 很長時一輪可能超過 240 秒 | 上線後看排程器 `trace-detect` 的耗時；超過時降成 50 或只對部分 API 調高 |
| 慢請求比例更低時仍報不出來 | 原理：平均值看不到少數變慢 | 例如只有 2% 請求變慢 | 看 p90 或慢請求比例（open-issues 9.2，還沒做） |
| 想抽超過 100 個 | ES 預設上限 | 要改 `index.max_inner_result_window` | 目前不需要 |
