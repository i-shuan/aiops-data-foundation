# AIOps 資料格式：aiops-* 索引的欄位

> **類型：規格 Reference**　同主題：[原理（為什麼這樣設計）](architecture.md)・[回饋與學習](feedback/README.md)・[相似事故檢索](rca/README.md)

查 AIOps（④～⑨）寫進 ES 的五個索引：每個欄位是什麼型別、誰在什麼時候寫、長怎樣。範例是 2026-10-31 在 ES 8.19.23 上跑 `loop_e2e`、`baseline_edge_e2e` 後取出的真實文件，長文字截短（標「…共 N 字」），內容沒改。log、metrics、trace 本身是 Elastic APM 的欄位，看 [aiops-engine/code/configs/apm/README.md](../aiops-engine/code/configs/apm/README.md)。

各索引存什麼、誰寫、多久一次、會不會自己消失，以及 Grafana 自己存什麼：[data-storage-reference.md](data-storage-reference.md)。

**真正的定義在程式**（改欄位時以程式為準，並同步改這份）：

| 索引 | mapping 在哪 |
|---|---|
| `aiops-anomalies` | `aiops-engine/code/core/anomaly.py` 的 `ANOMALY_MAPPING`（欄位的唯一定義是同一檔的 `Anomaly`） |
| `aiops-alerts` | `aiops-engine/code/core/alert.py` 的 `ALERT_MAPPING`（欄位的唯一定義是同一檔的 `Alert`；事件整合在手上的就是這個物件，`to_doc()` 寫、`from_doc()` 讀） |
| `aiops-incidents` | `aiops-engine/code/core/incident.py` 的 `INCIDENT_MAPPING`（欄位的唯一定義是同一檔的 `Incident`、`Fingerprint` 模型，寫入一律 `to_doc()`） |
| `aiops-feedback` | `aiops-engine/code/feedback/suggest.py` 的 `FEEDBACK_MAPPING` |
| `aiops-tuning` | `aiops-engine/code/core/tuning.py` 的 `TUNING_MAPPING`（⑨ 寫、④ 讀，所以放 `core/`）；寫進去前過同一檔的 `Adjustment`（`validated`） |

五個索引的 mapping 都是 `dynamic: false`：**沒列在 mapping 的欄位，會留在 `_source`（Grafana 面板讀得到、顯示得出來），但不能拿來篩選或聚合。**

## 資料怎麼流動

```
偵測（④）            寫 aiops-anomalies（⑤ 異常總表）  一筆異常一份文件
   ↓
事件整合（⑥ 收斂）     讀 anomalies → 同一個服務合成一件 → 寫 aiops-alerts（每 1～2 分鐘更新）
   │                  同時把 alert_id 寫回它併進來的每筆 anomalies
   │                  同時開一筆 aiops-incidents（status: auto，還沒人看）
   ↓
根因分析（⑦）        新的一件跑一次：寫進 alerts 的 findings、similar、report、llm_summary
   ↓
Grafana（⑧）         讀 aiops-alerts 發通知；儀表板讀 alerts、anomalies、feedback、tuning
   ↓
資深維運專家（老師傅）  在回饋表單送出 → 寫 aiops-feedback、更新 aiops-incidents
回饋（⑨）            feedback → 建議 → 回測 → 人核准 → 寫 aiops-tuning，偵測下一輪生效
```

| 索引 | 誰寫 | 誰讀 | `_id` |
|---|---|---|---|
| `aiops-anomalies` | `detector`（detect、rules） | 事件整合、Grafana、回饋 | 內容的 sha1（`doc_id`），同一點重跑不重複 |
| `aiops-alerts` | `investigator alert`；`detector detect` 只寫 `detect-status` 一筆 | Grafana（告警規則、儀表板）、回饋網頁 | `alert_id`；心跳固定 `heartbeat`、detect 狀態固定 `detect-status` |
| `aiops-incidents` | `investigator alert`（開）、回饋網頁（確認）、`detector incident`（人工補登） | 相似事故檢索 | 等於 `alert_id`；人工補登的是 `--id`，沒給就是（標題、開始時間）的 sha1 |
| `aiops-feedback` | 回饋網頁、`detector feedback` | `feedback suggest`、儀表板 | 內容的 sha1（一筆異常、一個人一筆） |
| `aiops-tuning` | `detector tune／adjust／feedback suggest` | `detect`、`train`、儀表板 | `--id` 指定的；沒給就是內容的 sha1（`suggest` 的是序列與對象、或服務與根因分類的 sha1） |

