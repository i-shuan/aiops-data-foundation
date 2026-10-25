# Grafana：讀 aiops-alerts 發通知

AIOps 把異常合成「一件事」寫進 `aiops-alerts`（`python3 -m investigator alert`，見 [configs/rca](../rca/README.md)）；**通知交給 Grafana**：送到哪、依 dept 分送、夜間、靜音、恢復通知都在 Grafana 設（[pdca/2026-10-alert-exit.md](../../../docs/pdca/2026-10-alert-exit.md)）。

這個資料夾是 Grafana provisioning 的範例，管 Grafana 的人照著改：

| 檔案 | 內容 | 要改的 |
|---|---|---|
| `datasource.yaml` | ES data source，只讀 `aiops-alerts` | `ES_URL`、`GRAFANA_ES_API_KEY`（`telemetry/scripts/elasticsearch-setup.sh` 發的「Grafana 唯讀 key」） |
| `alert-rules.yaml` | (1) 每一件 `status: open` 的事件一則告警；labels 帶 `alert_id`、`service`、`dept`、`group`、`severity`，annotation 帶摘要與報告連結；(2) `aiops-heartbeat`：10 分鐘沒有心跳 → 「AIOps 停了」送平台 | 一般不用改 |
| `contact-points.yaml` | 各部門的通知管道（範例用 webhook） | 換成你們的 Teams、Email、LINE… |
| `policies.yaml` | 依 `dept` 送到哪個 contact point；沒有 dept（資料品質、節點）送平台 | dept 清單（跟 `telemetry/groups.yaml` 一致） |

## 怎麼運作

- `aiops-alerts` 一件一筆。`status: open` 的那幾筆，`@timestamp` 是 `investigator alert` 最後一次看到它還開著的時間（每次執行都更新）。
- 告警規則查「最近 5 分鐘 `@timestamp`、`status: open`」，依 `alert_id`（再帶 `service`、`dept`、`group`、`severity`、`summary`、`report_url`）分組：**每一件一個告警**。
- 件結束（30 分鐘沒新的異常）→ `status: resolved` → 查不到 → Grafana 發恢復通知。
- `investigator alert` 停了：所有件都查不到，Grafana 會當成全部恢復。所以它每次跑都寫一筆心跳（`_id: heartbeat`、`status: ok`），第二條規則 `aiops-heartbeat` 查 10 分鐘內有沒有心跳，沒有就送平台（`noDataState: Alerting`）。這條規則寫好了、yaml 能讀，**還沒在 Grafana 上實測**（上一輪實測只有第一條）。
- 通知裡的「回饋」連結：`aiops-alerts.feedback_url`（`rca.yaml` 的 `alerts.feedback_url`，指到 [engine/feedback_web](../../feedback_web/__init__.py)）。要帶進通知，在 annotations 再加一個 `feedback: "{{ $labels.feedback_url }}"`，bucketAggs 多一個 terms `feedback_url`（跟 `report_url` 一樣的寫法）。

## 為什麼 group_by 用 alert_id

一件事已經是 AIOps 合併過的結果（同一個服務的多筆異常）。Grafana 這邊**不要再依 dept 或 service 合併**，不然同一個 dept 的兩件不相關的事會被併成一則通知。
