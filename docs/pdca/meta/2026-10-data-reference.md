# AIOps 資料格式規格文件（2026-10）

**狀態**：已完成　　**日期**：2026-10-31　　**相關**：分支 `claude/data-reference`
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

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 文件的欄位沒有漏寫 | 寫腳本讀五個 `*_MAPPING`，檢查每個欄位名都出現在對應章節 | 第一次：`aiops-incidents` 漏 `@timestamp`、`resolved_at`，`aiops-feedback` 漏 `@timestamp`；補上後五個索引都沒有漏 |
| 欄位的「誰寫、何時寫」寫對 | 逐一對程式：`cmd_baseline.py`、`cmd_rules.py`、`owner.py`、`cmd_alert.py`、`feedback_web/app.py`、`cmd_feedback.py` | 對出 5 處原本憑印象寫錯或不完整：`dept`／`group` 是 `owner.py` 事後補的；`warning` 不是「要累積才升級」而是不單獨開件、只併進已開的件；`category` 還有 `data_quality`；事故庫除了自動開、表單確認，還有人工補登（`incident add／resolve`）；學習儀表板第一個面板讀的是 `aiops-anomalies`，不是回饋 |
| 範例是真的 | 範例取自 ES 8.19.23 上 `loop_e2e`、`baseline_edge_e2e` 留下的文件 | 長文字截短、其餘未改；動態基準線學習的範例是 `baseline_edge_e2e` 的一筆，靜態規則、事故、事故庫、回饋、調整都是 `loop_e2e` 的 |
| 第二輪：文件跟程式是不是完全吻合（2026-10-31，被要求確認「100% 吻合」） | 寫腳本比對：欄位名與型別、列舉值（判定值、狀態、根因分類、誤報原因、kind、confidence）、相似事故權重與門檻、每個範例 JSON 的欄位與型別、儀表板查詢與欄位、告警規則的查詢與時間窗；再對 ES 8.19.23 上實際索引的 mapping；其餘敘述逐條對程式 | 腳本全部一致；但讀程式又找出 9 處文件不精確或錯誤，已修：①`rule`、`severity`、`message` 寫成「不索引」是錯的，這三個欄位由 `rules`、`peer`、`trace`、`quality` 用 `PUT _mapping` 補進索引（實際索引確認有），只有 `since` 不索引；②異常的寫入者漏了同儕橫向比對、trace、資料品質，`level` 漏了 `peer`、`quality`；③`status: warning` 的說明錯（基準線的 warning 只併進已開的件，規則 warning 與同儕才是「2 筆以上才開」）；④`category` 不只三種，還有 `namespace`、`node`、`other`，且只有 `service` 會跑根因分析、寫事故庫、出現在事故清單；⑤事故庫不是只在開的時候寫，是每輪有新異常或結束都寫，只更新還是 `auto` 的；⑥`llm_provider` 還有 `anthropic`、`openai` 與「template（…失敗）」的寫法；⑦`findings.segments` 漏 `now_calls`、`ref_calls`，`new_patterns` 漏 `example`；⑧「自己排除」不對，事件整合沒傳 `exclude_id`；⑨流程圖的模組編號（異常總表是 ⑤、事件整合是 ⑥）、`aiops-alerts` 的 `anomaly_ids` 上限 500 |
| 文件檢查 | `python3 tools/check-docs.py` | 連結、索引、舊說法、類型都是 0 個問題 |

沒測到的：`anthropic`、`openai` 兩條 LLM 路徑沒有實際呼叫（只對程式讀過）；`direction` 的 `up`、`down` 沒有實際資料；`aiops-tuning` 的 `schedule`、`effect` 只列欄位，沒有範例（目前資料裡沒有這兩種）。

## Act：Check 後沒改善的、還沒解決的

- **口頭回答裡的判定值寫錯過**：先前口頭說回饋判定有 `corrected`，實際表單送出的是 `wrong_root_cause`，`corrected` 是事故庫的狀態。文件寫的是正確的。發現於：對照 `feedback_web/app.py` 時。
- **欄位只有人工維護**：這份文件跟 mapping 之間沒有自動比對，改 mapping 忘了改文件就會再脫節。可行做法是在 `tools/check-docs.py` 加一項比對，未做。