## aiops-anomalies：一筆異常

寫入：`detector/cmd_baseline.py`（動態基準線學習，每次 detect）、`cmd_rules.py`（靜態臨界值規則）、`cmd_peer.py`（Pod 同儕橫向比對）、`cmd_trace.py`（trace 的基準線與規則）、`cmd_quality.py`（資料品質）。事件整合另外用部分更新補上 `alert_id`。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `@timestamp` | date | 異常那一點的時間（不是偵測時間） |
| `series` | keyword | 序列名，例如 `http.latency_p95_s`；規則觸發的是 `rule.<規則名>` |
| `keys` | flattened | 這筆屬於哪個對象（服務、方法、路由…），欄位依序列而不同 |
| `value` | double | 當時的值 |
| `median`、`expected_low`、`expected_high` | double | 平常的中位數與範圍；規則觸發的沒有中位數 |
| `z` | double | 偏離程度；規則觸發的是 null，卜瓦松的序列另有 `p` |
| `level` | keyword | 基準線那一層：`dow_hour`（星期×小時）、`hour`、`all`、`group`（用同群當基準）；不是基準線算出來的：`rule`（靜態規則、trace 規則）、`peer`（Pod 同儕橫向比對）、`quality`（資料品質）；工廠版接進來的：`ml`（ML 的異常跳點）、`fdc`（FDC 鳴叫） |
| `source`、`source_id` | keyword | 只有工廠版 `detector ingest` 寫的有：來源名（`ingest.yaml` 的 input 名，ml／fdc）與來源的識別碼（run_id、alarm_id；`_id` 由它算，重讀不重複）。工廠版的 `keys` 是 `equipment`、`chamber`、`recipe`、`step`、`lot`、`parameter` |
| `direction` | keyword | `both`、`up`、`down`；規則是比較符號（`>=`） |
| `resolution`、`method` | keyword | 粒度（`30s`、`5m`、`1h`）與方法（`mad`、`poisson`） |
| `baseline_source` | keyword | `self`，或 `group:<名稱>` |
| `confidence` | keyword | 由 `level` 決定：`dow_hour` 高、`hour` 中、`all` 與 `group` 低 |
| `status` | keyword | 只有動態基準線學習的有：`anomaly`（可以單獨開一件）或 `warning`（只跳一次，不開新的，只併進同服務已經開著的件）。`rule`、`peer`、`quality` 沒有這個欄位，看 `severity`（見下） |
| `streak` | integer | 連續幾點 |
| `p` | double | 卜瓦松的機率 |
| `dept`、`group` | keyword | 寫入時由 `sources` 的 `Backend.write_anomalies` 補（歸屬查 `core/owner.py`）：依 `keys` 裡的 namespace（優先，dept 是 namespace 的 label）或服務，對回 metrics 裡的部門與 group；找不到就是 null、`unassigned` |
| `alert_id` | keyword | 併進哪一件；事件整合寫回，沒併進去的沒有 |
| `rule`、`severity`、`message` | keyword、keyword、text | 動態基準線學習的沒有，`rule`、`peer`、`quality` 的有：規則名、嚴重度、給人看的一句話。**這三個欄位不在 `ANOMALY_MAPPING`**，由 `rules`、`peer`、`trace`、`quality` 第一次跑時用 `PUT _mapping` 補進索引（`detector/common.py` 的 `RULE_FIELDS`），所以只跑過 detect 的新索引沒有它們 |
| `since` | 不索引 | 只有靜態規則有：條件開始成立的時間 |
| `engine_version` | keyword | 哪一版 engine 寫的（`core/version.py`：環境變數 `ENGINE_VERSION`，chart 填 image tag；本機是 `dev`）。五種寫入都經 `detector/owner.py`，都有 |

