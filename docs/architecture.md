# it-aiops 系統架構規格書：架構、設計決策與現況 (Architecture Specification)

> **類型：原理 Explanation**

本文件定義 it-aiops（IT 系統的 AIOps 平台）的範圍、架構、關鍵決策與模組職責（第 1～5 節，只放既定設計），並記錄各 phase、各模組的現況、驗證、還沒做的與 PDCA 紀錄索引（第 6～14 節，隨實測更新）。目前以 PoC 驗證端到端可行性：從資料打通、異常辨識到 RCA 說明，並以回饋閉環持續校正。狀態依實測結果，不用完成比例；重點是確認每個模組所需的資料、分析方法與輸出結果。模組定義見第 4 節。

**目錄**

<!-- toc -->
- [1. 系統目標與邊界 (System Overview & Scope)](#1-系統目標與邊界-system-overview--scope)
- [2. 全域架構圖 (End-to-End Architecture)](#2-全域架構圖-end-to-end-architecture)
- [3. 關鍵架構決策 (Key Architectural Decisions)](#3-關鍵架構決策-key-architectural-decisions)
  - [3.1 資料來源與儲存 (Source & Storage)](#31-資料來源與儲存-source--storage)
  - [3.2 應用的埋點要求 (Instrumentation Requirements)](#32-應用的埋點要求-instrumentation-requirements)
  - [3.3 歸屬與權限 (Ownership & Access)](#33-歸屬與權限-ownership--access)
  - [3.4 其他已定案決策](#34-其他已定案決策)
  - [3.5 人在迴圈中：系統只建議、人決定 (Human-in-the-Loop)](#35-人在迴圈中系統只建議人決定-human-in-the-loop)
- [4. 核心處理模組職責 (Core Components ①～⑨)](#4-核心處理模組職責-core-components-①⑨)
  - [4.1 平台層與 AIOps 層的分界](#41-平台層與-aiops-層的分界)
  - [4.2 主程式與 plugin](#42-主程式與-plugin)
  - [4.3 部署角色：api／beat／worker（Superset 式）](#43-部署角色apibeatworkersuperset-式)
- [5. 資料關聯與根因定位機制 (Correlation & RCA)](#5-資料關聯與根因定位機制-correlation--rca)
- [6. 各 phase 一句話](#6-各-phase-一句話)
- [7. 各模組：程式、驗證、狀態](#7-各模組程式驗證狀態)
- [8. 收斂：很多筆怎麼變成一件](#8-收斂很多筆怎麼變成一件)
- [9. 產品主線 8 步](#9-產品主線-8-步)
- [10. 還沒做、還不足的](#10-還沒做還不足的)
- [11. 哪裡過度設計](#11-哪裡過度設計)
- [12. 想確認什麼、跑哪個](#12-想確認什麼跑哪個)
- [13. 下一步](#13-下一步)
- [14. 各 Phase 的 PDCA 紀錄](#14-各-phase-的-pdca-紀錄)
  - [14.1 Phase 1 收值（歷史）](#141-phase-1-收值歷史)
  - [14.2 Phase 2 異常分析](#142-phase-2-異常分析)
  - [14.3 Phase 3 定位問題](#143-phase-3-定位問題)
  - [14.4 Phase 4 RCA＋LLM](#144-phase-4-rcallm)
  - [14.5 Phase 5 learning loop](#145-phase-5-learning-loop)
  - [14.6 跨階段](#146-跨階段)
- [附錄 (Appendix)](#附錄-appendix)
  - [術語對照表 (Glossary)](#術語對照表-glossary)
<!-- /toc -->

## 1. 系統目標與邊界 (System Overview & Scope)

**核心目標**

1. **縮短從「發現異常」到「確認根因」的時間**：數百個服務的正常值各不相同且隨時段變化，固定門檻不可行。以動態基準線學習 (Dynamic Baseline Learning) 判斷異常，再沿 Metrics → Trace → Log 定位根因，交給值班可直接行動的事故報告。
2. **沿用公司現有的 Elastic APM 資料、不另建收值**：應用團隊不改埋點方式；engine 只讀 APM 寫進公司 ES 的 log、metrics、trace，結果寫進 `aiops-*`，三種訊號靠 `trace.id`、`service.name` 對得上。
3. **以專家回饋持續校正**：值班與資深維運專家的判斷回流為事故知識庫與門檻調整（建議 → 回測 → 核准）；以統計＋LLM 為主，不自行訓練模型。

**系統邊界**

| | 範疇 |
|---|---|
| 目前支援 | 一般 IT 服務：Web API、worker、Kafka、DB；已掛 Elastic APM agent 的應用 |
| 未來規劃 | 工廠（EAP、機台、MES／MCS／AMHS、批號載具）：不在目前範圍 |

對外介面：輸入為 Elastic APM 寫進公司 ES 的 `traces-apm*`、`logs-apm*`、`metrics-apm*`；輸出為依部門送出的 Grafana 通知、事故報告與回饋入口。

## 2. 全域架構圖 (End-to-End Architecture)

圖中括號為第 4 節的模組編號。

```mermaid
flowchart TB
  subgraph S1["收值、清資料、存（①②③）：公司現有，不動"]
    direction LR
    APP["應用 Pod／VM<br/>Elastic APM agent"]
    APM["Elastic APM<br/>apm-server／Fleet"]
    ES[("公司 Elasticsearch 8.19<br/>traces-apm*／logs-apm*／metrics-apm*")]
  end
  subgraph S4["異常偵測（④⑤）"]
    direction LR
    DET["Detector<br/>靜態臨界值規則／動態基準線<br/>Pod 同儕比對／Trace 分段"]
    AN[("異常總表")]
  end
  subgraph S5["關聯收斂（⑥）"]
    CORR["Investigator<br/>同服務、時間相連 → 一件事故"]
  end
  subgraph S6["根因分析（⑦）"]
    direction LR
    RCA["Investigator<br/>Metrics → Trace → Log"]
    SIM["相似事故檢索"]
    LLM["LLM 摘要<br/>每句附依據"]
  end
  subgraph S7["告警與反饋閉環（⑧⑨）"]
    direction LR
    GF["Grafana<br/>依部門通知"]
    FB["回饋入口"]
    KB[("事故知識庫")]
    TUNE["調整建議<br/>回測 → 核准"]
  end

  APP -- "log／metrics／trace" --> APM
  APM --> ES
  ES -- "唯讀查詢" --> DET
  DET --> AN --> CORR --> RCA
  ES -. "trace、log 證據" .-> RCA
  RCA --> SIM --> LLM --> GF --> FB
  FB --> KB
  FB --> TUNE
  KB -. "已確認事故" .-> SIM
  TUNE -. "門檻、排除時段、新規則" .-> DET
```

- **部署拓撲**：收值是公司現有的 Elastic APM 與 ES，不在這個 repo；ES、AIOps engine、Grafana 在同一個 cluster；engine 以專用 API key 讀 `traces-apm*`、`logs-apm*`、`metrics-apm*`，只寫 `aiops-*`、`traces-merged-*`。engine 一個 image，裝成一隻 pod 或 api／beat／worker 三種角色（4.3）；畫面全在 Grafana。
- **模組交接**：模組之間只透過 ES 索引交接（`aiops-anomalies`、`aiops-alerts`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning`），任一模組可獨立重跑或替換。

## 3. 關鍵架構決策 (Key Architectural Decisions)

### 3.1 資料來源與儲存 (Source & Storage)

- **決策**：收值由公司現有的 Elastic APM 負責，資料已在公司 Elasticsearch；這個 repo 不收值、不部署 ES、不改 APM 寫入的任何索引。engine 的 API key 對 APM 的三種資料只讀。
- **理由**：
  - 公司已有一套收集系統，少維運一套。
  - 三種訊號由同一個 APM agent 帶 `trace.id`、`service.name`，跨訊號關聯不需翻譯。
  - 讀寫分開：APM 的索引公司管，`aiops-*` 與 `traces-merged-*` 是 engine 的，權限與保留各管各的。
- **取捨**（APM 的資料形狀決定了哪些功能做得到，細節見 [aiops-engine/code/configs/apm/README.md](../aiops-engine/code/configs/apm/README.md)）：
  - metrics 是 apm-server 預先聚合的每分鐘直方圖（微秒），最細粒度 1 分鐘；沒有 Pod CPU／記憶體、K8s 物件狀態（要 `metrics-apm.app.*` 或另外收）。
  - log 只有開 log shipping 的服務才有；例外是 `logs-apm.error-*` 的獨立文件，還沒接回 span（例外吞噬檢測做不到）。
  - 往外呼叫（DB、下游）的 metrics 只有次數與耗時總和，沒有直方圖，只能算平均。
  - 保留天數由公司 ES 的 ILM 決定；`traces-merged-*` 7 天（[kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 第 1 段）。

### 3.2 應用的埋點要求 (Instrumentation Requirements)

- **決策**：應用掛 Elastic APM agent（由應用團隊與公司 APM 團隊管，不在這個 repo）。engine 對 agent 的要求只有三件：
  - **trace 100% 收**：transaction sample rate 設 1.0。取樣會讓 trace-merge 合出不完整的請求。
  - **帶部門標籤**：`labels.dept`、`labels.group`（agent 的全域標籤）。沒設的告警全進 `unassigned`。
  - **log 帶 `trace.id`**：根因分析靠它撈同一請求在各服務的 log；沒開 log shipping 的服務只有 `logs-apm.error-*` 的例外。
- **邊界**：
  - 業務 ID（交易 ID、批號）要在應用層補成 `labels.*`，只放 trace、log，不放 metrics（避免序列數爆炸）。
  - Kafka 消費端處理訊息時的呼叫若自成獨立 trace，對不回原訊息，只能以服務＋時間關聯。

### 3.3 歸屬與權限 (Ownership & Access)

- **決策**：
  - **歸屬讀 agent 的標籤**：engine 寫 `aiops-anomalies` 時，依異常的服務（或 namespace）去查 `metrics-apm.transaction.1m-*` 最近 2 天的 `labels.dept`、`labels.group` 補上；對不到歸 `unassigned`。
  - **權限**：engine 與 Grafana 各一把 API key（[kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt) 第 3、4 段）。engine 讀 APM 的三種資料、讀寫 `aiops-*`、`traces-merged-*`；Grafana 只讀 `aiops-alerts` 等四個索引。
- **理由**：部門歸屬用現有的標籤機制，不另建對照表；讀寫分開，engine 出錯最多弄壞自己的索引，不會動到 APM 的資料。
- **取捨**：標籤由 agent 自報，沒有邊緣驗證，填錯會送錯部門；沒設標籤的服務全進平台。要更準確時改成 engine 維護一張「服務 → 部門」的對照表（`owner.py` 已是單一入口）。

### 3.4 其他已定案決策

| 決策 | 理由 | 取捨 |
|---|---|---|
| trace 100% 收、一個請求合成一筆 | 製造業的事故檢討不能少資料：APM 全送（sample rate 1.0），engine 每分鐘把 `traces-apm-*` 的 transaction 與 span 合成一筆（`traces-merged-*`，7 天；根因分析讀這份） | 量比取樣時大；合成的 `nested` 不省空間；根因分析看到的是 1～2 分鐘前合好的（[trace-merge](pdca/phase1-collect/2026-10-trace-merge.md)） |
| 季節性時間序列基準線為主力 | 星期幾 × 小時的週期（上下班、夜間批次）只有分時段的歷史看得出來 | 需累積 2～4 週；超過一週的週期（月底批次）要另設週期放寬 |
| 統計＋LLM，不自訓模型 | 可解釋、可重現，不需大量標註與 MLOps（[feedback/README.md](feedback/README.md)） | 多指標複雜關聯的能力有限；準確度靠回饋與評測集校正 |
| metrics → trace 用「服務＋路由＋時間窗」，不用 exemplar | ES 不存 exemplar；exemplar 只是單一樣本、不保證是造成異常的那個請求 | 是同時間、同路由的統計關聯，不是同一個請求 |

### 3.5 人在迴圈中：系統只建議、人決定 (Human-in-the-Loop)

- **決策**：AIOps 只提供線索與建議，不做處置，也不自己改變偵測的行為。處置（重啟、擴縮、改設定）由值班決定；會改變偵測行為的調整，系統只提案，要人核准才生效。
- **理由**：製造業 IT 的處置錯了影響產線；統計與 LLM 的判斷會錯，要有人把關、可追責（誰、何時、為什麼）。單一回饋不改門檻，避免一個人按錯就改壞。
- **取捨**：從異常到處置一定經過人，處置時間取決於值班；建議累積後要有人審，沒人審就不會變好。

系統自動做的，都只是產生資訊給人看：

| 自動的部分 | 做什麼 | 界線 |
|---|---|---|
| 偵測、事件整合（⑤⑥） | 異常合成一件、同服務合併、沿呼叫關係併一層（上游併進下游那件） | 只決定怎麼呈現；併進來的上游記在 `upstream_services`，看得到 |
| 根因分析報告（⑦） | 慢在哪一段、最慢請求的 trace、相關 log、相似的過去事故 | 給值班的線索，不下結論 |
| LLM 摘要（⑦） | 報告開頭三到五句 | 只加註；每句要附依據，驗證不過就不顯示、退回固定句型 |
| 通知（⑧） | Grafana 依部門送出 | 通知人，不替人決定 |
| 自動開的事故（⑨） | `aiops-incidents` 的 `status: auto` | 老師傅確認後才是 `confirmed`；相似事故檢索只用確認過的 |

會改變偵測行為的，一律要人核准：

| 調整 | 誰提 | 怎麼生效 |
|---|---|---|
| 放寬門檻、週期放寬、調 `min_abs` | 同一個對象誤報累積 3 次（`feedback suggest`） | `status: proposed` → `backtest` → 人 `adjust approve --by <誰>` |
| 漏報後降門檻 | 漏報回饋 | 同上 |
| 升級成規則（`rule_suggestion`） | 同一個根因被確認 3 次 | 同上（核准前也要回測） |
| 一句話回饋抽出的調整 | LLM 抽取 | 只寫成 proposed，同上 |
| 事故時段不學（`exclude_window`） | 事故紀錄 | proposed，人核准 |

- 程式裡把建議變成生效的只有 `adjust approve`，會記 `approved_by`、`approved_at`；回測時把建議當成生效來試算，但不寫回。
- 人自己用 `tune`、`adjust add` 寫的調整預設直接生效、不用回測：是人做的決定，但沒有第二個人把關。
- 沿呼叫關係的合併是自動判斷的：合錯時上游那件會被標成已併入（`merged_into`）。合成資料的誤併率 0，還沒有真實資料驗證。
- 細節：[feedback/README.md](feedback/README.md)（回饋與調整怎麼生效）、[aiops-engine/code/configs/baseline/reference.md](../aiops-engine/code/configs/baseline/reference.md#使用者feedback-調整)（指令）。

## 4. 核心處理模組職責 (Core Components ①～⑨)

編號 ①～⑨ 是全專案唯一的模組編號，所有文件、PDCA 紀錄、待辦都沿用。

| 編號 | 層 | 模組 | 職責定義 | 核心技術或演算法 |
|---|---|---|---|---|
| ① | 公司現有 | 收值 | 應用的 log、metrics、trace | Elastic APM agent；apm-server；trace 100% 收 |
| ② | 公司現有 | 清資料 | apm-server 寫入時的欄位統一；engine 讀進來時轉成同一種格式 | `core/traces.py` 的 APM 轉換、序列的 `scale`（微秒→秒）、`configs/apm/` |
| ③ | 公司現有 | 存 | 公司 ES 8.19 的 data stream 與 ILM；engine 另在 ES 補 `traces-merged-*` 的 template 與 key | `elasticsearch/kibana-dev-tools.txt` |
| ④ | AIOps | 判斷 | 判斷每條序列是否異常，分 `warning`、`anomaly` 兩級 | 靜態臨界值規則 (Static Threshold Rules)；動態基準線學習（168 時段中位數＋MAD、卜瓦松、借同組）；Pod 同儕橫向比對 (Peer Comparison)；Trace 分段基準、N+1、例外吞噬檢測 (Exception Swallowing Detection)；學習前排除事故時段、行事曆、慢性問題 |
| ⑤ | AIOps | 異常總表 | 所有判斷結果的單一交接點 | `aiops-anomalies`，同序列同時間重跑覆蓋同一筆；寫入時補 `dept`、`group` |
| ⑥ | AIOps | 收斂 | 把大量異常合成值班可處理的「一件」 | 同一服務、30 分鐘內接續的異常合為一件；30 分鐘無新異常即結束；全程程式計算 |
| ⑦ | AIOps | 根因分析 | 對每件事故定位問題段落與證據，產出報告 | Metrics → Trace → Log 關聯（第 5 節）；相似事故檢索（指紋＋欄位比對，只檢索已確認事故）；LLM 摘要 |
| ⑧ | AIOps | 出口 | 把事故送到對的人，並提供回饋入口 | `aiops-alerts` 一件一筆；Grafana 依 dept 每件一則、結束發恢復，無 dept 送平台；告警心跳 |
| ⑨ | AIOps | feedback | 把專家判斷轉成知識與調整 | 確認的事故進知識庫；誤報提放寬、漏報提調降、同根因提規則草稿；回測通過才可核准；量化（件數、回饋率、誤報率） |

**④ 偵測方法的分工**：靜態臨界值規則管「狀態本身就是壞事」（重啟、副本不足、離極限太近；critical 立即觸發，其餘需持續一段時間）；動態基準線學習管有週期的服務整體指標（請求數、錯誤率、p95、log 筆數、CPU、記憶體）；Pod 同儕橫向比對管被服務平均稀釋的單一 Pod 異常（同服務至少 3 個 Pod）；Trace 分段基準管請求內部的變化（每段自身耗時、呼叫次數、錯誤、例外吞噬、Kafka 排隊）。單點偏離記為 `warning`（不觸發根因分析、不通知），同方向連續兩個時間窗或持續 30 分鐘才升為 `anomaly`；指定序列或偏離極大時採單點即判定 (Instant Anomaly)。

### 4.1 平台層與 AIOps 層的分界

| 層 | 模組 | 職責 |
|---|---|---|
| 公司現有 | ①②③ | Elastic APM 與公司 ES：把資料收齊、存好；這個 repo 不收值、不改它 |
| AIOps | ④～⑨ | 只讀 APM 的資料做判斷、收斂、根因、通知與回饋；只寫 `aiops-*`、`traces-merged-*` |

**灰色地帶**

| 項目 | 為什麼算灰色 |
|---|---|
| 讀進來時的格式轉換 | APM 與 OTel 的欄位名不同，轉換在 engine（`core/traces.py`、`configs/apm/`）；屬 ④ 的前處理，不是改資料 |
| 靜態臨界值規則 | 固定門檻屬傳統監控；放 engine 是為了跟其他異常走同一套收斂與通知 |
| 餵給基準線之前的清理（排除時段、行事曆、慢性病清單） | 決定基準線學哪些資料，屬 AIOps |

### 4.2 主程式與 plugin

engine 一份程式，情境各是一個 plugin（[core/plugin.py](../aiops-engine/code/core/plugin.py)、[plugins/](../aiops-engine/code/plugins/README.md)），環境變數 `AIOPS_PLUGIN` 選：`observability`（IT）、`factory`（工廠：ML／FDC 的異常＋履歷）、`metrics_only`（只有指標）；也可以自己寫一個掛上去。

| | 主程式（哪個情境都一樣） | plugin（情境不同就不同） |
|---|---|---|
| ④ | 五個模型的數學、`ingest`（別人判好的異常 → ⑤） | — |
| ⑤ | `aiops-anomalies` 的格式：**這是主程式與 plugin 之間的契約**——異常只要進得了 ⑤（有 `series`、`keys`、`level`、時間），後面全部接得上 | 異常的 `keys` 裡哪個欄位是「對象」（服務／機台）、哪個是「API」（method＋route／recipe） |
| ⑥ | 同對象時間相連合一件、開件關件、嚴重度分級（廣度＋持續＋換版上限） | 跨對象併件的依據（呼叫關係／履歷前後站／不併）、「換版」是什麼（版號／recipe 變更）、訊號怎麼分類（黃金訊號／參數家族） |
| ⑦ | 報告的每一段（[investigator/report.py](../aiops-engine/code/investigator/report.py)）、M4 分段、共同點、log 樣式、相似事故 | 報告有哪幾段、順序；索引與欄位的對照；用語（請求／lot、Trace／履歷） |
| ⑧⑨ | Grafana 通知、回饋、匯出 | 回饋表單的分類表、誤報原因 |

程式的資料夾照模組切（[docs/pdca/meta/2026-11-clean-code.md](pdca/meta/2026-11-clean-code.md)）：`detector/` ②③④⑤、`investigator/` ⑥⑦⑧、`feedback/` ⑨；兩個階段以上都碰的東西在 `core/`（連線、設定、plugin 擴充點、命令列共用的轉接、`aiops-incidents`／`aiops-tuning` 的契約）；`sources/` 是資料來源與結果存放的介面；`api/`、`scheduler/`、`tasks/`、`plugins/`、`models/`（門面）在最上層。命令列名稱沒變：⑨ 的子命令還是掛在 `python3 -m detector` 底下（`feedback/cli.py`），`detector/__main__.py` 只是入口。

擴充點只開「現在真的有兩種以上做法」的地方，沒有的不預先抽；第四種情境（例如網路設備靠拓樸併件）出現時，繼承一個內建 plugin 覆寫那幾個就好。`configs/` 不隨 plugin 搬：plugin 只決定預設排程表，設定檔仍在 `configs/<情境>/`。

### 4.3 部署角色：api／beat／worker（Superset 式）

engine 的每個工作都是「定期讀 ES、算、寫 ES」的子命令，沒有常駐狀態，所以**怎麼觸發**跟**算什麼**可以分開。一個 image、一份 values，`mode` 選（[aiops-engine/README.md](../aiops-engine/README.md)、[pdca/meta/2026-11-api-beat-worker.md](pdca/meta/2026-11-api-beat-worker.md)）：

| 角色 | 做什麼 | 份數 | 對應 Superset |
|---|---|---|---|
| Grafana（公司現有） | 所有畫面：儀表板、通知、回饋表單、「重跑根因分析」按鈕 | — | web 的畫面 |
| `api` | 沒有頁面的薄 API：表單送出、`POST /api/anomalies`（ML／FDC 推異常進 ⑤）、`POST /api/rca`；收到就寫 ES、丟一個 `alert` 工作 | 多份 | web 的後端 |
| `beat` | 每分鐘照 `schedule.yaml` 把該跑的工作丟進佇列，自己不跑 | 1 | celery beat |
| `worker` | 從一個佇列拿工作，開子行程跑 `python3 -m <args>`；`realtime`（每幾分鐘的）、`alert`（收斂，**單一消費者**）、`batch`（每天的，吃資源）各一個 Deployment | 各自設 | celery worker |
| Redis | 佇列（Celery broker） | 1 | Redis |

- **`mode: single`** 是同一份程式不經佇列：排程器自己開子行程，api 同一隻 pod。開發、離線廠區用；行為跟原本一樣。
- 為什麼 ④ 不改成事件驛動：ES 不會主動推資料，`detect`、`rules`、`train` 本來就要定時查；事件驛動只在「外部推異常進來」（工廠）和「人按重跑」兩處有意義，所以只有那兩處走 api → 佇列。
- 為什麼 `alert` 單一消費者：收斂是「一輪看所有新異常、同對象併一件」，兩個 worker 同時跑會各開一件。其他工作寫入都是固定 `_id`、重跑覆蓋，同時跑兩份不會錯只是浪費。
- 「上一次沒跑完就跳過」在佇列裡變成 `expires`：每個工作帶「下一次排定前 5 秒」的過期時間，worker 忙到過期的直接丟，不會堆一排舊的追著跑。

## 5. 資料關聯與根因定位機制 (Correlation & RCA)

**原則**：Metrics 回答「有沒有出事、何時開始」，Trace 回答「哪個服務的哪一段造成」，Log 回答「那一段裡發生了什麼」。三者靠共同鑰匙（服務、路由、Pod、namespace、時間窗、`trace_id`）串接；`dept`、`group` 是歸屬標籤，只決定資料屬於誰，不用於串接。

1. **Metrics 發現異常**：異常帶出服務、路由與時間窗，並經 ⑥ 收斂為一件事故。
2. **Trace 鎖定慢呼叫／錯誤段**：以「服務＋路由＋時間窗」撈出事時段最慢與出錯的請求，與平常（一週前同時段；沒有則用出事前同樣長的一段）比較。**只算每段自身耗時**（扣除子呼叫），增加最多的段排第一，否則鏈上每一層都顯得一樣慢；**錯誤源頭**是自己標記錯誤、子呼叫都沒錯的那一段。
3. **Log 擷取關鍵錯誤**：以樣本請求的 `trace_id` 撈同一請求在各服務的 log；可疑服務該時段的 warn／error 歸納成樣式與平常比較，找出**新出現的 log 樣式**。
4. **LLM 生成摘要**：程式把事故整理成結構化輸入（異常、主要 trace 段、log 樣式、相似的已確認事故），LLM 只寫報告開頭三到五句。**每句須附依據 id**，由程式檢查引用，引用不存在的 id 整段不顯示；模型不可用時退回固定句型。LLM 只加註，不決定是否告警。

**精度差異**：Metrics → Trace 是同服務、同路由、同時間窗的統計對應，不保證是造成指標變差的同一批請求；Trace → Log 以 `trace_id` 對應，是同一個請求。完整鑰匙清單、限制與實例見 [rca/README.md「metrics 異常怎麼串到 trace、log」](rca/README.md)。

## 6. 各 phase 一句話

| Phase | 層 | 現況 |
|---|---|---|
| 1 收值 | 公司現有 | 🟡 改用公司的 Elastic APM（自建 Collector 路徑 2026-11 已刪）；engine 讀 APM 格式，單元測試過，沒在真實 APM 資料上跑過 |
| 2 異常分析 | AIOps | ✅ 靜態臨界值規則、動態基準線、Pod 同儕橫向比對、trace 段都有；門檻是估的 |
| 3 定位問題 | AIOps | ✅ K8s 上整條線跑通；engine 的排程（一隻 pod）在 K8s 上跑過 |
| 4 RCA＋LLM | AIOps | ✅ 相似事故、LLM 摘要（真的模型跑過）；沒有評測集 |
| 5 learning loop | AIOps | ✅ 回饋 → 回測 → 核准用模擬資料跑通；真實數字等試點 |

## 7. 各模組：程式、驗證、狀態

K8s 排程頻率見 [aiops-engine/README.md](../aiops-engine/README.md)；Elastic APM 版的設定與排程在 `aiops-engine/code/configs/apm/`。

| # | 層 | Phase | 程式／設定 | 多久跑一次 | 怎麼驗過 | 狀態 |
|---|---|---|---|---|---|---|
| ① 收值 | 公司現有 | 1 | Elastic APM（公司管） | 常駐；metrics 每分鐘 | — | 公司現有 |
| ② 清資料 | 公司現有 | 1 | `aiops-engine/code/core/traces.py`（`_apm_span`）、`core/source.py`、`configs/apm/` | 讀進來時 | `tests/test_apm.py`（[apm-source](pdca/meta/2026-11-apm-source.md)） | 🟡 沒有真實 APM 資料 |
| ③ 存 | 公司現有 | 1 | `elasticsearch/kibana-dev-tools.txt`（只補 `traces-merged-*` 與 key） | — | — | 🟡 公司環境沒實跑 |
| ④ 判斷 | AIOps | 2 | `aiops-engine/code/detector/`、`aiops-engine/code/configs/baseline/*.yaml` | 每 1 分鐘（train 每天） | 單元測試；`synth_e2e`、`adaptive_e2e`、`fine_e2e`（一個 ES） | ✅；K8s CronJob 沒跑過 |
| ⑤ 異常總表 | AIOps | 2 | `aiops-anomalies` | — | — | ✅ |
| ⑥ 收斂 | AIOps | 3 | `aiops-engine/code/investigator/alerts.py`、`cmd_alert.py`；`rca.yaml` 的 `alerts:` | 每 1 分鐘 | `test_alerts.py`、`chain_e2e`；[alert-exit](pdca/phase3-locate/2026-10-alert-exit.md)、[cross-service-aggregation](pdca/phase3-locate/2026-10-cross-service-aggregation.md) | ✅ 同服務合成一件；沿呼叫關係一層 ✅、多層 📝 |
| ⑦ 根因分析 | AIOps | 3、4 | `aiops-engine/code/investigator/`（`__main__.py`、`similar.py`）、`aiops-engine/code/core/llm.py` | 跟 ⑥ 同一次 | `loop_e2e`；`test_similar.py`、`test_llm.py` | ✅；沒有評測集 |
| ⑧ 出口 | AIOps | 3 | `grafana/helmchart/files/*.yaml` | Grafana 每 1 分鐘查 | Grafana 12.2、13.0.7 實測；[rca-demo](pdca/phase3-locate/2026-10-rca-demo.md) | ✅；K8s CronJob 沒跑過 |
| ⑨ feedback | AIOps | 5 | `aiops-engine/code/api/`、`cmd_feedback.py`、`cmd_backtest.py`、`cmd_metrics.py` | api 常駐；suggest、backtest 人跑 | `loop_e2e`；`test_api.py`、`test_feedback.py`、`test_metrics.py` | ✅（模擬資料） |

- ⑤ 欄位見 [baseline README「異常帶的欄位」](../aiops-engine/code/configs/baseline/reference.md#異常帶的欄位)；⑥ 哪些異常會開一件、怎麼不重複開見 [rca README「事件整合」](../aiops-engine/code/configs/rca/README.md#事件整合alert)。
- ⑧ `aiops-alerts` 帶 `status`、`service`、`dept`、`group`、`severity`、`summary`、`report`、`feedback_url`；`summary` 是 Grafana 的 label，不放會變的字；心跳「AIOps 停了」docker 實測過。
- ⑨ 回饋表單定案用 Grafana Business Forms 外掛，回饋服務的小網頁當備案（[grafana-form](pdca/phase5-learning-loop/2026-10-grafana-form.md)）。
- 排程：`mode: single` 是一隻常駐 pod 裡的排程器（`aiops-engine/code/scheduler`，照 `configs/<plugin>/schedule.yaml`）自己跑；`mode: distributed` 是 beat 丟進佇列、worker 跑（4.3）。第一階段只開 rules、detect、alert、train 四個工作，其他在第二階段。

## 8. 收斂：很多筆怎麼變成一件

設計見 [rca/README.md「雜訊壓制」](rca/README.md)。

| 層 | 狀態 |
|---|---|
| ① 同一條序列連續 → 一段 | ✅ `status`、`streak`（升級規則見 [第 4 節 ④](#4-核心處理模組職責-core-components-①⑨)）；📝 一段記成一筆 |
| ② 同一個服務、時間重疊 → 一件 | ✅ `investigator alert`；同 dept 的兩個服務是兩件 |
| ③ 沿呼叫關係 → 找源頭 | ✅ 一層：下游有件開著、直接呼叫它的上游同時段異常 → 併進下游那件（`upstream_services`），根因分析以下游跑；📝 多層（A→B→C）、常駐拓撲 |
| ④ 排優先、靜音、限頻 | 📝 |

## 9. 產品主線 8 步

產品的說法是 8 步（賣點：learning loop、資深維運專家（老師傅）回饋、RCA 結合 RAG）；編號仍以 ①～⑨ 為準。**一列一個模組**，同一步跨模組的拆成多列；依 Phase、模組編號排序。

| Phase | 模組 | 步 | 內容 | 現況 |
|---|---|---|---|---|
| 1 收值 | ① 收值 | 1 | 收值（公司現有的 Elastic APM），三種資料靠 `trace.id`、`service.name` 對得上 | 🟡 |
| 1 收值 | ② 清資料 | 1 | engine 讀進來時把 APM 欄位轉成同一種格式 | 🟡 單元測試 |
| 2 異常分析 | ② 清資料 | 2 | 清資料（餵 baseline 前）：`adjust`、行事曆（見 4.1 灰色地帶）；`quality`、`chronic` 只有 OTel 版 | ✅ 程式；⚠️ 沒排程 |
| 2 異常分析 | ④ 判斷 | 3 | 異常分析：動態基準線、trace 每一段＋靜態臨界值規則、Pod 同儕橫向比對 | ✅ 程式；門檻是估的 |
| 2 異常分析 | ⑤ 異常總表 | 3 | 異常結果寫進 `aiops-anomalies` | ✅ |
| 3 定位問題 | ⑥ 收斂 | 6 | 整合事件：很多筆異常合成一件 | ⚠️ 同服務合成一件；沿呼叫關係只併一層 |
| 3 定位問題 | ⑦ 根因分析 | 4 | 串 metrics → trace → log，抓出問題在哪 | ✅ |
| 3 定位問題 | ⑦ 根因分析 | 5 | 輔助工具：呼叫關係、瀑布圖、log 樣式、業務 ID 查交易、LLM 查詢工具 | ⚠️ 前三樣有 |
| 3 定位問題 | ⑧ 出口 | 6、8 | 把一件事送出去（Grafana 通知）；回饋入口 | ✅ |
| 4 RCA＋LLM | ⑦ 根因分析 | 7 | 根因＋RAG：附相似事故；LLM 摘要 | ✅ 模擬資料 |
| 5 learning loop | ⑨ feedback | 7 | 相似事故裡老師傅當時怎麼判斷與處置（知識庫來自回饋） | ✅ 模擬資料 |
| 5 learning loop | ⑨ feedback | 8 | learning loop：知識庫更新、門檻調整（建議 → 回測 → 核准） | ✅ 模擬資料 |

- 8 步沒寫到、但一定要的是 ⑧ 出口：沒有它，第 6 步之後沒人看到，第 8 步也沒有回饋可學。
- 定案：不再加新的偵測機制，先把這一圈在真實環境跑起來。

整合驗證（`tests/loop_e2e.py`，docker 的 ES 9.5.5、模擬資料 `aiops-synth`：3 個服務 4 週歷史＋2 天，37 項全過）：

| 故事 | 結果 |
|---|---|
| 第 1 次 mes-api 變慢 → 一件＋報告 → 老師傅回饋 | `aiops-incidents` 先 `auto` 再 `confirmed`；每筆異常一筆回饋 |
| 第 2 次同樣症狀 | 報告最前面附第 1 件；LLM 摘要（template）每句附依據 |
| 第 3 次 → 同根因 3 次 | 附 2 件相似；`similar-eval` recall@3 = 3/3；提規則草稿 |
| 月底批次誤報 3 次 | 建議 z 5 → 7；沒回測不能核准；`backtest` 告警 3 → 1；核准後不再報 |
| 漏報 | 一筆降 z 的建議 |
| 量化（`metrics`） | 6 件、有回饋 100%、誤報率 50%、確認的事故 3 |

一件事從頭到尾（`loop_e2e` 跑出來的）：

```
10:00  mes-api 延遲 120ms → 480ms（4 個 5 分鐘）
10:01  detect：synth.latency z 45，第 1 點 warning、第 2 點 anomaly → aiops-anomalies
10:02  alert：開一件 a-20261005-0205-mes-api（D3120／it）
       → 根因分析（有 trace 時：慢在 wip-worker › Oracle SELECT lots）
       → 相似事故：知識庫空的，不附
       → aiops-alerts status open、summary「[warning] mes-api：synth.latency 526（平常 121.3）…」
       → aiops-incidents 一筆 status auto（帶指紋）
       → 心跳
10:02  Grafana：status:open 查到 → it-oncall 收到一則，附摘要、報告連結、回饋連結
10:10  老師傅點連結：真的、db、「Oracle 統計資訊過期」、處置「重跑 analyze」、
       怎麼看出來的「只有 lots 那張表的查詢慢，AWR 看到執行計畫變了」
       → aiops-feedback 3 筆、aiops-incidents 變 confirmed
10:35  30 分鐘沒新異常 → resolved → Grafana 發恢復
隔天   同樣症狀 → 新的一件，報告最前面：
       「跟 10-05 的事故相似（同服務；同樣的指標異常）；當時根因 [db] …；處置 …」
       「建議先照上次的方式確認：只有 lots 那張表的查詢慢… [I1]」
```

## 10. 還沒做、還不足的

會影響能不能用、能不能賣的：

1. **真實資料上沒跑過**：APM 的欄位照 Elastic 官方資料模型寫（[configs/apm/README.md](../aiops-engine/code/configs/apm/README.md) 列了要對樣本核對的欄位）；其餘是模擬資料：門檻（z 5、`instant_z` 15、相似分數 4、誤報 3 次、權重）全是估的，要 2～4 週真實資料。
2. **真實資料上還沒跑過**：排程在測試 cluster 連測試 ES 跑過（[single-pod-scheduler](pdca/phase3-locate/2026-10-single-pod-scheduler.md)），真實資料、真的 LLM 要等試點。
3. **沒有評測集**：真的 LLM 跑過（OpenAI 12 次、rca-demo 一次），相似事故在沒有 trace 時只靠「同服務＋同指標」剛好壓在門檻上。
4. **回饋沒有身分**：回饋表單、小網頁都沒登入；回饋 3 次可以是同一個人（沒有第二人確認）。
5. 漏報的回報：Grafana 回饋儀表板的第二個表單面板（`/api/missed`），或 `feedback` 指令（已合入 master，[PDCA](pdca/phase5-learning-loop/2026-11-open-issues-batch.md)）。
6. 月底批次只會提「放寬 z」，不會提排除時段。
7. 舊事故單匯入 `aiops-incidents` 的格式沒寫。
8. code-review 的 10 項都已處理（最後三項：metrics 回饋跨段、回測重複查 ES、相似事故候選 20 → 200，已合入 master，[PDCA](pdca/phase5-learning-loop/2026-11-open-issues-batch.md)）。

各模組還沒做的：

| 模組 | 還沒做 |
|---|---|
| ① ② 收值、清資料 | 公司現有，不在這個 repo；APM 沒有的（Pod 資源、K8s 狀態、例外接回 span、往外呼叫 p95）見 [configs/apm/README.md](../aiops-engine/code/configs/apm/README.md) |
| ③ 存 | `traces-merged-*` 的容量（APM 100% 收時的量） |
| ④ 判斷 | 記憶體以外的趨勢；`groups` 依公司命名；新服務前幾天的移動式基準線（p99、慢請求比例、往外呼叫耗時、一階跳變提 `reset_point` 已做） |
| ⑥ 收斂 | 沿呼叫關係合併、排優先；一段記成一筆；靜音、限頻 |
| ⑦ 串接 | 單一 Pod 異常 → 拿它的 trace 逐段比；撈 log 帶 `span.id` |
| ⑨ feedback | 版本、評測集、第二人確認、排除時段的建議 |

## 11. 哪裡過度設計

以「維運可靠度、AIOps 基本盤」來看：

| 東西 | 為什麼算過度 | 建議 |
|---|---|---|
| 動態基準線三個自動機制同時存在：自選粒度、卜瓦松、借同組 | 沒有真實資料就先做三種退路，設定、文件、測試都變厚 | 留著但不再擴；真實資料跑過再決定砍哪個 |
| trace 每一段的 baseline＋N+1、例外吞噬檢測規則 | raw 只留 2 天，學長期要靠 `aiops-trace-segments` 的統計 | 先只跑根因分析那條；trace baseline 等真資料 |
| 回饋表單 10 個「查了哪裡」、6 種誤報原因、9 類根因 | 選項是估的，沒人填過 | 第一週看真的填了什麼再縮 |
| `metrics` 的「最吵的服務」、依 dept | Grafana 讀 `aiops-alerts` 畫更合適 | 留表格，不再加欄 |

**不是過度、一定要的**：靜態臨界值規則的保底門檻、連續才算、心跳、「已確認不覆蓋」、每句附依據的檢查、沒回測不能核准。這些是可靠度，不是功能。

## 12. 想確認什麼、跑哪個

| 想確認什麼 | 跑哪個 | 需要 |
|---|---|---|
| 程式邏輯對（不連 ES） | `cd aiops-engine/code && python3 -m pytest -q` | 無 |
| engine 讀得到公司的 APM 資料 | `AIOPS_SOURCE=apm python3 -m detector detect --config configs/apm/series.yaml --window 10m`、`python3 -m detector trace-merge --config configs/apm/trace.yaml`（[configs/apm/README.md](../aiops-engine/code/configs/apm/README.md)） | 公司 ES 的 engine key |
| 動態基準線抓得到造的異常、誤報多少 | `cd aiops-engine/code && python3 -m tests.synth_e2e` | 一個 ES |
| 整個 learning loop 的故事 | `cd aiops-engine/code && python3 -m tests.loop_e2e` | 一個 ES，約 2 分鐘 |
| Grafana 一件一則、依 dept | 照 [configs/grafana/README.md](../grafana/README.md) | Grafana 12～13 |
| 上 K8s 排程 | `helm install aiops aiops-engine/helmchart`（[aiops-engine/README.md](../aiops-engine/README.md)） | 測試 cluster 跑過 30 分鐘（[single-pod-scheduler](pdca/phase3-locate/2026-10-single-pod-scheduler.md)） |

## 13. 下一步

1. ④⑥⑧ 上 K8s 排程，試點產線先只寫入、不通知 2～4 週。
2. ④ 用真實資料校正門檻。
3. ⑦⑨ 用有 trace 的事故驗相似事故與 LLM；建評測集（20～30 件過去事故）。
4. ⑥ 沿呼叫關係合併、排優先。

## 14. 各 Phase 的 PDCA 紀錄

每份紀錄一句結論；細節見各紀錄。

### 14.1 Phase 1 收值（歷史）

這些紀錄描述的是已刪除的自建 Collector 路徑（2026-11 改用公司現有的 Elastic APM，程式在 git 歷史）；當時的結論與數字不代表現在的架構。

| 紀錄 | 結論 |
|---|---|
| [es-startup-401](pdca/phase1-collect/2026-10-es-startup-401.md) | ES 剛起來的 401 窗口約 1 秒；健康檢查改用 API key，掉 0 |
| [field-unify](pdca/phase1-collect/2026-10-field-unify.md) | 在 ingestion 統一：log 加 alias、Java 指標在 Collector 改名 |
| [ingest-keys-check](pdca/phase1-collect/2026-10-ingest-keys-check.md) | 鑰匙圈交叉查得到；純文字 log 要開 OTLP 才帶 trace_id |
| [ingest-simplify](pdca/phase1-collect/2026-10-ingest-simplify.md) | 集中式資料寫入憑證 (Shared Ingest Token)；`k8s-*` 開 failure store；錯誤率、p95 靜態臨界值規則 |
| [k3d-e2e](pdca/phase1-collect/2026-10-k3d-e2e.md) | k3d 上 initContainer、邊緣身份鑑別與標籤注入 (Edge Identity Sanitization & Injection)、OTLP log 帶 trace_id 都跑過 |
| [retention](pdca/phase1-collect/2026-10-retention.md) | 保留天數已套用（trace 的保留在 2026-10-31 改成 raw 2 天、merged 7 天） |
| [time-alignment](pdca/phase1-collect/2026-10-time-alignment.md) | 沒帶時區差 8 小時已修；時鐘偏差抓得到；`NTP_SERVER` 要填 |
| [trace-durability](pdca/phase1-collect/2026-10-trace-durability.md) | 不用 Kafka；兩段佇列改寫磁碟；當掉仍掉約 20% |

### 14.2 Phase 2 異常分析

| 紀錄 | 結論 |
|---|---|
| [anomaly-owner](pdca/phase2-anomaly/2026-10-anomaly-owner.md) | 異常寫回時補 `dept`、`group`；對不到歸 unassigned |
| [chronic-calendar](pdca/phase2-anomaly/2026-10-chronic-calendar.md) | 慢性病清單、行事曆已合；清單沒送到負責人；沒排程 |
| [data-quality-check](pdca/phase2-anomaly/2026-10-data-quality-check.md) | 資料品質檢查已合；門檻是估的；沒排程 |
| [data-sources](pdca/phase2-anomaly/2026-10-data-sources.md) | 資料來源改接口＋假資料分開；負責人的真來源還沒有 |
| [exclude-adjust](pdca/phase2-anomaly/2026-10-exclude-adjust.md) | 排除時段、重置點、週期放寬已合；只放寬不變嚴；沒排程 |
| [instant-anomaly](pdca/phase2-anomaly/2026-10-instant-anomaly.md) | 偏太多（`instant_z` 15）單點即判定 (Instant Anomaly)；z 5.2 的誤報仍是 warning |
| [split-detector-cli](pdca/phase2-anomaly/2026-10-split-detector-cli.md) | `__main__.py` 1,100 行 → 160 行＋10 個檔，只搬不改 |

### 14.3 Phase 3 定位問題

| 紀錄 | 結論 |
|---|---|
| [alert-exit](pdca/phase3-locate/2026-10-alert-exit.md) | 同服務合成一件寫 `aiops-alerts`；Grafana 12.2 依 dept 一件一則 |
| [alert-flap](pdca/phase3-locate/2026-10-alert-flap.md) | 假恢復來自每輪覆蓋；engine 寫 `@timestamp` 往前 1 分鐘 |
| [rca-demo](pdca/phase3-locate/2026-10-rca-demo.md) | K8s 上整條線跑通：3 分半開件、根因指到 Oracle、通知送到 |
| [schedule](pdca/phase3-locate/2026-10-schedule.md) | Dockerfile、CronJob、心跳規則完成（CronJob 之後改成一隻 pod：[single-pod-scheduler](pdca/phase3-locate/2026-10-single-pod-scheduler.md)） |

### 14.4 Phase 4 RCA＋LLM

| 紀錄 | 結論 |
|---|---|
| [llm-summary](pdca/phase4-rca-llm/2026-10-llm-summary.md) | 每句附依據＋template 退路；捏造 id 整段不顯示；沒評測集 |
| [similar-incidents](pdca/phase4-rca-llm/2026-10-similar-incidents.md) | 指紋＋欄位比對、只檢索已確認；門檻權重是估的 |

### 14.5 Phase 5 learning loop

| 紀錄 | 結論 |
|---|---|
| [check-feedback](pdca/phase5-learning-loop/2026-10-check-feedback.md) | 事故紀錄、值班回饋的指令與 index 已合 |
| [expert-feedback](pdca/phase5-learning-loop/2026-10-expert-feedback.md) | 回饋小網頁：預填、一鍵判斷、事故變 confirmed；沒登入 |
| [fix-critical](pdca/phase5-learning-loop/2026-10-fix-critical.md) | 四個會讓整輪失敗或蓋掉資料的問題已修，各有測試 |
| [fix-feedback-ux](pdca/phase5-learning-loop/2026-10-fix-feedback-ux.md) | 三個操作問題已修；真的 LLM 實跑，修後 6/6 每句附依據 |
| [grafana-form](pdca/phase5-learning-loop/2026-10-grafana-form.md) | 定案用 Grafana Business Forms 外掛；身分還是自己填 |
| [grafana-plugin-init](pdca/phase5-learning-loop/2026-10-grafana-plugin-init.md) | initContainer 從 Nexus 裝外掛、核對 SHA-256；k3d 跑過 |
| [loop-update](pdca/phase5-learning-loop/2026-10-loop-update.md) | 誤報 3 次 → 回測 → 核准 → 不再報實測過；放寬 z 是治標 |
| [metrics-kpi](pdca/phase5-learning-loop/2026-10-metrics-kpi.md) | `metrics` 指令學之前、學之後並排；真實數字等試點 |
| [rule-threshold](pdca/phase5-learning-loop/2026-10-rule-threshold.md) | 規則門檻自動算、核准即載入；4 個定期工作見第 7 節 |

### 14.6 跨階段

| 紀錄 | 結論 |
|---|---|
| [docs-vs-code](pdca/meta/2026-10-docs-vs-code.md) | 只改文件；6 處對不上改 5 處；code-review 4 項要修 |
| [restructure](pdca/meta/2026-10-restructure.md) | 目錄改名（telemetry、engine、sdk、e2e），行為不變 |

## 附錄 (Appendix)

<details>
<summary>A.1 告警心跳與 ES 斷線時的行為</summary>

- 每輪收斂寫一筆心跳；Grafana 10 分鐘收不到即通知平台「AIOps 停了」，避免排程停擺被誤判為事故全部恢復。
- 開著的事故與心跳的時間戳往前 1 分鐘，避免 Grafana 查詢窗與覆寫錯開造成假恢復。
- ES 連不上時，Grafana 發的是自己的資料來源錯誤，不是「AIOps 停了」。

詳見 [troubleshooting.md](troubleshooting.md#grafana-告警)。
</details>

### 術語對照表 (Glossary)

正式術語與舊說法的對照表只留一份，放在 [CLAUDE.md](../CLAUDE.md#術語對照表)（每次開工都會被讀到）；`python3 tools/check-docs.py` 會檢查活的文件沒有再用舊說法。
