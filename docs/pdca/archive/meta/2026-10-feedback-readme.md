# feedback/README.md 縮成閉環步驟與介面（2026-10）

**狀態**：完成　　**日期**：2026-10-31　　**相關**：分支 `claude/feedback-readme`（從 master `78a9025` 開出）
**模組**：⑨ feedback

## Plan：這一次要達到什麼

- **需求**：`docs/feedback/README.md` 有 LLMOps 的理由、紀律、LLM 何時叫與 token、現況表，讀的人要的是「回饋怎麼走、有哪些介面、寫到哪」。
- **目的**：只留三塊：類型標示、`## 閉環步驟`（怎麼走、回饋結果與用法）、feedback 有哪些介面（放 `grafana-form.png`，說明跟 `aiops-*` 索引欄位的關係）。
- **範圍**：只改這一份。`aiops-data-reference.md` 不動；被刪掉的段落（LLMOps、紀律、LLM、現況）在 git 歷史，不搬到別處。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `docs/feedback/README.md` | 由 102 行（約 1500 字）改成：閉環步驟（兩張表與 `backtest` 一句）、介面（圖、三種介面、表單選項對應 `verdict` 與 `incidents.status`、建議與漏報寫到哪） | 照使用者指定的三塊；介面與索引的對應用 `engine/feedback_web/app.py` 的程式確認（`true_positive`→`confirmed`、`wrong_root_cause`→`corrected`、`false_positive`→`false_positive`） |
| `docs/pdca/README.md` | 加一列 | 索引 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 沒有斷連結、索引完整 | `python3 tools/check-docs.py` | 四項都是 0 個問題 |
| 介面描述與程式一致 | 讀 `engine/feedback_web/__main__.py`（`GET /api/alert`、`POST /api/feedback`、`GET /feedback`、`GET /report`、`FEEDBACK_CORS_ORIGIN`）、`engine/detector/__main__.py` 的子指令、`cmd_feedback.py` 的 `suggest`（誤報與漏報都寫 `proposed`） | 一致 |

- **沒測到的**：沒有在 Grafana 上重新送一次表單確認圖與現在的表單一致（圖是 2026-10-27 的截圖）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 刪掉的內容沒有搬到別處：LLMOps 的決定與理由、LLM 何時叫與 token 估計、現況與沒做的（版本、評測集、基準） | 照使用者指定「其餘的都刪掉」 | 沒做的項目不再有一份清單 | `docs/architecture.md` 第 10 節有「還沒做」；LLM 的實作在 `engine/core/llm.py`，其餘在 git 歷史 |
