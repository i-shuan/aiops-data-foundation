# AIOps 資料格式規格文件（2026-10）

**狀態**：進行中　　**日期**：2026-10-31　　**相關**：分支 `claude/data-reference`
**模組**：④ 判斷、⑤ 異常總表、⑥ 收斂、⑦ 根因分析、⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：問「事故清單、LLM 摘要、相似事故各拿什麼資料、格式長怎樣」時，repo 的 md 答不出來：`aiops-alerts`、`aiops-anomalies`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning` 的欄位只在程式的 mapping 裡，md 只有零散提到幾個欄位名。
- **目的**：有一份規格文件，能查到每個索引的每個欄位：型別、誰在什麼階段寫、真實範例；改欄位時有地方對照。
- **範圍**：做——新增 `docs/aiops-data-reference.md`（規格），更新 `docs/README.md`、`docs/pdca/README.md`。不做——不改程式、不改 mapping；不寫 log／metrics／trace 的欄位（已有 `docs/log/reference.md` 等）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `docs/aiops-data-reference.md`（新） | 五個索引的欄位表、寫入時機、真實範例、面板與欄位的對照 | 見需求 |
| `docs/README.md` | 在總覽加一列 | 新增 md 要同 commit 更新索引 |
| `docs/pdca/README.md` | 加本份 | 同上 |

無 code 改動。欄位內容取自程式（各 `*_MAPPING`、寫入的函式），範例取自 k3d 旁 ES 8.19.23 上 `loop_e2e` 跑出來的真實文件。

## Check：測試的目的與結果

（寫完文件後補：欄位表對程式的核對、範例的真實性、`tools/check-docs.py`。）

## Act：Check 後沒改善的、還沒解決的

（待補。）
