# 文件地圖

全 repo 的 md 都從這裡找得到。**文件留在原位**（跟程式強相關的放在各元件資料夾（`aiops-engine/`、`grafana/`）旁，改程式時才會順手改），這份只做索引和標類型、狀態。每次新增、刪除 md 都要更新這裡，`python3 tools/check-docs.py` 會檢查（沒列進來、連結斷了、用了舊說法、共用文件沒標類型都會失敗）。規則見 [CLAUDE.md](../CLAUDE.md)。

**類型**（Diátaxis：每份文件只做一種事；rca、feedback 各合成一份 README，原理在前，規格、操作、案例是章節）：
**原理**＝為什麼這樣設計　**操作**＝我要做 X 怎麼做　**規格**＝查欄位、參數、現況　**案例**＝實際跑一遍的實例

**狀態**：🟢 已實作（對得到程式與驗證）　🟡 定案方向、部分實作　⚪ 構想，尚未實作

**第一次來，這樣讀**：[專案 README](../README.md) → [architecture.md](architecture.md)（架構與設計決策）→ 要看哪個環節就進下面對應的主題。要「做事」直接找操作；要「查」找規格。

## 依主題

### 總覽

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [../README.md](../README.md) | 原理 | 專案是什麼、怎麼跑起來 | 🟢 |
| [architecture.md](architecture.md) | 原理 | 架構、設計決策與現況：收值用公司現有的 Elastic APM、目標、全域架構圖、關鍵決策、①～⑨ 模組職責、主程式與 plugin 的分界（§4.2）、根因定位機制（第 1～5 節）；各 phase、各模組的程式、驗證、還沒做的、PDCA 索引（第 6～14 節） | 🟡（Elastic APM 的資料沒實跑過） |
| [module-map.md](module-map.md) | 原理 | ①～⑨ 每一階段拆成哪些功能、程式在哪（整條線與資料存放的流程圖）、橫向支撐、現在最弱的地方、一次事故的走法 | 🟢 |
| [weakness-inventory.md](weakness-inventory.md) | 原理 | 設計弱點盤點：會真的出事的、做了但沒驗證過的、還沒做的、最緊資源測試測出的，最先處理的三個 | 🟢 |
| [detection-and-learning.md](detection-and-learning.md) | 原理 | 異常偵測與學習機制現在寫了什麼：統計方法清單、回饋學習（人在迴圈）、跟強化式學習差在哪、已知哪裡不準、貝氏更新構想 | 🟡（貝氏更新是構想；誤報量測是合成資料） |
| [semiconductor-design.md](semiconductor-design.md) | 原理 | 半導體（FDC）沒有 trace id 時：lot 加工路徑取代 trace、五個模型與事件整合各要改什麼、跟目前設計的對照、做的順序、現況 | 🟡（工廠版 engine 用假資料跑通；欄位名是假設，等 MES、FDC 樣本） |
| [aiops-data-reference.md](aiops-data-reference.md) | 規格 | AIOps 資料格式（PostgreSQL 的表）：`aiops_anomalies`、`aiops_alerts`、`aiops_incidents`、`aiops_feedback`、`aiops_tuning` 的欄位、誰在什麼時候寫、真實範例、Grafana 面板讀哪些欄位 | 🟢 |
| [deploy-how-to.md](deploy-how-to.md) | 操作 | 部署：公司已有 Elastic APM、ES、PostgreSQL：在 Kibana 手動補設定、跟 DBA 申請 PostgreSQL、搬以前存在 ES 的資料、部署 aiops-engine、接 Grafana | 🟡（沒在真實環境照做過） |
| [troubleshooting.md](troubleshooting.md) | 操作 | 症狀 → 原因 → 做法 | 🟢 |
| [img/README.md](img/README.md) | 案例 | POC 五個 KPI（聚合警報、LLM 解析、端到端串查、相似事故 Top-3、RCA 全文）在 Grafana 上的截圖 | 🟢（合成資料） |

### 資料存哪裡

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [data-storage-reference.md](data-storage-reference.md) | 規格 | 資料存哪裡：Elastic APM 寫的原始資料（ES，只讀）、engine 自己的資料（PostgreSQL 的 `aiops_*` 表：訓練、異常、RCA 報告、事故、回饋、調整）誰寫、多久一次、會不會自己消失；Grafana 自己存什麼 | 🟢 |
| [data-sources-reference.md](data-sources-reference.md) | 規格 | 原始資料來源 OTel 與 Elastic APM 怎麼對齊：統一的 span、兩種格式的欄位／單位／失敗／種類對照、來源適配器要回答什麼、怎麼選、限制 | 🟡（APM 只對合成資料驗過） |
| [data-sources-how-to.md](data-sources-how-to.md) | 操作 | 新增一種原始資料來源：寫適配器、登記、補測試、對真實資料跑一次、常見的坑 | 🟢 |