**哪些異常會自己開一件**（`investigator/alerts.py`）：`severity: critical` 的規則、`status: anomaly` 的基準線、資料品質會直接開；`severity: warning` 的規則、同儕橫向比對是弱的，同一個服務在 `close_after` 內有 2 筆以上才開；基準線的 `status: warning` 只併進已開著的件。
| `detected_at` | date | 偵測程式跑的時間 |

範例（動態基準線學習，小時粒度、卜瓦松）：

```json
{"@timestamp": "2026-10-26T02:00:00+00:00", "series": "edge.http.requests",
 "keys": {"service": "svc-rare", "method": "GET", "route": "/api/report"},
 "value": 21.0, "median": 6.15, "expected_low": 0.0, "expected_high": 16, "z": 5.99,
 "level": "hour", "direction": "both", "resolution": "1h", "method": "poisson",
 "baseline_source": "self", "confidence": "medium", "status": "anomaly", "streak": 1, "p": 2.1e-06,
 "dept": null, "group": "unassigned", "detected_at": "2026-10-31T06:59:44+00:00"}
```

範例（靜態臨界值規則）：

```json
{"@timestamp": "2026-10-31T06:50:00+00:00", "series": "rule.http.error_rate_floor", "rule": "http.error_rate_floor",
 "keys": {"resource.attributes.service.name": "p2-err", "attributes.http.request.method": "GET", "attributes.http.route": "/exact"},
 "value": 0.05, "median": null, "expected_low": null, "expected_high": 0.05, "z": null,
 "level": "rule", "direction": ">=", "severity": "critical",
 "message": "API 錯誤率超過 5%：p2-err、GET、/exact（5%，門檻 >= 5%）",
 "since": "2026-10-31T06:45:45+00:00", "detected_at": "2026-10-31T06:50:28+00:00", "dept": null, "group": "unassigned"}
```

## aiops-alerts：一件事故（Grafana 通知讀這個）

寫入：`investigator alert`（`aiops-engine/code/investigator/cmd_alert.py`），每 1～2 分鐘跑一次。同一個服務最近的異常合成一件；超過 `close_after`（預設 30 分鐘）沒有新異常就結束（`resolved`）。

