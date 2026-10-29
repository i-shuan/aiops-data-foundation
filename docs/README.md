# 文件地圖

全 repo 的 md 都從這裡找得到。**文件留在原位**（跟程式強相關的放在 `engine/`、`sdk/`、`e2e/` 旁，改程式時才會順手改），這份只做索引和標類型、狀態。每次新增、刪除 md 都要更新這裡，`python3 tools/check-docs.py` 會檢查（沒列進來、連結斷了、用了舊說法、共用文件沒標類型都會失敗）。規則見 [CLAUDE.md](../CLAUDE.md)。

**類型**（Diátaxis：每份文件只做一種事）：
**原理**＝為什麼這樣設計　**操作**＝我要做 X 怎麼做　**規格**＝查欄位、參數、現況　**案例**＝實際跑一遍的實例

**狀態**：🟢 已實作（對得到程式與驗證）　🟡 定案方向、部分實作　⚪ 構想，尚未實作

**第一次來，這樣讀**：[專案 README](../README.md) → [architecture.md](architecture.md)（架構與設計決策）→ 要看哪個環節就進下面對應的主題。要「做事」直接找操作；要「查」找規格。

## 依主題

### 總覽

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [../README.md](../README.md) | 原理 | 專案是什麼、怎麼跑起來 | 🟢 |
| [architecture.md](architecture.md) | 原理 | 架構與設計決策：目標、全域架構圖、關鍵決策、①～⑨ 模組職責、根因定位機制 | 🟢 |
| [architecture-reference.md](architecture-reference.md) | 規格 | 現況與驗證對照：各 phase、各模組的程式、驗證、還沒做的、PDCA 索引、術語對照 | 🟢 |
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
| [log/README.md](log/README.md) | 原理 | 資料怎麼走、現況與待辦 | 🟢 |
| [log/reference.md](log/reference.md) | 規格 | 欄位規範、pipeline 規則、index、檔案 | 🟢 |
| [log/how-to.md](log/how-to.md) | 操作 | 部署、驗證、坑 | 🟢 |
| [log/fluentbit-vs-otel.md](log/fluentbit-vs-otel.md) | 原理 | Fluent Bit vs OTel Collector 的實測比較與決定 | 🟢 |

### ①②③ 收值、清資料、存：metrics

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [metrics/README.md](metrics/README.md) | 原理 | 資料怎麼走、為什麼依 namespace 分、為什麼用 OTLP | 🟢 |
| [metrics/reference.md](metrics/reference.md) | 規格 | 檔案位置、ES 欄位 | 🟢 |
| [metrics/how-to.md](metrics/how-to.md) | 操作 | 應用要做什麼、部署、驗證、坑 | 🟢 |
| [metrics/catalog.md](metrics/catalog.md) | 規格 | 收了哪些指標、資料結構、哪些有 baseline | 🟢 |
| [metrics/capabilities.md](metrics/capabilities.md) | 原理 | metrics 能做到什麼、做不到什麼 | 🟢 |
| [metrics/cases.md](metrics/cases.md) | 案例 | 四個領域的典型案例與實測（含 .NET 實測） | 🟢 |
| [metrics/dotnet.md](metrics/dotnet.md) | 原理 | .NET：為什麼選自動埋點＋推、跟 Java 的差異 | 🟢（.NET Framework、Windows 非 K8s 未驗證） |
| [metrics/dotnet-how-to.md](metrics/dotnet-how-to.md) | 操作 | .NET：chart、Collector、Kafka、坑 | 🟢 |
| [metrics/dotnet-reference.md](metrics/dotnet-reference.md) | 規格 | .NET：自動埋點產生的指標名稱、baseline 與規則 | 🟢 |
| [../sdk/standards/tracing.md](../sdk/standards/tracing.md) | 操作 | 給應用團隊的 trace 埋點指引 | 🟢 |

### ①②③ 收值、清資料、存：trace

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [trace/README.md](trace/README.md) | 原理 | 資料怎麼走、跟 log、metrics 串起來、現況與待辦 | 🟢 |
| [trace/reference.md](trace/reference.md) | 規格 | ES 欄位 | 🟢 |
| [trace/how-to.md](trace/how-to.md) | 操作 | 應用要做什麼、部署、驗證、坑 | 🟢 |
| [trace/scenarios.md](trace/scenarios.md) | 案例 | trace 能做什麼：六個場景對照現況 | 🟢 |

