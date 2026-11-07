# 原始資料來源：OTel 與 Elastic APM 怎麼對齊

> **類型：規格 Reference**　怎麼新增一種來源見 [data-sources-how-to.md](data-sources-how-to.md)；接 APM 的設定與上線前確認見 [../aiops-engine/configs/apm/README.md](../aiops-engine/configs/apm/README.md)；這個模組為什麼這樣切見 [pdca/meta/2026-11-source-adapter.md](pdca/meta/2026-11-source-adapter.md)

engine 讀進來的 raw 資料有兩種格式：**OTel**（我們過去的 Collector 寫的，Collector 已不在這個 repo）與 **Elastic APM**（公司現有的）。兩種各有一個**來源適配器**（`aiops-engine/src/data/source/`），把自己的 raw 文件翻成同一種統一的 span；合成、判斷、告警、根因都只認統一的 span。

> 範圍：**trace**（span）。metrics 與 log 的差異不在適配器裡，靠 `configs/<來源>/series.yaml` 與情境資料檔（`common/scenario/it_otel.yaml`、`it_apm.yaml`）處理。來源用 `AIOPS_SOURCE=otel`、`apm`，或兩種並存 `otel,apm`（第一個是主要的；設定見 [../aiops-engine/configs/mixed/README.md](../aiops-engine/configs/mixed/README.md)）。

## 統一的 span

適配器的 `to_span(doc)` 回這個 dict（`data/store/models.py` 的 `Trace` 與之後的合成、根因分析都用它）：

| 欄位 | 意思 |
|---|---|
| `trace_id`、`span_id`、`parent` | 這一段、它的上一層（沒有就是 `None`） |
| `service` | 哪個服務（沒有就是 `"?"`） |
| `name` | 這一段叫什麼（API 的名稱、`SELECT …`） |
| `kind` | `Server`、`Consumer`（入口）、`Client`、`Producer`（往外）、`Internal` |
| `target` | 往外呼叫的對象（`oracle`、`wip-worker:8080`），沒有是 `None` |
| `start_ms`、`dur_ms` | 開始時間與耗時，**毫秒** |
| `error` | 這一段失敗了嗎（布林） |
| `exception` | 例外種類（沒有是 `None`） |
| `links` | 指到別的 trace 的 span id（例如訊息的生產者） |
| `attrs`、`resource` | 其餘屬性與資源（兩種來源都攤成 OTel 的名字，根因分析的共同點分析才能用同一套設定） |

## 兩種來源的 trace 怎麼對

| | OTel | Elastic APM |
|---|---|---|
| raw 索引 | `traces-generic.otel-*` | `traces-apm-*` |
| 一筆是什麼 | 一個 span | transaction、span 各一筆（`processor.event` 區分） |
| 文件結構 | 扁平欄位；資源與屬性在 `resource.attributes.*`、`attributes.*` | ECS 巢狀；`transaction`、`span` 是物件 |
| trace id | `trace_id` | `trace.id` |
| span id | `span_id` | span 是 `span.id`；transaction 是 `transaction.id` |
| 上一層 | `parent_span_id` | `parent.id` |
| 服務名 | `resource.attributes.service.name` | `service.name` |
| 耗時 | `duration`，**奈秒** | `transaction.duration.us`、`span.duration.us`，**微秒** |
| 失敗 | `status.code == "Error"` | `event.outcome == "failure"` |
| 種類 | 文件自帶 `kind` | 沒有；transaction 是入口（`messaging` 類是 `Consumer`，其他 `Server`）；span 有往外對象或是 `db`／`external`／`storage`／`messaging` 類是 `Client`（送訊息是 `Producer`），其餘 `Internal` |
| 往外的對象 | `attributes.peer.service`、`db.system`、`db.system.name`、`server.address` 取第一個有的 | `span.destination.service.resource`，沒有用 `service.target.name` |
| 例外 | span 屬性 `exception.type` | **沒有**：例外是 `logs-apm.error-*` 的獨立文件，沒接回 span |
| 部門、群組 | `resource.attributes.dept`、`.group` | `labels.dept`、`labels.group`（agent 設的標籤） |
| 壞值 | 耗時不是數字或是負數：當 0，不擋整批 | 同 |

metrics、log 的對照（索引、欄位、單位 `scale`、直方圖桶、粒度）見 [../aiops-engine/configs/apm/README.md](../aiops-engine/configs/apm/README.md)「跟 OTel 版差在哪」。

## 適配器要回答的問題（`SourceAdapter`，`data/source/adapter.py`）

