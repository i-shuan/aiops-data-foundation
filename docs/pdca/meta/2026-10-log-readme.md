# log/README.md 改成流程圖加四個重點（2026-10）

**狀態**：完成　　**日期**：2026-10-31　　**相關**：分支 `claude/log-readme`（從 master `7e3f458` 開出）
**模組**：①② 收值、清資料；③ 存

## Plan：這一次要達到什麼

- **需求**：`docs/log/README.md` 的「資料怎麼走」是一段文字，讀不出流程；使用者要的是 log 樣式格式有哪些、什麼時候清洗、怎麼清洗、多久壓縮。
- **目的**：資料怎麼走改畫流程圖；其餘章節只回答上面四件事，其餘刪掉。
- **範圍**：只改這一份。被刪的段落（Fluent Bit 與 OTel 的比較、部署與換 key 的操作、現況與沒做的）在 git 歷史。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `docs/log/README.md` | 開頭是 mermaid 流程圖（應用 → 節點 Collector → 中央 ES 各 pipeline → index → transform）；「log 樣式格式有哪些」（`default`、`logfmt` 已有規則，SECS、REST API 沒有；認得的欄位名稱表）；「什麼時候清洗、怎麼清洗」（階段表加兩個前後對照的例子）；「多久壓縮」（寫入時、rollover、30 天刪、5 分鐘統計、400 天）；「共同鑰匙圈與 log 自己的欄位」（鑰匙在 log 實際存在哪、歸屬 label 與鑰匙的差別、只在 log 有的欄位與 label） | 內容對照 `telemetry/collectors/node.yaml`（`file_log`、processors）、`telemetry/storage/` 的 `pipeline*.json`、`pipeline-*.painless`、`formats/*.json`、`ilm-policy.json`、`component-settings.json`、`component-mappings.json`（alias）、`transform-log-stats.json`、`retention.conf`；鑰匙與歸屬 label 的對照取自合併前 `docs/rca/README.md`、`docs/log/reference.md`（git 歷史），再對 mapping 核對 |
| `docs/pdca/README.md` | 加一列 | 索引 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 沒有斷連結、索引完整 | `python3 tools/check-docs.py` | 四項都是 0 個問題 |
| 欄位名稱表與 pipeline 一致 | 對 `pipeline-fields.painless` 的 `takeStr` 清單逐項核對 | 一致 |
| 壓縮與保留天數與設定一致 | 對 `component-settings.json`（`index.codec: best_compression`）、`ilm-policy.json`（rollover `30gb`／`ROLLOVER_MAX_AGE` 1d、`LOGS_DELETE_AFTER` 30d）、`transform-log-stats.json`（`frequency: 5m`）、`ilm-aiops-log-stats.json`（30d rollover、400d 刪） | 一致 |

- **沒測到的**：兩個「清洗前後」的例子是照 pipeline 程式推出來的，沒有在 ES 8.19 上實際送一行驗證；mermaid 圖沒有在 GitHub 上看呈現。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 沒有實際送一行 log 驗證例子 | 寫例子時 | 例子可能跟實際輸出有出入（例如多一個欄位） | 在 k3d 或 Rancher 的 ES 8.19 送一行 JSON、一行純文字，對照文件 |
| 被刪掉的內容沒有搬到別處：部署與換 key 的操作、Fluent Bit 與 OTel 的比較結果、現況與沒做的清單 | 照使用者指定四個重點 | 換 key、Secret 的坑要查 `troubleshooting.md` 與 `telemetry/scripts/rotate-writer-key.sh` | 無 |
