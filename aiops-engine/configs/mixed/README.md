# OTel 與 APM 並存

> **類型：操作 How-To**　兩種格式怎麼對齊見 [docs/data-sources-reference.md](../../../docs/data-sources-reference.md)；單一來源的設定見 [../apm/README.md](../apm/README.md)、[../baseline/README.md](../baseline/README.md)

一部分服務用 OTel、一部分用 APM，同時給同一個 engine 看。**並存是「每個資料來源各自翻成統一格式」**，後面的判斷、告警、根因不認得來源。

## 設定

1. **宣告兩個來源**：環境變數 `AIOPS_SOURCE=otel,apm`（helm：`--set 'source=otel\,apm'`，逗號要跳脫）。第一個是**主要的**：排程表、RCA 設定的 `indices.logs`、同一個服務兩邊都有時的歸屬都跟它。
2. **trace**：`raw_index` 寫清單，每個索引帶自己的 `source`（範例 [trace.yaml](trace.yaml)）。trace-merge 會讀兩個索引、合成同一筆；一個請求跨過兩套系統時，呼叫鏈接得起來。
3. **序列（metrics、log 行數…）**：同一份 `series.yaml` 裡直接混寫兩種——每條序列本來就自己帶 `index`、`by`、`value`、`scale`，沒有全域的來源。**`name` 不能重複**（異常、基準線、調整都用名字認序列；重複載入時會被擋下）：兩邊同名的指標加前綴（`otel.http.requests`、`apm.http.requests`）。`interval`、`lag` 是整份檔一個值，取兩邊裡粗的（`interval: 1m`、`lag: 90s`）；OTel 的序列想用 30 秒就另外一份 `series.yaml` 跑另一個排程工作。
4. **歸屬（dept、group）、根因分析讀的 log**：兩個來源各查各的（`common/scenario/it_otel.yaml`、`it_apm.yaml` 的 `owner`、`log`）。log 的次要來源要在那份檔裡有 `index`（已寫好：OTel `k8s-*`、APM `logs-apm.app.*,logs-apm.error-*`）。

## 檢查

```bash
AIOPS_SOURCE=otel,apm python3 -m main detector source-check --config configs/mixed/series.yaml --trace-config configs/mixed/trace.yaml
```

只回報、不擋；每輪 `detect`、`trace-merge` 也會自動檢查並寫狀態文件，Grafana 的「AIOps 來源對不上」規則叫人。端到端測試：`python3 -X utf8 -m tests.mixed_e2e`（造一個跨兩個來源的請求，看合成後是同一筆、呼叫鏈接得起來）。

## 限制（沒做的）

- **同一個服務兩邊都送資料**：服務名（`service.name`）要對得上，異常才會併成同一件；目前沒有「兩邊服務名不同怎麼對」的規則。**不要讓同一個服務兩邊都送**（指標會算兩次）。
- **log 只有 `of_traces`（這個請求的 log）與 `problems`（warn、error）會查兩個來源**；`changes`（工廠的變更紀錄）、`daily_counts`（慢性病清單的每日筆數）只看主要來源。
- **粒度**：兩種來源的最細粒度不同（OTel 30 秒、APM 1 分鐘）；混在一份 `series.yaml` 時用粗的那個。
- **只有合成資料驗過**（`tests/mixed_e2e.py`、`tests/test_source_mixed.py`），沒對真實的公司資料跑過。