| 成員 | 回答什麼 |
|---|---|
| `name` | 來源的名字（`"otel"`、`"apm"`），就是 `AIOPS_SOURCE` 的值 |
| `trace_fields` | 查 raw trace 要撈哪些欄位（list，或 `{includes, excludes}`） |
| `trace_sort` | 同一個 trace 的 span 怎麼排序（分頁用，要有穩定的次要排序） |
| `trace_id_field` | raw 文件裡 trace id 的欄位名 |
| `span_id_field` | raw 文件裡 span id 的欄位名 |
| `span_ids_filter(ids)` | 用一批 span id 讀回來的 ES 條件（APM 要同時比 `span.id` 與 `transaction.id`） |
| `can_handle(doc)` | 這筆 raw 文件是不是我的：看內容有沒有**我才有的**欄位。要積極地認，不要「不是別人的就是我的」 |
| `owns_index(index)` | 這個索引名是不是我的（APM 認 `apm`；OTel 沒有可靠特徵，回 False） |
| `to_span(doc)` | 翻成統一的 span |
| `aliases` | 調整的 `match` 可以互換的欄位名（短名 ↔ 這個來源的路徑；APM 目前是空表） |

## 選哪個適配器

| 時機 | 函式 | 規則 |
|---|---|---|
| 查 raw 之前（文件還沒讀回來） | 設定明講的 `source`（`trace.yaml` 的 `raw_index` 清單每個索引帶一個），沒講就 `for_index(索引名, 宣告的來源)` | 明講的優先；沒講：有適配器認領索引名（APM 認 `traces-apm-*`）就用它；沒有就用宣告的來源（並存時是第一個）；宣告的不在登記表（工廠情境的 `"factory"`）用 OTel |
| 讀回來之後 | `to_span(doc)` | 依序問每個適配器 `can_handle`，第一個認得的翻；**都不認得丟 `UnknownSourceError`**（訊息列出文件有的欄位，不默默當成 OTel） |
| 合成後的索引（`traces-merged-*`、`lot-paths-*`） | `is_merged(index)` | 看索引名；這是 engine 自己的格式，不是來源 |

## 欄位對照的覆寫（`aiops_mapping`）

情境資料檔（`it_otel.yaml`、`it_apm.yaml`）的 `owner`、`log` 是基準；PostgreSQL 的 `aiops_mapping` 裡**核准過**的覆寫在每次讀時蓋上去（`common/scenario/mapping.py`）。

| 開放的欄位 | |
|---|---|
| `owner` | `index`、`namespace`、`service`、`dept`、`group` |
| `log` | `index`、`service`、`level`、`trace_id`、`message`（清單）、`problem_levels`（清單） |
| `trace` | `raw_index`：trace-merge 讀哪個原始索引（蓋過 `trace.yaml` 裡這個來源的；核准前要抽得到、而且是這個來源的格式） |

| 狀態 | 意思 |
|---|---|
| `proposed` | 提了，還沒生效 |
| `active` | 核准了，生效中；同一個欄位新的核准會把舊的改成 `disabled` |
| `rejected` | 沒核准 |
| `disabled` | 停用，回到 yaml 的值 |

trace 合成時的部門、群組直接用 `owner` 的 `dept`、`group` 欄位名（假設同一個來源的 metrics 與 trace 用同一個 label 路徑），所以改歸屬的部門欄位，trace 也跟著。

讀不到資料庫時照 yaml；一筆覆寫寫錯（欄位沒開放、來源沒在用）只略過那一筆。怎麼操作見 [data-sources-how-to.md](data-sources-how-to.md)。

## 目前的限制

- **並存的限制**（[../aiops-engine/configs/mixed/README.md](../aiops-engine/configs/mixed/README.md)）：同一個服務不要兩邊都送；序列的 `name` 不能重複；`interval`、`lag` 整份一個值；log 只有「這個請求的 log」與 warn／error 會查兩個來源。
- **只涵蓋 trace。** metrics、log 的差異靠設定檔。
- **自我檢查只回報、不擋**：`detector source-check`，每輪 `detect`、`trace-merge` 也自動檢查，Grafana 的「AIOps 來源對不上」規則叫人；「抽不到資料」只算提示（可能本來就沒有，例如沒開 log shipping），「抽到了但欄位或格式對不上」才算對不上。
- **APM 版只對合成資料（`tests/apm_synth.py`）驗過**，沒有對公司真實的 APM 資料跑過。
- **APM 的別名表是空的**：`match` 寫 `service=…` 時，若 APM 序列的 key 是 `service.name` 找不到對應；補在 `apm.py` 的 `aliases`。
