# 文件地圖

全 repo 的 md 都從這裡找得到。**文件留在原位**（跟程式強相關的放在 `engine/`、`sdk/`、`e2e/` 旁，改程式時才會順手改），這份只做索引和標類型、狀態。每次新增、刪除 md 都要更新這裡，`python3 tools/check-docs.py` 會檢查（沒列進來、連結斷了、用了舊說法、共用文件沒標類型都會失敗）。規則見 [CLAUDE.md](../CLAUDE.md)。

**類型**（Diátaxis：每份文件只做一種事；log、metrics、trace、rca、feedback 各合成一份 README，原理在前，規格、操作、案例是章節）：
**原理**＝為什麼這樣設計　**操作**＝我要做 X 怎麼做　**規格**＝查欄位、參數、現況　**案例**＝實際跑一遍的實例

**狀態**：🟢 已實作（對得到程式與驗證）　🟡 定案方向、部分實作　⚪ 構想，尚未實作

**第一次來，這樣讀**：[專案 README](../README.md) → [architecture.md](architecture.md)（架構與設計決策）→ 要看哪個環節就進下面對應的主題。要「做事」直接找操作；要「查」找規格。

## 依主題

### 總覽

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [../README.md](../README.md) | 原理 | 專案是什麼、怎麼跑起來 | 🟢 |
| [architecture.md](architecture.md) | 原理 | 架構、設計決策與現況：目標、全域架構圖、關鍵決策、①～⑨ 模組職責、根因定位機制（第 1～5 節）；各 phase、各模組的程式、驗證、還沒做的、PDCA 索引（第 6～14 節） | 🟢 |
| [aiops-data-reference.md](aiops-data-reference.md) | 規格 | AIOps 資料格式：`aiops-anomalies`、`aiops-alerts`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning` 的欄位、誰在什麼時候寫、真實範例、Grafana 面板讀哪些欄位 | 🟢 |
| [troubleshooting.md](troubleshooting.md) | 操作 | 症狀 → 原因 → 做法 | 🟢 |

### 保留天數

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [retention.md](retention.md) | 原理 | 為什麼這樣定天數與容量 | 🟢 |
| [retention-reference.md](retention-reference.md) | 規格 | 現在的設定、每筆資料實測佔多少 | 🟢 |
| [retention-how-to.md](retention-how-to.md) | 操作 | 怎麼套用、怎麼改 | 🟢 |

### ①②③ 收值、清資料、存：log

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [log/README.md](log/README.md) | 原理 | 資料怎麼走、關鍵決定（含 Fluent Bit vs OTel）、欄位規格、部署與新增格式、現況 | 🟢 |

### ①②③ 收值、清資料、存：metrics

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [metrics/README.md](metrics/README.md) | 原理 | 資料怎麼走、關鍵決定、欄位、收了哪些指標與哪些有 baseline、能做到與做不到、.NET 與 Java、部署與驗證、現況 | 🟢（.NET Framework、Windows 非 K8s 未驗證） |
| [../sdk/standards/tracing.md](../sdk/standards/tracing.md) | 操作 | 給應用團隊的 trace 埋點指引 | 🟢 |

### ①②③ 收值、清資料、存：trace

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [trace/README.md](trace/README.md) | 原理 | 資料怎麼走、尾端取樣、欄位、跟 log、metrics 串、部署與驗證、六個場景、現況 | 🟢 |

### ④～⑦ 判斷、收斂、根因分析

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [rca/README.md](rca/README.md) | 原理 | 根因分析六步與 AI 的位置、三種資料怎麼串、動態基準線學習怎麼判斷、15 個故障案例的歸納、現況；graph RAG 構想（⚪ 沒有程式、沒有驗證） | 🟢 |
| [../engine/configs/baseline/reference.md](../engine/configs/baseline/reference.md) | 規格 | baseline 規格：序列、狀態、參數、規則、檢查、欄位、結果在哪 | 🟢 |
| [../engine/configs/baseline/README.md](../engine/configs/baseline/README.md) | 操作 | baseline：怎麼跑 train／detect／rules／peer／quality／chronic、怎麼調、怎麼驗 | 🟢 |
| [../engine/configs/baseline/verification.md](../engine/configs/baseline/verification.md) | 案例 | baseline 各功能的實測與驗證結果 | 🟢 |
| [../engine/configs/rca/README.md](../engine/configs/rca/README.md) | 操作 | 根因分析：預設值、怎麼跑、報告長什麼樣子、限制、怎麼驗 | 🟢 |
| [../engine/configs/rca/example-report.md](../engine/configs/rca/example-report.md) | 案例 | 一份範例報告 | 🟢 |
| [../engine/interfaces/README.md](../engine/interfaces/README.md) | 規格 | 要別人提供的資料的格式 | 🟢 |
| [../engine/fake-data/README.md](../engine/fake-data/README.md) | 規格 | 假資料（只為了讓程式現在能跑、能測） | 🟢 |

### ⑧⑨ 出口與 feedback（learning loop）

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [feedback/README.md](feedback/README.md) | 原理 | 閉環怎麼走、為什麼用 LLMOps 不自己訓練模型、紀律、LLM 何時叫與 token、現況 | 🟢 |
| [../engine/configs/grafana/README.md](../engine/configs/grafana/README.md) | 操作 | Grafana：讀 aiops-alerts 發通知、儀表板 | 🟢 |
| [../engine/configs/grafana/INSTALL-form-plugin.md](../engine/configs/grafana/INSTALL-form-plugin.md) | 操作 | 給管 Grafana 的人：裝回饋表單外掛 | 🟢 |

### 部署與測試環境

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [../engine/deploy/README.md](../engine/deploy/README.md) | 操作 | K8s CronJob、回饋網頁、「AIOps 停了」 | 🟢（K8s 排程本身還沒跑過） |
| [../engine/deploy/grafana/README.md](../engine/deploy/grafana/README.md) | 操作 | Grafana 跑在 K8s：用 initContainer 裝外掛 | 🟢 |
| [../e2e/local-k3d/README.md](../e2e/local-k3d/README.md) | 操作 | 本機 k3d（K8s 實測） | 🟢 |
| [../e2e/local-k3s/tail-sampling/README.md](../e2e/local-k3s/tail-sampling/README.md) | 案例 | 尾端取樣實測 | 🟢 |

## 依類型找

| 我要… | 看這些 |
|---|---|
| **懂為什麼這樣設計**（原理） | [architecture.md](architecture.md)、[log](log/README.md)・[metrics](metrics/README.md)・[trace](trace/README.md)・[rca](rca/README.md)・[feedback](feedback/README.md) 的 README（各自也含規格、操作、案例章節）、[retention.md](retention.md) |
| **做一件事**（操作） | [troubleshooting.md](troubleshooting.md)、[retention-how-to.md](retention-how-to.md)、[engine/deploy](../engine/deploy/README.md)、[baseline 怎麼跑](../engine/configs/baseline/README.md)；log、metrics、trace 的部署與驗證在各自 README 的「操作」「部署與驗證」章節 |
| **查欄位、參數、現況**（規格） | [aiops-data-reference.md](aiops-data-reference.md)、[retention-reference.md](retention-reference.md)、[baseline 規格](../engine/configs/baseline/reference.md)；log、metrics、trace 的欄位在各自 README 的「規格」「欄位」章節 |
| **看實例**（案例） | [baseline 實測](../engine/configs/baseline/verification.md)、[範例報告](../engine/configs/rca/example-report.md)、[tail-sampling 實測](../e2e/local-k3s/tail-sampling/README.md)；故障案例的歸納在 [rca/README.md](rca/README.md) |

## PDCA 紀錄

每次工作一份，依 phase 分資料夾；索引、各 phase 的結論在 [pdca/README.md](pdca/README.md)，寫法見 [pdca/TEMPLATE.md](pdca/TEMPLATE.md)。PDCA 是紀錄，不是文件，不分類型。
