# 老師傅的回饋入口（2026-10）

**狀態**：完成（網頁＋寫入，docker ES 實測）　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-plans`；前置 [告警出口](2026-10-alert-exit.md)；主線 [product-loop](2026-10-product-loop.md) 第 8 步
**模組**：⑧ 出口（回饋從人回來）、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：回饋現在只能打指令（`python3 -m detector feedback add --anomaly … --verdict …`）。值班、老師傅不會去開終端機，所以**實際上收不到回饋**。而且現在只存「真的／誤報／根因錯」和一句備註，**老師傅最值錢的東西沒存**：他是看到什麼、查了哪裡、怎麼判斷出根因的。這段經驗不留下來，第 3 步的 RAG 就沒有料，人走了經驗也跟著走。
- **目的**：
  1. 收到告警的人，**從通知點一下、1 分鐘內**就能回饋，不用登入任何系統、不用打指令。
  2. 除了真假，留下五樣東西：**根因、處置、怎麼看出來的、查了哪些地方、下次怎麼更快**。
  3. 存進現有的 `aiops-feedback`（對單筆告警的判斷）和 `aiops-incidents`（一件事的經驗），格式要讓第 3 步直接拿來比對。
- **範圍**：
  - 做：回饋表單（欄位、預填、送出）；`aiops-incidents` 加欄位；從告警到表單的連結；回饋後在通知下面回一則「已由 ○○ 標記為真的」。
  - 不做：登入、權限（先用通知管道本身的身分）；回饋改門檻（那是 [loop-update](2026-10-loop-update.md)）；LLM 幫忙填（之後）。

### 表單長什麼樣（預填的都來自告警，老師傅只填空的）

| 欄位 | 誰填 | 寫進哪 | 為什麼要 |
|---|---|---|---|
| 告警 id、服務、時間、異常的指標、根因分析的前幾名 | 預填 | — | 讓人不用回頭查 |
| **判斷**：真的／誤報／真的但根因報錯了／（另外入口）漏報 | 必填，一鍵 | `aiops-feedback.verdict` | 誤報率、門檻調整 |
| **根因分類**：DB、網路、設定變更、版本更新、外部系統、資源不足、程式錯誤、資料問題、其他 | 真的時必填，下拉 | `aiops-incidents.root_cause_category`（新） | 第 3 步比對要「同一類根因」，自由文字比不準 |
| **根因**（一句話） | 真的時必填 | `aiops-incidents.root_cause` | |
| **處置**（做了什麼讓它恢復） | 選填 | `aiops-incidents.action` | 下次直接照做 |
| **怎麼看出來的**：看到什麼現象讓你想到是這個原因 | 選填，**提示要寫** | `aiops-incidents.how_detected`（新） | 老師傅的經驗：這是 RAG 最有價值的欄位 |
| **查了哪些地方**：例如 Oracle AWR、某台主機的 iostat、某個 Kibana 畫面 | 選填，可多選＋自由填 | `aiops-incidents.checked`（新） | 下次報告可以直接列「建議先查這裡」 |
| **下次怎麼更快**：缺什麼指標、什麼規則、什麼畫面 | 選填 | `aiops-incidents.improve`（新） | 變成補序列、補規則的待辦 |
| 誤報時：**為什麼不是問題**（月底批次、壓測、已知慢的報表…） | 誤報時選填，可選常見原因 | `aiops-feedback.note` | 累積後提排除時段、行事曆 |
| 誰、什麼時候 | 自動 | `by`、`@timestamp` | 防污染：之後依人加權、要第二個人確認 |

### 入口要選一種（要你決定）

| 做法 | 怎麼運作 | 好處 | 代價 |
|---|---|---|---|
| A. 通知裡的按鈕＋卡片表單（Teams Adaptive Card、Slack Block Kit） | 在聊天軟體裡直接填 | 最快、不離開聊天軟體 | 綁定那個聊天軟體；要設定 bot、對外的回呼網址 |
| B. 通知裡放連結 → 一個小網頁表單 | 我們在 cluster 裡跑一個小服務（例如 FastAPI），表單寫進 ES | 不管通知走哪個管道都一樣；表單長什麼樣自己決定 | 多一個服務要部署、要有內網網址 |
| C. Kibana 的表單 | 用 Kibana 的畫面讓人填 | 不用寫服務 | Kibana 不適合做表單；老師傅不一定有帳號 |

**建議 B**：通知管道還沒定，B 不受影響；之後要做 A，按鈕也是打同一個網址。

### 驗收

1. 從通知點連結，到送出「真的＋根因分類＋一句根因」，**1 分鐘內**（找兩三個值班實際試）。
2. 送出後 `aiops-feedback`、`aiops-incidents` 各一筆，欄位齊；通知下面回「已標記」。
3. 同一筆告警第二個人再標：兩筆都留，以最後一筆為準（現有 `summarize` 的規則）。
4. 跑兩週後：有回饋的告警比例（`feedback summary` 的「已標記」）≥ 50%；「怎麼看出來的」有填的比例另外統計（這是經驗有沒有留下來的指標）。

## Do：改動哪些 code、改了什麼、為何要改

入口選 **C. 小網頁**（Plan 的表）：不綁通知管道，Teams、LINE、Email 的通知都只是帶一個連結；先不做登入（內網、表單裡填名字）。

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/feedback_web/__init__.py`、`__main__.py`（新） | 標準函式庫 `http.server`：`GET /feedback?alert=<id>` 表單、`POST /feedback`（表單）／`/api/feedback`（JSON）、`GET /report?alert=<id>` 報告全文、`/health`；環境變數 `ES_URL`、`ES_API_KEY`、`FEEDBACK_PORT` | 不另外裝套件；跟其他指令同一個 image |
| `engine/feedback_web/app.py`（新） | `form_data`：從 `aiops-alerts` 預填標題、摘要、LLM 摘要、相似事故、報告連結。`submit`：寫 `aiops-feedback`（**一件的每筆異常各一筆**，帶 `alert_id`、`series`、`keys`）、更新 `aiops-incidents`（`true_positive → confirmed`、`wrong_root_cause → corrected`、`false_positive → false_positive`；存根因分類、根因、處置、怎麼看出來的、查了哪裡、下次怎麼更快、`confirmed_by/at`；事故還沒開出來就 upsert 一筆）；先用正式 mapping 建 index | 每筆異常一筆，現有的 `feedback summary／suggest`（依序列＋對象算）才對得起來；事故要變 `confirmed` 相似事故檢索才會拿來用 |
| 表單欄位（`render_form`） | 一鍵：真的／誤報／真的但根因錯；真的時：根因分類（下拉 9 類）、根因一句話、處置、**怎麼看出來的**（提示「這是最值錢的」）、查了哪些地方（10 個勾選）、下次怎麼更快；誤報時：為什麼不是問題（月底批次、壓測、本來就慢、量太少、換版、其他）；名字必填 | Plan 的表；老師傅只填空的 |
| `engine/detector/feedback.py` | `INCIDENT_MAPPING` 加 `alert_id`、`dept`、`group`、`root_cause_category`、`how_detected`、`checked`、`improve`、`confirmed_at`、`confirmed_by`、`fingerprint`；`FEEDBACK_MAPPING` 加 `alert_id`、`alert_started_at`、`fp_reason`；`ROOT_CAUSE_CATEGORIES` 9 類 | 要存的欄位；分類固定才比得準 |
| `engine/investigator/cmd_alert.py`、`configs/rca/rca.yaml` | 每一件寫 `feedback_url`（`alerts.feedback_url`，含 `{alert_id}`）；新的一件同時寫一筆 `aiops-incidents`（`status: auto`，帶指紋），**已確認的不覆蓋** | 通知裡要有連結；事故一開始就存在，老師傅只是改狀態 |
| `engine/deploy/feedback-web.yaml` | Deployment＋Service | 要有地方跑 |
| `engine/tests/test_feedback_web.py`（新） | 7 個：預填、報告頁、確認寫幾筆、誤報存原因不存根因、根因錯存正確根因、異常過期仍留一筆、錯的輸入不寫 | |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 從通知點進來就能填、不用查 | `loop_e2e`（docker ES 9.5.5、模擬資料）：第 1 件開了後 `GET /feedback?alert=…` | 200；表單裡有那件的標題、摘要、「怎麼看出來的」欄位；`/report` 有報告全文 |
| 五樣東西存進去 | `POST /api/feedback` 真的＋db＋根因＋處置＋怎麼看出來的＋查了 2 個地方＋下次怎麼更快 | `aiops-incidents` 那件變 `confirmed`，`root_cause_category: db`、`how_detected` 有 AWR、`confirmed_by: 老王`；`aiops-feedback` 寫了跟異常數一樣多的筆數（3 筆） |
| 件結束不會蓋掉老師傅的 | 確認後再跑 alert 讓那件 `resolved` | 事故仍 `confirmed`、根因分類還在 |
| 誤報的原因留下來、給 suggest 用 | 3 件批次各標誤報（`fp_reason: batch`） | `feedback suggest` 算到同一個對象誤報 3 次（見 [loop-update](2026-10-loop-update.md)） |
| 第一筆寫入不能讓 mapping 壞掉 | 第一次跑時 `aiops-feedback` 還不存在 | **第一次發現問題**：網頁先寫，ES 把 `anomaly_id` 猜成 text，之後 `feedback suggest` 的 `ensure_index` 400。改成網頁寫入前先用正式 mapping 建 index，重跑過 |
| 邏輯不連 ES 也能測 | `test_feedback_web.py` 用假 ES | 7 個情境都如預期 |

## Act：Check 後沒改善的、還沒解決的

- **沒有登入**（在什麼情況下發現：設計入口時）。內網、連結只在通知裡，先這樣；要防亂填時最省的是放在公司 SSO 的反向代理後面，表單的 `by` 改讀 header。
- **2026-10-26 補：表單也可以放在 Grafana 裡**（[grafana-form](2026-10-grafana-form.md)），寫回 ES 的還是這個服務。
- **「回饋後在通知下面回一則」沒做**（Plan 的範圍有寫）：Grafana 的通知是單向的；要做要在 Grafana 用 annotation 或另發一則，之後看通知管道再決定。
- **還沒有真人填過**：欄位是照老師傅會講的話設計的，10 個「查了哪裡」的選項是估的；試點時看第一週填了什麼再改。
- **漏報沒有網頁入口**：漏報沒有告警可以點，現在仍只能 `feedback add --verdict missed`；之後在網頁加一個「回報漏報」頁。
- LLM 摘要、相似事故在表單裡是給人參考，**人看了會不會被它帶著走**（anchoring）要觀察：之後可以考慮預設收合。
