# 相容性：Elasticsearch 8.19.23、Grafana 13.0.7（2026-10）

**狀態**：完成（docker 實測；Collector 只用最小設定測寫入路徑，沒測 DaemonSet、耐久、壓測）　　**日期**：2026-10-30　　**相關**：分支 `claude/compat-es8-grafana13`
**模組**：③ 存、⑧ 出口（也會影響 ① 收值：Collector 寫 ES 的入口）

## Plan：這一次要達到什麼

- **需求**：公司實際的版本是 Elasticsearch **8.19.23**、Grafana **13.0.7**；repo 一直以 ES **9.5.5**、Grafana **12.2** 開發與驗證。兩邊差一個大版本，不知道哪些會壞。
- **目的**：用這兩個版本把現有的東西各跑一次，列出「能用、要改、不能用」。不先改程式；壞的先記下來，再決定改哪邊。
- **範圍**：
  1. ES 8.19：`elasticsearch-setup.sh`（ILM、template、pipeline、角色、key）、`/_otlp/v1/*` 寫入入口、TSDS、failure store、`e2e/pipeline` 的 pipeline 案例、engine 的讀寫（`loop_e2e`）。
  2. Grafana 13：provisioning（資料源、儀表板、告警規則、contact point）、Business Forms 外掛 6.3.1 與較新版本、回饋表單實際送出。
- **已知的疑點（動手前的推測，待驗）**：ES 的 OTLP 原生入口（`/_otlp/v1/metrics`…）是 9.x 才加的，8.19 可能沒有，那 Collector 的三條出口都要改走別的路；Grafana 12.2 只能裝 6.3.1 的外掛，13 可能要換新版。

## Do：預計怎麼做

docker 起 ES 8.19（9201）與 Grafana 13（3001），跟現有 9.5.5／12.2 的容器並存；逐項跑、逐項記。

## Check：測試的目的與結果

環境：docker；ES 8.19.23（9201）、Grafana 13.0.7（3001）；對照組是同一台上的 ES 9.5.5、Grafana 12.2。

### Elasticsearch 8.19.23

| 目的 | 怎麼驗 | 結果 |
|---|---|---|
| ES 自己的 OTLP 入口在不在 | `POST /_otlp/v1/{metrics,logs,traces}` | **沒有**：`no handler found for uri [/_otlp/v1/metrics]`。Collector 現在三條出口都走這裡（`common.yaml` 的 `otlphttp`），**8.19 一定要改** |
| `elasticsearch-setup.sh` 跑不跑得完 | 整支跑一次 | exit 0；ILM、component／index template、三條 pipeline（`k8s-logs`、`k8s-logs-otel`、`traces-otel`…）、角色、4 把 key 都建好 |
| TSDS、look_ahead_time、logsdb | 看建出來的 index settings | metrics 是 `time_series`、`look_ahead_time: 2h` 有吃；logs、traces 是 `logsdb` |
| failure store | 建一個 `k8s-it-x` data stream 看 `_options` | `failure_store.enabled: true`，預設 30 天；**8.19 支援** |
| 中央 pipeline 的規則 | `e2e/pipeline` 四支 `_simulate` 案例 | trace 7、level 24、timestamp 12、trace_id 34：**全過** |
| engine 讀寫（④⑤⑥⑦⑨） | `tests.loop_e2e` 對著 8.19 | 36 PASS、1 FAIL。那個 FAIL（`metrics` 的「有回饋的比例」）是測試的日期窗到 10-28、今天是 10-30，回饋的 `@timestamp` 落在窗外；對著 9.5.5 今天也會 FAIL，**不是 8.19 的問題**（記在 Act） |
| Collector 不走 `_otlp`、改用 `elasticsearch` exporter 寫 8.19 | otelcol-contrib 0.162.0 docker，`elasticsearch` exporter、`mapping.mode: otel`、資源帶 `group`、`k8s.cluster.name`、`data_stream.namespace`；OTLP/JSON 各送一筆 | log 落到 `k8s-it-test`、被 `k8s-logs-otel` → `k8s-logs` 整理成一樣的形狀（`message`、`log.level`、`trace.id`、`agent.type: otelcol`）；metrics 落 `metrics-generic.otel-it`（TSDS）；trace 落 `traces-generic.otel-it`，`default_pipeline: traces-otel` 有掛上。**文件形狀跟 9.5 的 `_otlp` 入口一樣，中央 pipeline 不用改** |
| 同上，exporter 設定差異 | 0.162 的 `elasticsearch` exporter | `flush` 已拿掉（改 `sending_queue`）；`*_dynamic_index` 已是預設、會印 deprecated 警告 |