**`@timestamp` 的意思**：開著的件每輪都改成「這一輪的時間」，結束的件是 `resolved_at`。Grafana 的告警規則只看最近 5 分鐘、`status:open`，所以事件整合停了，所有件都會查不到，要靠心跳規則（見下）。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `alert_id` | keyword | 同時是 `_id`；格式 `a-<開始日期>-<開始時分>-<服務>`（服務名的特殊字元換成 `_`） |
| `category` | keyword | 這件歸在誰身上：`service`（服務，優先）、`namespace`、`node`（異常的 `keys` 裡沒有服務時依序退到這兩個）、`other`（都沒有，名稱用序列名）、`data_quality`（資料品質，`alert_id` 結尾多 `-dq`）、`heartbeat`（心跳）、`status`（detect 狀態，見下）。**只有 `service` 會跑根因分析、寫進 `aiops-incidents`、顯示在事故清單面板** |
| `service`、`status` | keyword | `service` 欄位放的是上一列那個類別的名稱（服務名、namespace、節點名…）；`status` 是 `open`、`resolved` |
| `severity` | keyword | `critical`、`high`、`warning`：代表要不要吵人，不是偏離幾倍。critical：靜態規則 critical，或 ≥ 3 類黃金訊號（latency、errors、downstream、saturation）同時異常且不是單點；high：2 類，或 1 類但持續（≥ 10 分鐘、最後 15 分鐘窗 ≥ 80% 有異常）或好幾條都跳；warning：其餘。換版後 10 分鐘內的異常上限 warning。Grafana 只通知 critical、high（[investigator/severity.py](../aiops-engine/code/investigator/severity.py)） |
| `severity_why` | text（不索引） | 嚴重度怎麼來的一句話（幾類、跨幾分鐘、換版上限） |
| `deploy_at` | date | 這個服務最近一次換版（`service.version` 第一次出現的時間）；查不到就沒有 |
| `dept`、`group` | keyword | 通知依 `dept` 分送；沒有 dept 的送平台 |
| `started_at`、`updated_at`、`resolved_at` | date | 事故起訖 |
| `anomaly_ids`、`anomaly_count` | keyword、integer | 併進來的異常（指到 `aiops-anomalies` 的 `_id`），一件最多記 500 筆 |
| `series`、`routes` | keyword | 涉及的指標、路由（工廠版的「路由」是 recipe） |
| `lots` | keyword | 工廠版：這件牽涉哪些 lot（異常的 `keys.lot`，最多 500 筆）；前後站併件時「那件已經有這批 lot」的前站優先（[investigator/alerts.py](../aiops-engine/code/investigator/alerts.py) 的 `links`） |
| `title`、`summary` | keyword | 通知用的標題與摘要。**存成 keyword**：Grafana 告警只能把「分組欄位的值」帶成 label，內容一變就是另一則（舊的 resolved、新的 firing），所以摘要不放持續時間、筆數 |
| `report` | text（不索引） | 根因報告全文（Markdown）；有相似事故與 LLM 摘要時放在最前面 |
| `report_at` | date | 跑根因分析的時間（新開的一件跑一次；之後只有還沒有報告時才補跑） |
| `report_url`、`feedback_url` | keyword | 由 `rca.yaml` 的 `alerts.report_url`、`alerts.feedback_url` 帶入 `{alert_id}` |
| `findings` | object（不索引） | 結構化的根因分析結果，見下 |
| `similar` | object（不索引） | 相似事故，見下 |
| `suggested_category`、`category_confidence` | keyword、double | 告警分類：相似事故（只有確認過的）以分數投票的結果與信心（贏的那類分數 ÷ 全部）；沒有相似的就空。回饋表單預帶這個分類，人改掉就是 `corrected`（[case 只做分類](pdca/meta/2026-11-case-classify.md)） |
| `llm_summary`、`llm_provider` | text、keyword | LLM 摘要與誰產的：`template`（固定句型，沒設 LLM）、`anthropic`、`openai`；模型回空的或失敗時寫成 `template（<原本的 provider> 回空的）`、`template（<provider> 失敗：…）`。每一句要附依據編號，驗證沒過就沒有摘要（`llm_summary` 空） |

**`findings`**（`investigator/__main__.py` 的 `investigate_full`）：

| 子欄位 | 內容 |
|---|---|
| `service`、`route`、`start`、`end`、`ref_label` | 查哪個服務、哪段時間、跟哪段比 |
| `anomalies[]` | `id`、`series`、`value`、`median`、`z`、`status`、`severity`、`at` |
| `segments[]` | 變慢的那一段（最多 6 筆）：`segment`、`now_ms`、`ref_ms`、`delta_ms`、`now_calls`、`ref_calls` |
| `error_origins[]` | 錯誤源頭：`segment`、`now_errors`、`ref_errors` |
| `new_patterns[]` | 新出現的 log 樣式（最多 8 筆）：`service`、`pattern`、`now`、`ref`、`is_new`、`example`（一行範例） |
| `topology[]` | 呼叫關係：`caller`、`callee`、`calls`、`errors` |
| `requests` | 這段時間的請求：`requests`、`errors`、`p50_ms`、`p95_ms` |

沒有 trace 的環境（例如合成資料），`segments`、`error_origins`、`topology` 是空陣列，報告的 Trace 段會寫「這段時間沒有這個服務的 trace」。

**`similar[]`**（`investigator/similar.py` 的 `search`）：每筆是一件過去的事故，加上為什麼像：

