# 整合儀表板：metrics → trace → log → AI 根因（Elastic APM 資料）（2026-11）

**狀態**：進行中　　**日期**：2026-11-06　　**相關**：分支 `claude/apm-dashboard`（從 master `8ed32ba` 開）；舊的示範儀表板（`e2e/k8s/rca-demo/dashboard.json`，2026-11-02 隨自建收值刪除，截圖 [rca-demo-dashboard.png](../phase3-locate/img/rca-demo-dashboard.png)）；[APM 來源](2026-11-apm-source.md)
**模組**：⑧ 出口（Grafana）

## Plan

- **需求**：以前有一頁把「metrics 哪裡異常 → trace 慢在哪一段 → log 那幾段發生什麼事 → AI 根因」放在同一頁，值班一眼看完。自建收值刪掉後這頁跟著沒了；現在的三個儀表板（事故與根因、學習迴圈、回饋）都只看 AIOps 自己寫的索引，看不到原始的 metrics、trace、log。
- **目的**：重做成讀公司 Elastic APM 的整合儀表板，上半是原始資料（APM 的 metrics、trace、log），下半接現有的事故與根因面板。
- **範圍**：
  - Grafana 多三個唯讀 data source：`apm-metrics`（`metrics-apm.transaction.1m-*`）、`apm-traces`（`traces-apm-*`）、`apm-logs`（`logs-apm.error-*`、`logs-apm.app.*`）；`elasticsearch/kibana-dev-tools.txt` 第 4 段 Grafana key 的讀取範圍同步加。
  - 新儀表板 `aiops-overview`：①延遲 p95、②請求數與錯誤率、③trace 每一段耗時、④error log（帶 `trace.id`）、⑤AIOps 事件與 AI 摘要、⑥根因報告全文。選一個服務與時間範圍，整頁跟著動。
  - 驗證用的合成 APM 資料 `tests/apm_synth.py`：欄位照 `configs/apm/README.md` 對照表（`transaction.duration.histogram`、`processor.event`、`span.destination.service.resource`…），一個「下游資料庫連線池滿」的事故。用 engine 自己跑 `rules`→`trace-merge`→`alert` 產生事件與報告，不手寫 AIOps 的文件。
- **不做**：不動 engine 的判斷邏輯；不接 K8s 狀態與 Pod CPU／記憶體（APM 沒送，見 `configs/apm/README.md`）；不取代現有三個儀表板。
- **風險**：沒有真的公司 APM 資料，欄位以官方 mapping 與 README 對照表為準，合成資料只能證明「面板語法對、欄位對得上我們的假設」，不能證明公司環境的欄位一模一樣——上線前照 README 第 4 步用 `GET <索引>/_search?size=1` 對一次。

## Do

（做完補）

## Check

（做完補）

## Act

（做完補）
