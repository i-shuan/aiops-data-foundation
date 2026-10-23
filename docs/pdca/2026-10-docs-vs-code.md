# 文件對照程式核對（2026-10）

**狀態**：已完成　　**日期**：2026-10-23　　**相關**：分支 `claude/feedback-pdca`

## Plan：要解決什麼

- **問題**：文件分好幾個資料夾，同一件事在多處重複（架構總覽、現況與待辦、共用 Collector、baseline），改了一處容易漏其他處，已有文件跟程式不符。
- **目標**：事實對得到程式或設定；現況與待辦只剩一份權威。
- **做法**：log、metrics、trace、rca 四組各自對照程式核對，只改事實不符，不重寫。

## Do：實際做了什麼

- 四組分別核對路徑、欄位名、index 名、設定值、狀態標記、相對連結。
- 全 repo 相對連結跑過檢查，無壞連結。
- 整併：`overview.md` 併入 `system-map.md`；現況與待辦獨立成 `docs/pdca/`。

## Check：結果對不對

找到的主要不符：

| 位置 | 文件寫的 | 程式實際 |
|---|---|---|
| trace README | 尾端取樣還沒設計，是各服務在源頭取樣 | 尾端取樣已做（`sampler.yaml`、`trace-sampler.yaml`） |
| rca cases | 跟兄弟比 ❌ | 已做（`peer`），k3s 驗過 |
| baseline README | 5 分鐘一點 | 預設 30 秒，依事件數往粗調 |
| metrics README | 三條資料流、只有 Prometheus 格式 | 四條（加 `generic`），.NET 用 OTLP 推 |
| `test/pipeline/level-cases.json` | `verbose` 原樣保留 | 程式轉成 `trace`；實際執行該案例失敗，修正後 24 個全過 |

**重複度**：跨檔逐行完全相同只有 0.8%（23／2,736 行），但同一件事用不同文字重複在四組：架構總覽、現況與待辦、共用 Collector、baseline 說明。

## Act：決定與後續

- **決定**：架構總覽只留 `system-map.md`；現況與待辦以 `docs/pdca/README.md` 為準。
- **要改的**：
  - 各資料夾 README 開頭的「現況與待辦」、`system-map.md` 第 11 節、`poc-status.md` 仍各列一份狀態，之後改成連結到 `pdca/README.md`。
  - `docs/log/testing.md`（1,130 行）是歷史紀錄，內含過時數字（53 項、`verbose`），標明「歷史紀錄」或搬到 archive。
  - `claude/retention` 合併後，同步更新 `retention.md`。
- **未核對**：文件內的實測數字（961/961、CPU、MB）只能重跑環境驗證；Kafka exporter 是否真實測過，`metrics/README` 標 ✅、`cases.md` 標 🔶，要確認；metrics、trace 現用 ES 預設保留政策，需連正式 ES 才能確認。
