# 文件地圖

全 repo 的 md 都從這裡找得到。**文件留在原位**（跟程式強相關的放在各元件資料夾（`aiops-engine/`、`elasticsearch/`、`grafana/`）旁，改程式時才會順手改），這份只做索引和標類型、狀態。每次新增、刪除 md 都要更新這裡，`python3 tools/check-docs.py` 會檢查（沒列進來、連結斷了、用了舊說法、共用文件沒標類型都會失敗）。規則見 [CLAUDE.md](../CLAUDE.md)。

**類型**（Diátaxis：每份文件只做一種事；rca、feedback 各合成一份 README，原理在前，規格、操作、案例是章節）：
**原理**＝為什麼這樣設計　**操作**＝我要做 X 怎麼做　**規格**＝查欄位、參數、現況　**案例**＝實際跑一遍的實例

**狀態**：🟢 已實作（對得到程式與驗證）　🟡 定案方向、部分實作　⚪ 構想，尚未實作

**第一次來，這樣讀**：[專案 README](../README.md) → [architecture.md](architecture.md)（架構與設計決策）→ 要看哪個環節就進下面對應的主題。要「做事」直接找操作；要「查」找規格。

## 依主題

### 總覽

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [../README.md](../README.md) | 原理 | 專案是什麼、怎麼跑起來 | 🟢 |
| [architecture.md](architecture.md) | 原理 | 架構、設計決策與現況：收值用公司現有的 Elastic APM、目標、全域架構圖、關鍵決策、①～⑨ 模組職責、根因定位機制（第 1～5 節）；各 phase、各模組的程式、驗證、還沒做的、PDCA 索引（第 6～14 節） | 🟡（Elastic APM 的資料沒實跑過） |
| [semiconductor-design.md](semiconductor-design.md) | 原理 | 半導體（FDC）沒有 trace id 時：lot 加工路徑取代 trace、五個模型與事件整合各要改什麼、跟目前設計的對照、做的順序 | ⚪ 構想（沒有 MES、FDC 欄位樣本） |
| [aiops-data-reference.md](aiops-data-reference.md) | 規格 | AIOps 資料格式：`aiops-anomalies`、`aiops-alerts`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning` 的欄位、誰在什麼時候寫、真實範例、Grafana 面板讀哪些欄位 | 🟢 |
| [deploy-how-to.md](deploy-how-to.md) | 操作 | 部署：公司已有 Elastic APM 與 ES，不跑 es-setup，在 Kibana 手動補設定、部署 aiops-engine、接 Grafana | 🟡（沒在真實環境照做過） |
| [troubleshooting.md](troubleshooting.md) | 操作 | 症狀 → 原因 → 做法 | 🟢 |
| [img/README.md](img/README.md) | 案例 | POC 五個 KPI（聚合警報、LLM 解析、端到端串查、相似事故 Top-3、RCA 全文）在 Grafana 上的截圖 | 🟢（合成資料） |

### 資料存哪裡

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [data-storage-reference.md](data-storage-reference.md) | 規格 | 資料存哪裡：Elastic APM 寫的原始資料（只讀）、ES 的 `aiops-*`（訓練、RCA 報告、回饋）誰寫、多久一次、會不會自己消失；Grafana 自己存什麼 | 🟢 |

### ④～⑦ 判斷、收斂、根因分析

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [rca/README.md](rca/README.md) | 原理 | 根因分析六步與 AI 的位置、三種資料怎麼串、動態基準線學習怎麼判斷、15 個故障案例的歸納、現況；graph RAG 構想（⚪ 沒有程式、沒有驗證） | 🟢 |
| [../aiops-engine/code/configs/baseline/reference.md](../aiops-engine/code/configs/baseline/reference.md) | 規格 | baseline 規格：序列、狀態、參數、規則、檢查、欄位、結果在哪 | 🟢 |
| [../aiops-engine/code/configs/baseline/README.md](../aiops-engine/code/configs/baseline/README.md) | 操作 | baseline：怎麼跑 train／detect／rules／peer／quality／chronic、怎麼調、怎麼驗 | 🟢 |
| [../aiops-engine/code/configs/baseline/verification.md](../aiops-engine/code/configs/baseline/verification.md) | 案例 | baseline 各功能的實測與驗證結果 | 🟢 |
| [../aiops-engine/code/configs/rca/README.md](../aiops-engine/code/configs/rca/README.md) | 操作 | 根因分析：預設值、怎麼跑、匯入歷史事故單、報告長什麼樣子、限制、怎麼驗 | 🟢 |
| [../aiops-engine/code/configs/rca/example-report.md](../aiops-engine/code/configs/rca/example-report.md) | 案例 | 一份範例報告 | 🟢 |
| [../aiops-engine/code/configs/apm/README.md](../aiops-engine/code/configs/apm/README.md) | 操作 | 用 Elastic APM 的資料跑 engine（收值就是這個）：哪些功能能用、ES role、template、`source: apm`、欄位對照 | 🟡（未在真實 APM 資料上跑過） |
| [../aiops-engine/code/sources/README.md](../aiops-engine/code/sources/README.md) | 操作 | 資料來源：ES 以外（檔案、HTTP、程式給 DataFrame）也能跑 train／detect／peer／rules；`source:`、`store:` 怎麼寫、跟 ES 算得一不一樣 | 🟢 |
| [../aiops-engine/code/models/README.md](../aiops-engine/code/models/README.md) | 規格 | 五個模型的標準輸入（欄位、一列是什麼）與主要函式 | 🟢 |
| [../aiops-engine/code/interfaces/README.md](../aiops-engine/code/interfaces/README.md) | 規格 | 要別人提供的資料的格式 | 🟢 |
| [../aiops-engine/code/fake-data/README.md](../aiops-engine/code/fake-data/README.md) | 規格 | 假資料（只為了讓程式現在能跑、能測） | 🟢 |

### ⑧⑨ 出口與 feedback（learning loop）

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [feedback/README.md](feedback/README.md) | 原理 | 閉環怎麼走、為什麼用 LLMOps 不自己訓練模型、紀律、LLM 何時叫與 token、現況 | 🟢 |
| [../grafana/README.md](../grafana/README.md) | 操作 | Grafana：讀 aiops-alerts 發通知、儀表板、回饋表單、用 Helm 裝或 initContainer 裝外掛 | 🟢 |
| [../grafana/code/INSTALL-form-plugin.md](../grafana/code/INSTALL-form-plugin.md) | 操作 | 給管 Grafana 的人：裝回饋表單外掛 | 🟢 |

### 部署

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [../elasticsearch/README.md](../elasticsearch/README.md) | 操作 | 在公司 ES 上補 `traces-merged-*` 的 template 與 ILM、發 engine 與 Grafana 的 key、snapshot | 🟢 |
| [../aiops-engine/README.md](../aiops-engine/README.md) | 操作 | engine：build image、Helm 裝一隻 pod（排程器＋回饋網頁）、排程表、「AIOps 停了」 | 🟢 |

## 依類型找

| 我要… | 看這些 |
|---|---|
| **懂為什麼這樣設計**（原理） | [architecture.md](architecture.md)、[rca](rca/README.md)・[feedback](feedback/README.md) 的 README（各自也含規格、操作、案例章節） |
| **做一件事**（操作） | [部署](deploy-how-to.md)、[troubleshooting.md](troubleshooting.md)、[接 Elastic APM 的資料](../aiops-engine/code/configs/apm/README.md)、[baseline 怎麼跑](../aiops-engine/code/configs/baseline/README.md)、[在公司 ES 上補設定](../elasticsearch/README.md) |
| **查欄位、參數、現況**（規格） | [aiops-data-reference.md](aiops-data-reference.md)、[data-storage-reference.md](data-storage-reference.md)、[baseline 規格](../aiops-engine/code/configs/baseline/reference.md) |
| **看實例**（案例） | [baseline 實測](../aiops-engine/code/configs/baseline/verification.md)、[範例報告](../aiops-engine/code/configs/rca/example-report.md)；故障案例的歸納在 [rca/README.md](rca/README.md) |

## PDCA 紀錄

每次工作一份，依 phase 分資料夾；索引、各 phase 的結論在 [pdca/README.md](pdca/README.md)，寫法見 [pdca/TEMPLATE.md](pdca/TEMPLATE.md)。PDCA 是紀錄，不是文件，不分類型。