| 子欄位 | 內容 |
|---|---|
| `incident_id`、`title`、`start`、`services` | 那一件是哪件 |
| `score`、`why[]` | 分數與比對上的特徵，例如「同服務（mes-api）」 |
| `root_cause_category`、`root_cause`、`action`、`how_detected`、`checked[]`、`by` | 資深維運專家（老師傅）當時填的 |
| `status` | `confirmed` 或 `corrected` |

範例（`findings` 與 `similar` 只留一兩筆）：

```json
{"alert_id": "a-20261006-0705-mes-api", "category": "service", "service": "mes-api", "status": "resolved",
 "started_at": "2026-10-06T07:00:00+00:00", "updated_at": "2026-10-06T07:15:00+00:00", "resolved_at": "2026-10-06T07:45:00+00:00",
 "anomaly_ids": ["ab5c0017…", "d2345c33…", "…共 4 項"], "anomaly_count": 4,
 "series": ["synth.latency"], "routes": [], "severity": "warning", "dept": null, "group": "unassigned",
 "report_at": "2026-10-06T07:20:00+00:00",
 "findings": {"service": "mes-api", "route": null,
   "anomalies": [{"id": "ab5c0017…", "series": "synth.latency", "value": 516.9, "median": 119.7, "z": 56.4, "status": "anomaly", "at": "2026-10-06T07:05:00+00:00"}, "…共 4 項"],
   "segments": [], "error_origins": [], "new_patterns": [], "topology": [],
   "requests": {"requests": 0, "errors": 0, "p50_ms": 0.0, "p95_ms": 0.0},
   "start": "2026-10-06T07:05:00+00:00", "end": "2026-10-06T07:20:00+00:00"},
 "similar": [{"incident_id": "a-20261005-0205-mes-api", "score": 4.0, "why": ["同服務（mes-api）", "同樣的指標異常（synth.latency）"],
   "title": "[warning] mes-api：synth.latency 526（平常 120.6） 等 3 項異常", "start": "2026-10-05T02:05:00+00:00",
   "root_cause_category": "db", "root_cause": "Oracle 統計資訊過期，SELECT lots 走錯執行計畫", "action": "重跑 analyze，10 分鐘後恢復",
   "how_detected": "只有 lots 那張表的查詢慢、其他 API 正常；AWR 看到執行計畫變了", "checked": ["Oracle AWR / 慢查詢", "trace 瀑布圖"], "by": "老王", "status": "confirmed"}, "…共 2 項"],
 "title": "[warning] mes-api：synth.latency 449.1（平常 119.7） 等 4 項異常",
 "summary": "[warning] mes-api：synth.latency 449.1（平常 119.7）\n從 10-06 15:00 開始；異常：synth.latency\n過去相似：2026-10-05 [db] Oracle …（共 195 字）",
 "report": "> mes-api 的 synth.latency 異常：現在 516.9，平常 119.7 [A1]。\n> 跟 2026-10-05T02:05 的事故相似 …（共 1208 字）",
 "report_url": "http://localhost:18080/report?alert=a-20261006-0705-mes-api",
 "feedback_url": "http://localhost:18080/feedback?alert=a-20261006-0705-mes-api",
 "llm_summary": "mes-api 的 synth.latency 異常：現在 516.9，平常 119.7 [A1]。\n跟 2026-10-05T02:05 的事故相似 …（共 246 字）",
 "llm_provider": "template", "@timestamp": "2026-10-06T07:45:00+00:00"}
```

**心跳**：同一個索引裡 `_id: heartbeat` 的一筆，每輪 `investigator alert` 都更新。Grafana 的 `aiops-heartbeat` 規則看 10 分鐘內有沒有它，沒有就送平台。

```json
{"@timestamp": "2026-10-06T07:44:00+00:00", "alert_id": "heartbeat", "category": "heartbeat", "status": "ok",
 "service": "aiops", "title": "investigator alert 心跳", "group": "unassigned",
 "started_at": "2026-10-06T07:45:00+00:00", "updated_at": "2026-10-06T07:45:00+00:00",
 "anomaly_ids": [], "series": [], "routes": [], "anomaly_count": 0}
```

