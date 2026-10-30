# Trace 進 Elasticsearch：操作

> **類型：操作 How-To**　同主題：[原理](README.md)・[規格（欄位）](reference.md)・[場景（案例）](scenarios.md)

應用要做什麼、怎麼部署、怎麼驗證、遇到問題怎麼辦。

## 應用要做什麼

用 app-common chart 而且開了 Java agent（`otel.javaAgent.enabled: true`）或 .NET 自動埋點（`otel.dotnet.enabled: true`）的服務不用做任何事，chart 會設好：

| 環境變數 | 值 |
|---|---|
| `OTEL_TRACES_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `http://otel-node-collector.otel-collector.svc:4318/v1/traces` |
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio`：上游決定要記的跟著記，自己開始的請求依比例 |
| `OTEL_TRACES_SAMPLER_ARG` | `otel.traces.sampleRatio`，預設 `1.0`（全部送）。**保持 1.0**：要不要留由 trace-sampler 在請求結束後決定（尾端取樣）；在應用裡調低的話，出事的請求在一開始就被丟了 |

沒用 chart 的服務，自己設上面這幾個環境變數。不是 Java、.NET 的服務，用該語言的 OpenTelemetry SDK，送到同一個位址。

**什麼時候要自己埋點、怎麼埋（業務步驟、批號這類欄位、SECS 等 agent 不認得的元件），見 [sdk/standards/tracing.md](../../sdk/standards/tracing.md)。**

## 部署

中央 ES 和每個 cluster 的步驟跟 log、metrics 一樣（[log/how-to.md 的「部署」](../log/how-to.md#部署)）：重跑 `elasticsearch-setup.sh`，新發的 key 才有 `traces-*.otel-*` 的權限（或用 `rotate-writer-key.sh` 一個一個 group 換，不掉資料）；`kubectl apply -k telemetry/collectors` 會多一個 Service `otel-node-collector`、StatefulSet `trace-sampler`（尾端取樣）。

ES 要有內建的 `traces-otel@template`（8.19 以上；Collector 用 `elasticsearch` 寫入元件寫入，不需要 `/_otlp` 入口）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u elastic:<password> https://<es>:9200/_index_template/traces-otel@template
```

## 驗證

```bash
ES_URL=http://<ip>:9200 e2e/local-k3s/run-trace-e2e.sh
```

測試應用（[trace-app.sh](../../e2e/local-k3s/trace-app.sh)）每 5 秒印一行帶 `trace_id` 的 log、送一個同 `trace_id` 的 span。it-mes-prod 那個故意在資源欄位自稱 imc-wip-prod、dept D1110、group imc、假的 Pod UID。**23 項全部通過**：

| 類別 | 檢查 |
|---|---|
| 分流 | 三個 namespace 的 trace 各進自己的 group，沒有落錯 |
| 邊緣身份鑑別 | 自稱 IMC 的那個仍進 IT，namespace、dept、Pod UID 都是真的；IMC 裡沒有它的資料 |
| 欄位 | 服務名稱、版本、cluster、Pod 名稱；沒有夾帶 pod label |
| 串起來 | 從 trace 的 `trace_id` 找得到那行 log，反過來也找得到 |
| 去重複 | 同一個 span 送 3 次，ES 只有 1 筆（另外兩次回 409） |
| 權限 | 寫入 key 能寫所有 group 的 trace、不能讀（2026-10 起一把共用）；AIOps key 能讀全部；IT 的唯讀角色看得到 IT 的 trace |

另外手動測過（每個測試應用每 5 秒一個 span）：

| 情境 | 結果 |
|---|---|
| ES 正常關機 3 分鐘 | 停機期間三個應用各 35～36 個 span 全部補進來，沒有重複 |
| ES `kill -9` 兩次（第 2 次在補送中） | 各 20 個 span 全部補進來，沒有重複 |

## 坑

集中在 [troubleshooting.md](../troubleshooting.md#trace)。