### ④～⑦ 判斷、收斂、根因分析

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [rca/README.md](rca/README.md) | 原理 | 根因分析六步與 AI 的位置、三種資料怎麼串、動態基準線學習怎麼判斷、15 個故障案例的歸納、現況；graph RAG 構想（⚪ 沒有程式、沒有驗證） | 🟢 |
| [../aiops-engine/configs/baseline/reference.md](../aiops-engine/configs/baseline/reference.md) | 規格 | baseline 規格：序列、狀態、參數、規則、檢查、欄位、結果在哪 | 🟢 |
| [../aiops-engine/configs/baseline/README.md](../aiops-engine/configs/baseline/README.md) | 操作 | baseline：怎麼跑 train／detect／rules／peer／quality／chronic、怎麼調、怎麼驗 | 🟢 |
| [../aiops-engine/configs/baseline/verification.md](../aiops-engine/configs/baseline/verification.md) | 案例 | baseline 各功能的實測與驗證結果 | 🟢 |
| [../aiops-engine/configs/rca/README.md](../aiops-engine/configs/rca/README.md) | 操作 | 根因分析：預設值、怎麼跑、匯入歷史事故單、報告長什麼樣子、限制、怎麼驗 | 🟢 |
| [../aiops-engine/configs/rca/example-report.md](../aiops-engine/configs/rca/example-report.md) | 案例 | 一份範例報告 | 🟢 |
| [../aiops-engine/configs/apm/README.md](../aiops-engine/configs/apm/README.md) | 操作 | 用 Elastic APM 的資料跑 engine（收值就是這個）：哪些功能能用、ES role、template、`source: apm`、欄位對照 | 🟡（未在真實 APM 資料上跑過） |
| [../aiops-engine/configs/mixed/README.md](../aiops-engine/configs/mixed/README.md) | 操作 | OTel 與 APM 並存：`AIOPS_SOURCE=otel,apm`、trace 的 raw_index 寫清單、序列混寫、歸屬與 log 各查各的、檢查、限制 | 🟡（只有合成資料驗過） |
| [../aiops-engine/src/common/scenario/README.md](../aiops-engine/src/common/scenario/README.md) | 操作 | 情境：`AIOPS_PLUGIN` 選 observability／factory／metrics_only、三份資料檔（yaml）各填了什麼、兩個策略、哪些功能在哪個情境能用、怎麼自己寫一個 | 🟢（三個都有單元測試；factory、metrics_only 假資料 e2e 跑通） |
| [../aiops-engine/configs/factory/README.md](../aiops-engine/configs/factory/README.md) | 操作 | 工廠（`AIOPS_PLUGIN=factory`）：ML／FDC 判好的異常接進 ⑤、MES＋S6F11 合成履歷取代 trace、機台 alarm／PM／recipe 變更當 log、前後站併件、誤報匯出給 ML；要給什麼資料、怎麼跑、哪些功能能用 | 🟡（假資料跑通；欄位名是假設） |
| [../aiops-engine/src/common/README.md](../aiops-engine/src/common/README.md) | 規格 | repository 層：一個實體一個，索引名與 ES 查詢只在這裡；`Repos` 怎麼用、每個 repository 管哪個索引 | 🟢 |
| [../aiops-engine/fake-data/interfaces.md](../aiops-engine/fake-data/interfaces.md) | 規格 | 要別人提供的資料的格式 | 🟢 |
| [../aiops-engine/fake-data/README.md](../aiops-engine/fake-data/README.md) | 規格 | 假資料（只為了讓程式現在能跑、能測） | 🟢 |

### ⑧⑨ 出口與 feedback（learning loop）

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [feedback/README.md](feedback/README.md) | 原理 | 閉環怎麼走、為什麼用 LLMOps 不自己訓練模型、紀律、LLM 何時叫與 token、現況 | 🟢 |
| [../grafana/README.md](../grafana/README.md) | 操作 | Grafana：讀 aiops_alerts 發通知、儀表板、回饋入口（預設連到 engine api 的網頁，外掛選用）、用 Helm 裝 | 🟢 |
| [../grafana/code/INSTALL-form-plugin.md](../grafana/code/INSTALL-form-plugin.md) | 操作 | 給管 Grafana 的人：裝回饋表單外掛（選用：要在 Grafana 裡直接填才需要） | 🟢 |

### 部署

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [../aiops-engine/README.md](../aiops-engine/README.md) | 操作 | engine：build image、Helm 的兩種 mode（一隻 pod／api＋beat＋worker）、API 端點、排程表、「AIOps 停了」 | 🟢（分散模式本機對測試 ES＋Redis 跑過，沒 helm 實裝） |
| [../aiops-engine/tests/tight/README.md](../aiops-engine/tests/tight/README.md) | 操作 | 最緊資源測試怎麼跑：池子 1～2 條、1 CPU、記憶體壓到 train 放不下、日誌磁碟 256 KB、重試 0 秒 | 🟢（本機跑過，沒有 ES，部分工作是替身） |

## 依類型找

| 我要… | 看這些 |
|---|---|
| **懂為什麼這樣設計**（原理） | [architecture.md](architecture.md)、[rca](rca/README.md)・[feedback](feedback/README.md) 的 README（各自也含規格、操作、案例章節） |
| **做一件事**（操作） | [部署](deploy-how-to.md)、[troubleshooting.md](troubleshooting.md)、[接 Elastic APM 的資料](../aiops-engine/configs/apm/README.md)、[選情境／自己寫一份](../aiops-engine/src/common/scenario/README.md)、[工廠怎麼接](../aiops-engine/configs/factory/README.md)、[baseline 怎麼跑](../aiops-engine/configs/baseline/README.md)、[在 ES 補設定](../elasticsearch/kibana-dev-tools.txt) |
| **查欄位、參數、現況**（規格） | [aiops-data-reference.md](aiops-data-reference.md)、[data-storage-reference.md](data-storage-reference.md)、[baseline 規格](../aiops-engine/configs/baseline/reference.md) |
| **看實例**（案例） | [baseline 實測](../aiops-engine/configs/baseline/verification.md)、[範例報告](../aiops-engine/configs/rca/example-report.md)；故障案例的歸納在 [rca/README.md](rca/README.md) |

## PDCA 紀錄

每次工作一份，依 phase 分資料夾；現況與待辦在 [pdca/README.md](pdca/README.md)，每一輪紀錄的索引在 [pdca/records.md](pdca/records.md)，寫法見 [pdca/TEMPLATE.md](pdca/TEMPLATE.md)。PDCA 是紀錄，不是文件，不分類型。