**detect 狀態**：`_id: detect-status` 的一筆，每輪 `detector detect` 都更新，形狀跟心跳一樣（`status: ok`，不是一件）。`missing_baseline` 是這輪沒有 baseline 的序列數、`series` 是那些序列名。Grafana 的 `aiops-baseline-missing` 規則查 `missing_baseline > 0` 持續 5 分鐘就送平台（`aiops-baseline` 掉了 detect 只會跳過那些序列，不然沒人知道）。

```json
{"@timestamp": "2026-11-01T02:04:00+00:00", "alert_id": "detect-status", "category": "status", "status": "ok",
 "service": "aiops", "title": "detector detect 狀態", "group": "unassigned",
 "started_at": "2026-11-01T02:05:00+00:00", "updated_at": "2026-11-01T02:05:00+00:00",
 "anomaly_ids": [], "series": ["http.latency_p95_s", "http.error_rate"], "routes": [], "anomaly_count": 0, "missing_baseline": 2}
```

## aiops-incidents：事故庫（相似事故檢索的資料池）

欄位定義在 `aiops-engine/code/core/incident.py` 的 `Incident`（指紋是 `Fingerprint`）；四條寫入路徑都建模型再 `to_doc()`，下表是它的欄位說明。

三種寫入：事件整合每一輪只要這件有新異常併進來、新開或結束，就寫一次（`status: auto`；用 scripted upsert，**只更新還是 `auto` 的，人處理過的一個欄位都不動**）；資深維運專家在回饋表單送出後，回饋網頁（`aiops-engine/code/api/app.py`）更新同一筆；也可以人用 `python3 -m detector incident add／resolve` 補登一件（`status: open`，結束時 `resolved`）。

| 欄位 | 型別 | 誰寫 | 說明 |
|---|---|---|---|
| `alert_id` | keyword | 事件整合 | 同時是 `_id` |
| `@timestamp` | date | 事件整合 | 這件事故的時間（結束的件是 `resolved_at`，開著的是這一輪的時間） |
| `title`（text）、`start`、`end`（date）、`services`、`severity`（keyword） | | 事件整合 | 從 `aiops-alerts` 複製 |
| `dept`、`group`、`detected_at`、`anomaly_ids` | | 事件整合 | `anomaly_ids` 最多 200 個 |
| `fingerprint` | object | 事件整合 | 指紋，六組陣列，見下 |
| `status` | keyword | 兩者 | `auto`（還沒人看）、`confirmed`（標真的）、`corrected`（真的但根因報錯了）、`false_positive`（誤報） |
| `root_cause_category`、`checked`（keyword）、`root_cause`、`action`、`how_detected`、`improve`（text） | | 回饋網頁 | 老師傅填的；誤報不填 |
| `by`、`confirmed_by`、`confirmed_at` | keyword、date | 回饋網頁 | 誰、何時 |
| `resolved_at` | date | `incident resolve` | 只有人工補登的事故會寫；事件整合開的不寫（結束時間看 `end`） |

`root_cause_category` 只有 `db`、`network`、`config_change`、`deploy`、`external`、`resource`、`bug`、`data`、`other`。

**相似事故檢索只用 `confirmed`、`corrected`**：沒人確認過的、被標誤報的，都不會被當成「過去相似」，也不會被 LLM 引用。

**`fingerprint`** 六組陣列，比對時每組加權（`investigator/similar.py` 的 `WEIGHTS`；低於 4 分不算像，最多取前 3 件；檢索可以排除指定的一件，但事件整合沒有傳：新的一件剛開時它在事故庫裡還是 `auto`，本來就查不到自己）：