### ④～⑦ 判斷、收斂、根因分析

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [rca/README.md](rca/README.md) | 原理 | 根因分析怎麼做、哪裡需要 AI；四種 baseline 各適合什麼；兩階段推進 | 🟢 |
| [rca/baseline.md](rca/baseline.md) | 原理 | baseline 原理：為什麼用中位數和 MAD、方法本身抓不到什麼 | 🟢 |
| [rca/cases.md](rca/cases.md) | 案例 | 15 個真實故障案例：要哪種「平常」才抓得到 | 🟢 |
| [rca/graph-rag.md](rca/graph-rag.md) | 原理 | RCA ＋ LLM 搭配 graph RAG | ⚪ 沒有程式、沒有驗證 |
| [../engine/configs/baseline/README.md](../engine/configs/baseline/README.md) | 規格＋操作（混合，未拆） | baseline：怎麼判斷、參數、怎麼跑、結果在哪、怎麼驗 | 🟢 |
| [../engine/configs/rca/README.md](../engine/configs/rca/README.md) | 操作 | 根因分析：預設值、怎麼跑、報告長什麼樣子、限制、怎麼驗 | 🟢 |
| [../engine/configs/rca/example-report.md](../engine/configs/rca/example-report.md) | 案例 | 一份範例報告 | 🟢 |
| [../engine/interfaces/README.md](../engine/interfaces/README.md) | 規格 | 要別人提供的資料的格式 | 🟢 |
| [../engine/fake-data/README.md](../engine/fake-data/README.md) | 規格 | 假資料（只為了讓程式現在能跑、能測） | 🟢 |

### ⑧⑨ 出口與 feedback（learning loop）

| 文件 | 類型 | 內容 | 狀態 |
|---|---|---|---|
| [feedback/README.md](feedback/README.md) | 原理 | 告警出口、事故紀錄、回饋、調整、什麼時候叫 LLM | 🟢 |
| [feedback/llm-mlops.md](feedback/llm-mlops.md) | 原理 | 後期接 LLM 還是 MLOps：定案走 LLMOps | 🟡 LLM 摘要已用真的模型跑過；評測集、版本管理未做 |
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
| **懂為什麼這樣設計**（原理） | [architecture.md](architecture.md)、[rca/README.md](rca/README.md)、[rca/baseline.md](rca/baseline.md)、[log](log/README.md)・[metrics](metrics/README.md)・[trace](trace/README.md) 的 README、[metrics/capabilities.md](metrics/capabilities.md)、[metrics/dotnet.md](metrics/dotnet.md)、[retention.md](retention.md)、[feedback/README.md](feedback/README.md)、[feedback/llm-mlops.md](feedback/llm-mlops.md)、[log/fluentbit-vs-otel.md](log/fluentbit-vs-otel.md) |
| **做一件事**（操作） | [troubleshooting.md](troubleshooting.md)、[log/how-to.md](log/how-to.md)、[metrics/how-to.md](metrics/how-to.md)、[metrics/dotnet-how-to.md](metrics/dotnet-how-to.md)、[trace/how-to.md](trace/how-to.md)、[retention-how-to.md](retention-how-to.md)、[engine/deploy](../engine/deploy/README.md) |
| **查欄位、參數、現況**（規格） | [log/reference.md](log/reference.md)、[metrics/reference.md](metrics/reference.md)、[metrics/catalog.md](metrics/catalog.md)、[metrics/dotnet-reference.md](metrics/dotnet-reference.md)、[trace/reference.md](trace/reference.md)、[retention-reference.md](retention-reference.md)、[architecture-reference.md](architecture-reference.md) |
| **看實例**（案例） | [metrics/cases.md](metrics/cases.md)、[rca/cases.md](rca/cases.md)、[trace/scenarios.md](trace/scenarios.md)、[範例報告](../engine/configs/rca/example-report.md) |

## PDCA 紀錄

每次工作一份，依 phase 分資料夾；索引、各 phase 的結論在 [pdca/README.md](pdca/README.md)，寫法見 [pdca/TEMPLATE.md](pdca/TEMPLATE.md)。PDCA 是紀錄，不是文件，不分類型。
