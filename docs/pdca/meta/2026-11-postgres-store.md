# engine 自己的資料從 ES 搬到 PostgreSQL，相似事故加向量檢索（2026-11）

**狀態**：Plan　　**日期**：2026-11-07　　**相關**：分支 `claude/postgres-store`（從 `claude/docs-module-map` 開：要改那邊新增的 module-map.md）
**模組**：③ 存（全部 `aiops-*`）、⑦⑨（相似事故 RAG）、⑧（Grafana 資料來源）

## Plan：這一次要達到什麼

- **需求**：使用者要把 engine 的資料全部搬到 PostgreSQL（「未來其他系統要復用也比較簡單」），並加 RAG。三種資料：異常、基準線、告警（量大、依時間查、聚合）；事故、回饋（量小、欄位比對找相似）；調整、對照覆寫（量很小、有狀態與紀錄）。
- **使用者的決定**：①**一次切過去，直接刪 ES 存取**（不留兩套）；②RAG **欄位比對為主、向量輔助**；③PostgreSQL 是**公司現有的、要申請**。
- **範圍**
  - **搬**：engine 自己寫的 `aiops-anomalies`、`aiops-baseline`、`aiops-alerts`、`aiops-incidents`、`aiops-feedback`、`aiops-tuning`、`aiops-mapping`、`aiops-state`、`aiops-chronic`、`aiops-trace-segments` → PostgreSQL 一張表一個。
  - **不搬**：Elastic APM 寫的原始資料（公司的 ES，engine 只讀）；`traces-merged-*`（從原始 trace 合成、跟原始資料一起由 ES 的 ILM 管，根因分析用 ES 的 nested 查詢）。
  - **只用標準 PostgreSQL**（14 以上、jsonb、GIN、BRIN），不用 TimescaleDB；`pgvector` 有就用、沒有也能跑（向量存一般欄位、在程式裡算相似度，事故只有幾十到幾百件）。
  - Grafana 的儀表板與告警規則改讀 PostgreSQL。
  - 既有資料：提供一次性的「從 ES 匯入」指令。
- **設計**
  - 一張表 `id`（文件 id）、`ts`（這張表的時間欄）、`doc jsonb`（整份文件）；常查的欄位加運算式索引。存取類別對外的方法不變，裡面改成 SQL：呼叫端幾乎不用動，`tests/fakes.MemoryRepos` 照用。
  - 表格名、SQL 只在各功能的 `store/` 與 `common/`（跟原本「索引名只在 store」同一條規則）。
  - 事故被確認或匯入後才算向量；embedding 模型用設定的 OpenAI 相容 API，沒設就只用欄位比對。
