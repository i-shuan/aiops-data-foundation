# 相似事故檢索：RAG 的「檢索」（2026-10）

**狀態**：完成（欄位比對版，docker ES 實測；舊事故匯入格式見 Act）　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-plans`；前置 [老師傅回饋入口](2026-10-expert-feedback.md)；主線 [product-loop](2026-10-product-loop.md) 第 7 步
**模組**：⑦ 根因分析、⑨ feedback（知識庫）

## Plan：這一次要達到什麼

- **需求**：根因報告每次都從零開始。過去同樣的事（同一個服務、同一段變慢、同一種錯）老師傅處理過、知道原因和做法，但 `investigator` **完全沒讀 `aiops-incidents`**，報告不會說「這跟上次很像」。值班只能靠記憶或去問人。
- **目的**：新的一件告警，報告最前面附上**最像的 3 件過去事故**：當時的根因、處置、誰處理的、怎麼看出來的，以及**為什麼覺得像**（哪幾點相同）。像的程度不夠就不附，不硬湊。
- **範圍**：
  - 做：事故的「指紋」（要存哪些結構化欄位）；根因分析輸出指紋；用 ES 檢索、打分；報告加一段；**舊事故單匯入的格式**（現在就能開始收）；評測方法。
  - 不做：LLM 寫摘要（[llm-summary](2026-10-llm-summary.md)）；向量資料庫、embedding（先用看得懂的欄位比對，不夠再加）。

### 為什麼先不用向量檢索

| | 欄位比對（ES 的 bool／terms／BM25） | 向量檢索（embedding） |
|---|---|---|
| 能不能解釋為什麼像 | 能：「同服務、同一段變慢、同一種錯」 | 很難：只有一個相似分數 |
| 要不要模型 | 不用 | 要 embedding 模型（內網可不可以連外？） |
| 20～30 件資料夠不夠 | 夠 | 量小時優勢不明顯 |
| 文字描述很亂時 | 弱 | 強 |

先做欄位比對，評測後如果自由文字（根因、怎麼看出來的）比對不夠準，再加向量欄位。

### 事故的指紋（要存的東西）

一件事故（自動產生的告警、或人補登的舊事故）存這些，**舊事故單要匯入也是這個格式**：

| 欄位 | 從哪來 | 比對權重（先估） |
|---|---|---|
| 服務（可多個）、入口 API | 告警 | 高：同服務＋同 API |
| 異常的指標組合：例如 `{p95, error_rate, peer.http_error_rate}` | 告警裡的異常 series | 中 |
| **慢在哪一段**：例如 `mes-api → Oracle SELECT lots` | 根因分析（現在只在 Markdown 報告裡，要改成也輸出結構化結果） | 高 |
| **錯從哪開始**：例如 `wip-worker → Kafka produce` | 根因分析 | 高 |
| 新出現的 log 樣式（前 5 個） | 根因分析 | 中 |
| 根因分類、根因、處置、怎麼看出來的、查了哪些地方 | 老師傅回饋 | 檢索結果要顯示的內容；根因分類也用來評測 |
| 時間、持續多久、嚴重度、dept、group | 告警 | 只用來顯示和過濾（同一個 group 優先） |
| 狀態：自動／已確認／根因錯（已更正） | 回饋 | 只檢索「已確認」和「已更正」的；未確認的不拿來教人 |

### 舊事故單要怎麼收（可以現在就開始）

格式放在 `engine/interfaces/`（跟慢性病、行事曆一樣：接口＋假資料分開）：一件一列，**最少要有**：時間、服務、現象（一兩句）、根因分類、根因、處置；**有更好**：慢的那段、錯的源頭、怎麼看出來的。舊事故沒有 trace 指紋也可以，比對時只用有的欄位。

### 驗收（評測）

1. **留一法**：每件已確認的事故輪流當「新的」，用它的指紋去查其他的，看**同一個根因分類的事故有沒有排在前 3**（recall@3）。目標先訂 70%（估的，看資料再調）。
2. 報告上的「為什麼像」每一點都對得到兩邊的欄位（不能說同一段變慢但其實不同）。
3. 沒有像的時候（分數低於門檻）不附，評測集裡放幾件「沒有前例」的事故驗證這點。
4. 示範：k3s 造兩件同根因的事故（例如 DB 慢），第二件的報告最前面出現第一件。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/investigator/__main__.py` | `investigate` 拆成 `investigate_full(cfg, args) → (報告, findings)`：`findings` 是結構化結果（`anomalies`、`segments`、`error_origins`、`new_patterns`、`topology`、`requests`）；`alert` 改用它；新增 `similar-eval` 子指令 | 指紋要「慢在哪一段、錯從哪開始」，以前只在 Markdown 裡 |
| `engine/investigator/similar.py`（新） | `fingerprint(alert, findings)`：服務、API、指標組合、慢的那段（前 3，SQL 裡的數字換成 `#`）、錯誤源頭（前 3）、新 log 樣式（前 5）。`query`：ES bool／terms／match，只檢索 `status` confirmed／corrected。`explain`：**自己算分**（每個欄位有交集就加權重：慢段 4、錯誤源頭 4、服務 3、API 2、log 樣式 2、指標 1）並寫出交集的內容。`search`：分數 ≥ 4 的前 3 件。`format_similar`：報告那一段。`evaluate`：留一法 recall@k | 分數自己算才解釋得出「為什麼像」；權重先估 |
| `engine/investigator/cmd_alert.py` | 新的一件：`investigate_full` → `search` → 相似事故附在報告最前面、存 `similar`、`findings`；摘要多一行「過去相似：…」；同時寫一筆 `aiops-incidents`（`status: auto`、帶 `fingerprint`） | 告警一出來就查；事故的指紋在產生時就存好 |
| `engine/investigator/alerts.py` | `summary(..., similar)` 多一行 | 通知裡就看得到上次的根因、處置 |
| `engine/investigator/es.py` | `anomalies()` 回傳 `_id` | 指紋要對回異常 |
| `engine/detector/feedback.py` | `INCIDENT_MAPPING` 加 `fingerprint`（6 個 keyword／text 欄位）、`root_cause_category` | 查得到 |
| `engine/configs/rca/rca.yaml` | `alerts.similar: true` | 可以關 |
| `engine/tests/test_similar.py`（新） | 9 個：指紋（去數字）、沒有 findings、explain 只列真的相同的欄位、只檢索已確認、分數不到不附、排除自己、留一法、格式 | |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 第 2 次的報告最前面出現第 1 次（Plan 驗收 4） | `loop_e2e`：mes-api 延遲 ×4 兩次（隔一天），第 1 次老師傅確認 db | 第 2 件的 `similar[0]` 就是第 1 件；報告有「## 過去相似的事故」；通知摘要有「過去相似：2026-10-05 [db] Oracle 統計資訊過期…；處置：重跑 analyze」 |
| 「為什麼像」每一點對得到兩邊（驗收 2） | 看 `why` | 「同服務（mes-api）；同樣的指標異常（synth.latency）」，兩邊的指紋確實都有；單元測試另驗：路由不同就不會說「同一支 API」 |
| 沒有前例就不附（驗收 3） | 第 1 件（知識庫空的）；單元測試：只有同服務（3 分）的事故 | 第 1 件 `similar` 空；3 分的不附 |
| 未確認的不拿來教人 | 單元測試：同指紋但 `status: auto` | 不出現 |
| 留一法 recall@3（驗收 1） | `similar-eval`：3 件 db 事故 | 3/3；但這 3 件是同一種模擬，**不能當成 70% 達標的證據**（見 Act） |
| 第 3 件附 2 件 | 同上 | 附 2 件，分數都 4.0 |