### Grafana 13.0.7

| 目的 | 怎麼驗 | 結果 |
|---|---|---|
| provisioning | 資料源、三個儀表板、告警規則、contact point、policy 原檔掛上 | 全部載入，沒有錯誤；兩條規則、三個 contact point、依 dept 的 policy 都在 |
| ES 8.19 資料源 | 唯讀 key，health | healthy |
| Business Forms 外掛 | 現有的 6.3.1 zip 掛進去 | 載入、簽章 valid；grafana.com 列的版本：6.3.1 要 ≥ 11.0，6.3.2～6.3.5 要 ≥ 12.3；**13 兩種都能裝**，可以直接用新版 |
| 回饋表單實際送出 | Playwright：預填、選「真的」、填根因、送出 | 預填標題／相似事故／AI 摘要都有；`POST /api/feedback` 200；`aiops-incidents` 變 `confirmed`、記確認人 |
| RCA、學習迴圈儀表板 | 載入 | 有資料（跟 12.2 一樣的 JSON） |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| **Collector 三條出口要改**：`otlphttp` → ES `_otlp` 在 8.19 不存在 | 第一個檢查 | 8.19 上 Collector 寫不進任何資料 | 改成 `elasticsearch` exporter（`mapping.mode: otel`）。`common.yaml` 136 行註解寫過當初不用它的理由：「連線逾時當永久錯誤、不重試」。換回去之前要在 k3s 上重跑 [durability](../phase1-collect/2026-10-durability.md)、[load-test](../phase1-collect/2026-10-load-test.md) 的 ES 停機、限流情境 |
| 只測了最小設定 | docker 單一 Collector、三筆資料 | DaemonSet、trace-sampler 兩台、`k8s_attributes`、尾端取樣沒跟 8.19 一起跑 | k3d 上把 overlay 的出口換成 `elasticsearch` exporter 重跑 `run-rca-e2e.sh` |
| 9.5 專屬的設定可能有漏 | 這輪只看 setup 腳本 exit 0 與幾個 settings | `component-settings.json`、ILM 的 downsample 細節沒逐項比 | 列一份 9.5 → 8.19 的設定差異表 |
| `loop_e2e` 的 `metrics` 檢查跟日期綁死 | 今天 10-30 跑，窗到 10-28 | 之後每天都會 FAIL 這一項 | 把回饋的 `@timestamp` 改成跟事故同一天，或窗用「今天」算 |
| Grafana 13 只驗 provisioning 與表單 | 這輪 | 通知真的送出（contact point 打 webhook）、恢復通知、心跳規則的評估沒在 13 上跑 | 跟 k3d 那輪一起 |
| 外掛版本 | 13 可裝 6.3.5 | `plugin-init-patch.yaml` 的 SHA-256 是 6.3.1 的 | 決定要不要換新版；換就重算 SHA-256 |

- **決定**：ES 8.19、Grafana 13 **可以用**。唯一要改程式／設定的是 Collector 出口（`_otlp` → `elasticsearch` exporter）；其餘（setup 腳本、pipeline、engine、Grafana provisioning、外掛、表單）原樣可用。
- **下一輪**：改 `telemetry/collectors/common.yaml` 的出口並在 k3d 上重跑耐久與 rca 端到端；再定 9.5 → 8.19 的設定差異表。