| 欄位 | 內容 | 權重 |
|---|---|---|
| `slow_segments` | 變慢的那幾段（段名的數字換成 `#`，SQL 裡的 id、批號才對得起來） | 4 |
| `error_origins` | 錯誤源頭的段名 | 4 |
| `services` | 這件的服務，加慢的那幾段所屬服務 | 3 |
| `routes` | 路由 | 2 |
| `patterns` | 新出現的 log 樣式（text，用 match 比對） | 2 |
| `series` | 異常的指標 | 1（最多算 3 個） |

範例（確認過的）：

```json
{"@timestamp": "2026-10-06T07:19:00+00:00", "alert_id": "a-20261006-0705-mes-api",
 "title": "[warning] mes-api：synth.latency 516.9（平常 119.7） 等 3 項異常",
 "start": "2026-10-06T07:05:00+00:00", "end": "2026-10-06T07:15:00+00:00", "services": ["mes-api"],
 "severity": "warning", "status": "confirmed", "dept": null, "group": "unassigned",
 "anomaly_ids": ["ab5c0017…", "d2345c33…", "…共 3 項"], "detected_at": "2026-10-06T07:05:00+00:00",
 "fingerprint": {"services": ["mes-api"], "routes": [], "series": ["synth.latency"], "slow_segments": [], "error_origins": [], "patterns": []},
 "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze",
 "by": "小陳", "confirmed_by": "小陳", "confirmed_at": "2026-10-31T06:49:11+00:00"}
```

## aiops-feedback：回饋

寫入：回饋網頁（`api/app.py`）；命令列 `python3 -m detector feedback add` 也能寫（四種判定值都可以，漏報只能用命令列）。**一件事故的每筆異常各寫一筆**（現有的 `feedback summary`、`suggest` 是依「序列＋對象」算的，才對得起來）。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `verdict` | keyword | 表單送出的三種：`true_positive`（真的）、`false_positive`（誤報）、`wrong_root_cause`（真的，但根因報錯了）；命令列補登的另有 `missed`（漏報） |
| `@timestamp` | date | 送出的時間 |
| `by` | keyword | 誰；沒填是 `anonymous` |
| `alert_id`、`incident_id`、`alert_started_at` | keyword、date | 哪一件；漏報沒有 |
| `anomaly_id`、`anomaly_at`、`series`、`keys` | | 指到哪一筆異常；漏報指的是「沒被偵測到的那條序列與對象」 |
| `fp_reason` | keyword | 誤報的原因：`batch`、`loadtest`、`known_slow`、`low_volume`、`deploy`、`other` |
| `correct_root_cause` | text | `wrong_root_cause` 時填的正確根因 |
| `note` | text | 備註 |

範例（誤報）：

```json
{"@timestamp": "2026-10-31T06:48:59+00:00", "verdict": "false_positive", "by": "amy",
 "alert_id": "a-20261006-0105-mes-report", "incident_id": "a-20261006-0105-mes-report", "alert_started_at": "2026-10-06T01:05:00+00:00",
 "note": "月底結帳批次", "fp_reason": "batch",
 "anomaly_id": "0d889019…", "anomaly_at": "2026-10-06T01:05:00+00:00", "series": "synth.requests", "keys": {"service": "mes-report"}}
```

範例（漏報，沒有 `alert_id`）：

```json
{"@timestamp": "2026-10-31T06:49:25+00:00", "verdict": "missed", "by": "amy", "note": "批次之外真的掉量沒報",
 "series": "synth.requests", "keys": {"service": "mes-report"}, "anomaly_at": "2026-10-06T08:00:00+00:00"}
```

## aiops-tuning：偵測的調整

