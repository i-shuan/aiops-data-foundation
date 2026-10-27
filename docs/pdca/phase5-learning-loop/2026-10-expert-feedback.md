# 老師傅的回饋入口（2026-10）

**狀態**：完成（網頁＋寫入，docker ES 實測）　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-plans`；前置 [告警出口](../phase3-locate/2026-10-alert-exit.md)；[產品主線 8 步](../../architecture.md#產品主線-8-步) 第 8 步
**模組**：⑧ 出口（回饋從人回來）、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：
  - 回饋只能打指令（`python3 -m detector feedback add --anomaly … --verdict …`），值班、老師傅不會開終端機，**實際上收不到回饋**。
  - 只存「真的／誤報／根因錯」和一句備註；**老師傅怎麼判斷出根因的沒存**，第 3 步的 RAG 沒有料，人走了經驗也跟著走。
- **目的**：
  1. 收到告警的人**從通知點一下、1 分鐘內**就能回饋，不用登入、不用打指令。
  2. 除了真假，留下五樣：**根因、處置、怎麼看出來的、查了哪些地方、下次怎麼更快**。
  3. 存進現有的 `aiops-feedback`（單筆告警的判斷）和 `aiops-incidents`（一件事的經驗），格式讓第 3 步直接比對。
- **範圍**：
  - 做：回饋表單（欄位、預填、送出）；`aiops-incidents` 加欄位；告警到表單的連結；回饋後在通知下面回「已由 ○○ 標記」。
  - 不做：登入、權限（先用通知管道的身分）；回饋改門檻（[loop-update](2026-10-loop-update.md)）；LLM 幫忙填。

### 表單欄位（預填的來自告警，老師傅只填空的）

| 欄位 | 誰填 | 寫進哪 |
|---|---|---|
| 告警 id、服務、時間、異常指標、根因分析前幾名 | 預填 | — |
| **判斷**：真的／誤報／真的但根因錯／（另外入口）漏報 | 必填，一鍵 | `aiops-feedback.verdict` |
| **根因分類**（9 類，見下） | 真的時必填，下拉 | `aiops-incidents.root_cause_category`（新） |
| **根因**（一句話） | 真的時必填 | `aiops-incidents.root_cause` |
| **處置**（做了什麼讓它恢復） | 選填 | `aiops-incidents.action` |
| **怎麼看出來的**（什麼現象讓你想到這個原因） | 選填，提示要寫 | `aiops-incidents.how_detected`（新） |
| **查了哪些地方**（如 Oracle AWR、iostat、Kibana 畫面） | 選填，多選＋自由填 | `aiops-incidents.checked`（新） |
| **下次怎麼更快**（缺什麼指標、規則、畫面） | 選填 | `aiops-incidents.improve`（新） |
| 誤報時：**為什麼不是問題**（月底批次、壓測…） | 誤報時選填 | `aiops-feedback.note` |
| 誰、什麼時候 | 自動 | `by`、`@timestamp` |

- 根因分類：DB、網路、設定變更、版本更新、外部系統、資源不足、程式錯誤、資料問題、其他。
- 分類要固定：第 3 步比對要「同一類根因」，自由文字比不準。
- 「怎麼看出來的」是老師傅的經驗，RAG 最有價值的欄位。
- 「查了哪些地方」讓下次報告直接列「建議先查這裡」；「下次怎麼更快」變成補序列、補規則的待辦。
- 誤報原因累積後用來提排除時段、行事曆。
- `by` 用來防污染：之後依人加權、要第二個人確認。

### 入口比較

| 做法 | 怎麼運作 | 好處 | 代價 |
|---|---|---|---|
| A. 通知裡的按鈕＋卡片 | Teams Adaptive Card、Slack Block Kit | 最快、不離開聊天軟體 | 綁定聊天軟體；要 bot、對外回呼網址 |
| **B. 通知裡放連結 → 小網頁（採用）** | cluster 裡跑小服務，表單寫進 ES | 不管通知走哪個管道都一樣 | 多一個服務要部署、要內網網址 |
| C. Kibana 的表單 | Kibana 畫面讓人填 | 不用寫服務 | 不適合做表單；老師傅不一定有帳號 |

- **決定**：B。通知管道還沒定，B 不受影響；之後做 A，按鈕也打同一個網址。
- 先不做登入：內網、表單裡填名字。

### 驗收

1. 從通知點連結，到送出「真的＋根因分類＋一句根因」，**1 分鐘內**（找兩三個值班實際試）。
2. 送出後 `aiops-feedback`、`aiops-incidents` 各一筆，欄位齊；通知下面回「已標記」。
3. 同一筆告警第二個人再標：兩筆都留，以最後一筆為準（現有 `summarize` 的規則）。
4. 跑兩週後：有回饋的告警比例（`feedback summary` 的「已標記」）≥ 50%；「怎麼看出來的」有填的比例另外統計。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/feedback_web/__init__.py`、`__main__.py`（新） | 標準函式庫 `http.server`；路由見下 | 不另外裝套件；跟其他指令同一個 image |
| `engine/feedback_web/app.py`（新）`form_data` | 從 `aiops-alerts` 預填標題、摘要、LLM 摘要、相似事故、報告連結 | 老師傅不用回頭查 |
| 同上 `submit` → `aiops-feedback` | **一件的每筆異常各一筆**，帶 `alert_id`、`series`、`keys` | 現有 `feedback summary／suggest` 依序列＋對象算，才對得起來 |
| 同上 `submit` → `aiops-incidents` | 更新狀態與五樣東西、`confirmed_by/at`；事故還沒開就 upsert | 事故要 `confirmed`，相似事故檢索才會用 |
| 同上 | 寫入前先用正式 mapping 建 index | 見 Check 的 mapping 那列 |
| 表單欄位（`render_form`） | 見下 | Plan 的表 |
| `engine/detector/feedback.py` `INCIDENT_MAPPING` | 加 10 個欄位（見下） | 要存的欄位 |
| 同上 `FEEDBACK_MAPPING` | 加 `alert_id`、`alert_started_at`、`fp_reason` | 同上 |
| 同上 `ROOT_CAUSE_CATEGORIES` | 9 類 | 分類固定才比得準 |
| `engine/investigator/cmd_alert.py`、`configs/rca/rca.yaml` | 每件寫 `feedback_url`（`alerts.feedback_url`，含 `{alert_id}`） | 通知裡要有連結 |
| 同上 | 新的一件同時寫一筆 `aiops-incidents`（`status: auto`，帶指紋），**已確認的不覆蓋** | 事故一開始就存在，老師傅只是改狀態 |
| `engine/deploy/feedback-web.yaml` | Deployment＋Service | 要有地方跑 |
| `engine/tests/test_feedback_web.py`（新） | 7 個情境，見 Check | — |