## Act：Check 後沒改善的、還沒解決的

- **模擬資料沒有 trace，指紋只有服務＋指標**（在什麼情況下發現：loop_e2e 的分數都剛好 4.0 ＝ 同服務 3 ＋ 同指標 1，正好壓在門檻上）。慢的那段、錯誤源頭這兩個「強特徵」在 k3s 的 rca e2e 才有；**門檻 4 和權重都還是估的**，要用有 trace 的事故（k3s 造兩種不同根因）再看一次：同服務不同根因的事故會不會被當成像。
- **recall@3 = 3/3 沒有意義**：3 件長得一樣。真正的評測要 20～30 件真實事故，含幾件「沒有前例」的。
- **舊事故單的匯入格式還沒寫**（Plan 範圍有）：欄位就是 `aiops-incidents` 的（`title`、`start`、`end`、`services`、`root_cause_category`、`root_cause`、`action`、`how_detected`、`checked`、`fingerprint.{services,routes,series,slow_segments,error_origins,patterns}`、`status: confirmed`、`by`），一件一筆 JSON 直接 bulk 進去就能被查到；要補一個 `interfaces/incidents.md` 和一支匯入指令。
- **相似事故的時間顯示 UTC**（`start[:16]`）：報告、LLM 摘要裡是 `2026-10-05T02:05`，老師傅看的是台北時間，要轉。
- 指紋裡的 `patterns` 用 `match`（BM25），其他用 `terms`；log 樣式字數多時 `match` 容易「部分像」就加 2 分，之後看誤判再改成 `match_phrase`。