寫入：`detector tune`、`detector adjust`、`detector feedback suggest`（來源 `source: feedback`）。**`status: proposed` 不生效，人用 `adjust approve` 改成 `active` 才生效**；`detect`、`train` 下一輪讀它。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `kind` | keyword | `override`（改某序列、某些對象的 z、粒度、最小絕對量）、`exclude_window`（排除時段不學）、`reset_point`（重置點）、`recurring_override`（週期性放寬）、`rule_suggestion`（回饋建議新增的靜態規則） |
| `series`、`match` | keyword、flattened | 套用到哪條序列、哪些對象 |
| `z`、`min_abs`、`resolution`、`confirm_points`、`instant_z` | | `override` 的參數 |
| `start`、`end`、`reset_at`、`warmup`、`apply_to`、`schedule`、`effect` | | 各 kind 用到的時間與效果 |
| `status`、`source`、`expires_at` | keyword、date | 狀態、誰寫的（`feedback`、人）、到期時間 |
| `reason`、`by`、`at`、`approved_by`、`approved_at` | | 理由與核准紀錄 |
| `backtest` | object（不索引） | `backtest` 命令寫的回測結果 |
| `rule_draft` | object（不索引） | `rule_suggestion` 的規則草稿 |

範例（生效中的規則建議）：

```json
{"kind": "rule_suggestion", "status": "active", "source": "feedback", "at": "2026-10-31T06:50:42+00:00",
 "rule_draft": {"name": "p2.tuned_lag", "title": "（測試）p2 group 落後超過 5,000", "severity": "warning", "type": "value",
   "index": "metrics-*.otel-*", "field": "metrics.kafka_consumergroup_lag_sum", "op": ">=", "threshold": 5000,
   "by": ["attributes.consumergroup", "attributes.topic"], "filter": [{"term": {"attributes.consumergroup": "p2-lag-9999"}}]}}
```

## Grafana 面板讀什麼

| 面板（儀表板） | 讀 | 條件 | 顯示的欄位 |
|---|---|---|---|
| 事故清單（`aiops-rca`） | `aiops-alerts` | `category:service`，50 筆 | `started_at`、`status`、`severity`、`service`、`dept`、`anomaly_count`、`title` |
| LLM 摘要 | `aiops-alerts` | `alert_id:"<選的那件>"`，1 筆 | `llm_provider`、`llm_summary` |
| 過去相似事故與通知摘要 | `aiops-alerts` | 同上 | `summary`（裡面已經寫好「過去相似」） |
| 根因報告全文 | `aiops-alerts` | 同上 | `report` |
| 這件事故的異常 | `aiops-anomalies` | `alert_id:"<選的那件>"`，200 筆 | `@timestamp`、`status`、`severity`、`series`、`value`、`median`、`expected_high`、`z`、`message` 等 |
| mes-report 月底批次：目前被判為異常的筆數（`aiops-learning`） | `aiops-anomalies` | `keys.service:mes-report AND series:synth.requests AND status:anomaly` | 筆數（依 `@timestamp` 分時段） |
| 老師傅回饋筆數 | `aiops-feedback` | 全部 | 筆數 |
| 老師傅回饋（表） | `aiops-feedback` | 全部 | `@timestamp`、`by`、`verdict`、`series`、`alert_id`、`note` |
| 已生效的調整（核准後） | `aiops-tuning` | `status:active` | 筆數（依 `at` 分時段） |
| 調整建議（表） | `aiops-tuning` | 全部 | `at`、`kind`、`status`、`series`、`service`、`z`、`reason`、`backtest.*`、`approved_by` |
| 告警規則 `aiops-incident` | `aiops-alerts` | `status:open`，最近 5 分鐘，依 `alert_id` 分組 | 把 `service`、`dept`、`group`、`severity`、`summary`、`report_url` 帶成 label |
| 告警規則 `aiops-heartbeat` | `aiops-alerts` | `alert_id:heartbeat`，最近 10 分鐘 | 數筆數，少於 1 就告警 |
| 告警規則 `aiops-baseline-missing` | `aiops-alerts` | `alert_id:detect-status AND missing_baseline:>0`，最近 10 分鐘 | 數筆數，有就告警（持續 5 分鐘才送；查不到不算） |

相似事故的資料池是 `aiops-incidents`，但面板不直接讀它：相似事故在事件整合寫入前就檢索好、寫進 `aiops-alerts` 的 `similar` 與 `summary`。
