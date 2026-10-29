# 文件地圖

全 repo 的 md 都從這裡找得到。**文件留在原位**（跟程式強相關的放在 `engine/`、`sdk/`、`e2e/` 旁，改程式時才會順手改），這份只做索引和標狀態。每次新增、刪除 md 都要更新這裡，`python3 tools/check-docs.py` 會檢查（沒列進來、連結斷了、用了舊說法都會失敗）。規則見 [CLAUDE.md](../CLAUDE.md)。

**狀態**：🟢 已實作（對得到程式與驗證）　🟡 定案方向、部分實作　⚪ 構想，尚未實作

**第一次來，這樣讀**：[專案 README](../README.md) → [architecture.md](architecture.md)（規格、現況、術語表）→ 要看哪個環節就進下面對應的那一份。

## 架構與總覽

| 文件 | 內容 | 狀態 |
|---|---|---|
| [../README.md](../README.md) | 專案是什麼、怎麼跑起來 | 🟢 |
| [architecture.md](architecture.md) | 系統架構規格書：目標、①～⑨ 模組、關鍵決策、各 phase 現況、還沒做的、**術語對照** | 🟢 |
| [retention.md](retention.md) | 保留天數與容量 | 🟢 |
| [troubleshooting.md](troubleshooting.md) | 症狀 → 原因 → 做法 | 🟢 |

## ①②③ 收值、清資料、存：log、metrics、trace

| 文件 | 內容 | 狀態 |
|---|---|---|
| [log/README.md](log/README.md) | log 進 ES：欄位規範、pipeline、部署、驗證 | 🟢 |
| [log/fluentbit-vs-otel.md](log/fluentbit-vs-otel.md) | log 收集：Fluent Bit vs OTel Collector 的實測比較 | 🟢 |
| [metrics/README.md](metrics/README.md) | metrics 進 ES | 🟢 |
| [metrics/catalog.md](metrics/catalog.md) | 收了哪些指標、哪些有 baseline | 🟢 |
| [metrics/cases.md](metrics/cases.md) | 指標的四個領域：典型案例與實測 | 🟢 |
| [metrics/dotnet.md](metrics/dotnet.md) | 應用是 .NET 時 | 🟢（.NET Framework、Windows 非 K8s 未驗證） |
| [trace/README.md](trace/README.md) | trace 進 ES | 🟢 |
| [trace/scenarios.md](trace/scenarios.md) | trace 能做什麼：六個場景對照現況 | 🟢 |
| [../sdk/standards/tracing.md](../sdk/standards/tracing.md) | 給應用團隊的 trace 埋點指引 | 🟢 |

## ④～⑦ 判斷、收斂、根因分析

| 文件 | 內容 | 狀態 |
|---|---|---|
| [rca/README.md](rca/README.md) | 根因分析怎麼做、哪裡需要 AI；baseline 的道理；兩階段推進 | 🟢 |
| [rca/cases.md](rca/cases.md) | 15 個真實故障案例：要哪種「平常」才抓得到 | 🟢 |
| [rca/graph-rag.md](rca/graph-rag.md) | RCA ＋ LLM 搭配 graph RAG | ⚪ 沒有程式、沒有驗證 |
| [../engine/configs/baseline/README.md](../engine/configs/baseline/README.md) | baseline：怎麼學、怎麼判斷、怎麼跑、參數 | 🟢 |
| [../engine/configs/rca/README.md](../engine/configs/rca/README.md) | 根因分析：怎麼跑、報告長什麼樣子、限制 | 🟢 |
| [../engine/configs/rca/example-report.md](../engine/configs/rca/example-report.md) | 一份範例報告 | 🟢 |
| [../engine/interfaces/README.md](../engine/interfaces/README.md) | 要別人提供的資料的格式 | 🟢 |
| [../engine/fake-data/README.md](../engine/fake-data/README.md) | 假資料（只為了讓程式現在能跑、能測） | 🟢 |

## ⑧⑨ 出口與 feedback（learning loop）

| 文件 | 內容 | 狀態 |
|---|---|---|
| [feedback/README.md](feedback/README.md) | 告警出口、事故紀錄、回饋、調整、LLM | 🟢 |
| [feedback/llm-mlops.md](feedback/llm-mlops.md) | 後期接 LLM 還是 MLOps：定案走 LLMOps | 🟡 LLM 摘要已用真的模型跑過；評測集、版本管理未做 |
| [../engine/configs/grafana/README.md](../engine/configs/grafana/README.md) | Grafana：讀 aiops-alerts 發通知、儀表板 | 🟢 |
| [../engine/configs/grafana/INSTALL-form-plugin.md](../engine/configs/grafana/INSTALL-form-plugin.md) | 給管 Grafana 的人：裝回饋表單外掛 | 🟢 |

## 部署與測試環境

| 文件 | 內容 | 狀態 |
|---|---|---|
| [../engine/deploy/README.md](../engine/deploy/README.md) | K8s CronJob、回饋網頁、「AIOps 停了」 | 🟢（K8s 排程本身還沒跑過） |
| [../engine/deploy/grafana/README.md](../engine/deploy/grafana/README.md) | Grafana 跑在 K8s：用 initContainer 裝外掛 | 🟢 |
| [../e2e/local-k3d/README.md](../e2e/local-k3d/README.md) | 本機 k3d（K8s 實測） | 🟢 |
| [../e2e/local-k3s/tail-sampling/README.md](../e2e/local-k3s/tail-sampling/README.md) | 尾端取樣實測 | 🟢 |

## PDCA 紀錄

每次工作一份，依 phase 分資料夾；索引、各 phase 的結論在 [pdca/README.md](pdca/README.md)，寫法見 [pdca/TEMPLATE.md](pdca/TEMPLATE.md)。
