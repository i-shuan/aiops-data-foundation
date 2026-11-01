# 根因分析：沒有 trace 的件自動重跑（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/rca-retry-no-trace`；[aiops-engine/code/configs/rca/rca.yaml](../../../aiops-engine/code/configs/rca/rca.yaml)
**模組**：⑥ 收斂、⑦ 根因分析

## Plan：這一次要達到什麼

- **需求**：實跑時報告是空的，LLM 摘要還寫「trace 沒看到變慢」。根因分析只在開件跑一次，開件當下 trace 還沒合成完。
- **目的**：沒有 trace 的件等 trace 到了重跑；還沒就緒時不能說「沒變慢」。
- **範圍**：做：重跑、摘要措辭。不做：開件後又注入新事故時重跑（見 Act）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `investigator/cmd_alert.py` | `has_trace`、`needs_rerun`，欄位 `rca_attempts`；沒有 trace 的件每 `rca_retry_after` 重跑，最多 `rca_retry_max` 次；重跑還是沒有 trace 就不換報告、不叫 LLM | trace 合成晚 1～2 分鐘；服務本來沒埋點時不能永遠重跑 |
| `core/llm.py` | 輸入包多 `trace_ready`，PROMPT 與範本都分開「還沒就緒」和「沒變慢」 | 沒資料不等於沒變慢 |
| `investigator/__main__.py` | 沒有 trace 的說明補上重跑 | 報告講清楚 |
| `configs/rca/rca.yaml` | `rca_retry: true`、`rca_retry_after: 2m`、`rca_retry_max: 5` | 可調 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 重跑邏輯 | 單元測試 `RerunWithoutTrace`、`test_llm` | 全部 541 個測試通過 |
| 真實的件會重跑 | 對測試 ES 上的 `a-20261101-1244-rca-mes-api` 跑 `investigator alert` | 印出 🔁，`rca_attempts` 2，報告 trace 請求數 195 |
| 基準乾淨時根因指得對 | 故障解除 18 分鐘後注入情境 1（13:13:33Z），`investigator investigate` 查 13:14～13:20 | 慢在 `rca-wip-worker › SELECT LOT_HISTORY → oracle`（1.50 s，平常 32 ms，+1.47 s）；錯誤最早出在同一段（50 個請求）；新 log `Connection pool timeout`（176 次）；都是注入的根因 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 同一件內的第二次事故不重跑 | 13:13 注入時 12:44 的件還開著（`close_after: 30m`），新異常併進舊件，報告停在舊的 | 30 分鐘內復發時，報告是上一次的內容；這次根因是手動跑 `investigate` 得到的 | 另外決定：併入時間隔夠長就重跑 |
| 基準線被前一次事故污染 | 第一輪上一個故障沒解除 | 參考段本身就慢，比不出差異 | 操作上解除故障後等 20 分鐘 |