- `INCIDENT_MAPPING` 新欄位：`alert_id`、`dept`、`group`、`root_cause_category`、`how_detected`、`checked`、`improve`、`confirmed_at`、`confirmed_by`、`fingerprint`。
- 路由：`GET /feedback?alert=<id>` 表單；`POST /feedback`（表單）、`/api/feedback`（JSON）；`GET /report?alert=<id>` 報告全文；`/health`。
- 環境變數：`ES_URL`、`ES_API_KEY`、`FEEDBACK_PORT`。
- 判斷對應的事故狀態：`true_positive → confirmed`、`wrong_root_cause → corrected`、`false_positive → false_positive`。
- 表單：一鍵判斷（真的／誤報／真的但根因錯）；名字必填。
- 真的時：根因分類（下拉 9 類）、根因、處置、**怎麼看出來的**（提示「這是最值錢的」）、查了哪些地方（10 個勾選）、下次怎麼更快。
- 誤報時：為什麼不是問題（月底批次、壓測、本來就慢、量太少、換版、其他）。

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 從通知點進來就能填 | `loop_e2e`（docker ES 9.5.5、模擬資料）：第 1 件開了後 `GET /feedback?alert=…` | 200；有那件的標題、摘要、「怎麼看出來的」欄位 |
| 報告頁 | 同上，`GET /report` | 有報告全文 |
| 五樣東西存進去 | `POST /api/feedback`：真的＋db＋根因＋處置＋怎麼看出來的＋查 2 處＋下次怎麼更快 | 事故 `confirmed`、`root_cause_category: db`、`how_detected` 有 AWR、`confirmed_by: 老王` |
| 每筆異常各一筆回饋 | 同上 | `aiops-feedback` 筆數 = 異常數（3 筆） |
| 件結束不蓋掉老師傅的 | 確認後再跑 alert，讓那件 `resolved` | 事故仍 `confirmed`、根因分類還在 |
| 誤報原因留給 suggest | 3 件批次各標誤報（`fp_reason: batch`） | `feedback suggest` 算到同一對象誤報 3 次（見 [loop-update](2026-10-loop-update.md)） |
| 第一筆寫入不讓 mapping 壞掉 | `aiops-feedback` 還不存在時由網頁先寫 | 沒先建時 `anomaly_id` 被猜成 text、`ensure_index` 回 400；先建 index 後通過 |
| 邏輯不連 ES 也能測 | `test_feedback_web.py` 用假 ES | 7 個情境如預期 |

- 7 個情境：預填、報告頁、確認寫幾筆、誤報存原因不存根因、根因錯存正確根因、異常過期仍留一筆、錯的輸入不寫。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 沒有登入 | 設計入口時 | 防不了亂填；內網、連結只在通知裡，先這樣 | 放在公司 SSO 的反向代理後面，`by` 改讀 header |
| 「回饋後在通知下面回一則」沒做 | Plan 範圍有寫 | 通知裡看不到已有人標記 | Grafana 通知是單向的；用 annotation 或另發一則，看通知管道再定 |
| 還沒有真人填過 | 欄位與 10 個「查了哪裡」選項是估的 | 欄位可能不合用 | 試點看第一週填了什麼再改 |
| [⑨] 漏報沒有網頁入口 | 漏報沒有告警可以點 | 仍只能 `feedback add --verdict missed` | 網頁加「回報漏報」頁 |
| LLM 摘要、相似事故可能帶著人走（anchoring） | 表單裡給人參考 | 根因判斷被帶偏 | 觀察；考慮預設收合 |

- **2026-10-26 補**：表單也可以放在 Grafana 裡（[grafana-form](2026-10-grafana-form.md)），寫回 ES 的還是這個服務。
