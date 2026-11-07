# 收值模組化：OTel 與 APM 各一個來源模組，可選、之後可並存（2026-11）

**狀態**：進行中（階段 0、1 已完成；階段 2、3 還沒做）　　**日期**：2026-11-07　　**相關**：分支 `claude/source-adapter`（從 master `bf8b5a9` 開）
**模組**：①②（讀進來、清資料）、④⑦（判斷、根因分析讀 trace 與 log 的那一側）

## Plan：這一次要達到什麼

- **需求**：engine 現在靠環境變數 `AIOPS_SOURCE=otel|apm` 在**整個程序**選一種來源，來源相關的程式散在 7 個檔案（見下表），沒有一個地方說得出「一種來源要提供什麼」。要接第三種格式、或讓 OTel 與 APM 並存，都不知道從哪裡下手；宣告錯了（環境變數設 `otel`、資料其實是 APM）不會報錯，只是查不到資料。
- **目的**：①「一種來源」是一個模組，介面寫明它要提供什麼；OTel、APM 各一個實作，後面的判斷、告警、根因不認得來源。②行為不變（現有測試、e2e、合成 APM 資料的結果跟現在一樣）。③宣告錯了要在啟動時就報出來。④做完後，來源並存只剩「依索引選模組」這一步（第二階段）。
- **範圍**：做**讀的那一側**（「收值」＝engine 把公司 ES 的 metrics、trace、log 讀進來並轉成自己的格式）。不做：寫入端、採集端（Collector 已刪）、判斷邏輯、門檻校正、Pod 資源等 APM 沒有的能力（補不回來，見 `configs/apm/README.md`）。

## 現況盤點：來源相關的程式在哪

`grep` 認得 `apm`／`otel`／`resource.attributes`／`schema_of` 的檔案與次數（`aiops-engine/src/`）：

| 檔案 | 次數 | 在做什麼 | 這次怎麼處理 |
|---|---|---|---|
| `detect/store/models.py` | 25 | `schema_of`（用索引名猜格式）、`_SPAN_FIELDS`／`_APM_FIELDS`（要撈哪些欄位）、`trace_id_field`、`to_span`／`_apm_span`／`_apm_kind`（raw 文件 → 統一的 span）、`NS_KEYS`／`NODE_KEYS` | **搬進來源模組**：這是現在「事實上的轉接器」，只是跟資料物件混在同一個檔 |
| `detect/store/trace_repo.py` | 12 | 查 raw trace 時依 `schema_of` 選欄位、選 trace id 欄位 | 改成問來源模組，不自己判斷 |
| `common/scenario/base.py` + `it_otel.yaml`／`it_apm.yaml` | 5 | `source`、`owner`、`log` 的欄位對照；`AIOPS_SOURCE` 選哪一份 | 欄位對照留在 yaml（資料），選擇改由來源模組宣告 |
| `detect/series_ops/manual_adjust.py` | 6 | 調整的 `match` 用的欄位別名（`service` 對 `resource.attributes.service.name`／`entry.service`） | 別名表改由來源模組提供 |
| `incident/store/log_repo.py`、`detect/store/owner_repo.py`、`detect/store/series_repo.py` | 3／2／3 | 讀 log、歸屬、序列時的欄位 | 欄位來自 scenario，確認只剩註解層級的提及 |
| `detect/trace/span_merge.py`、`it_deployment.py`、`learn/*` | 各 1～2 | 多半是註解或字串 | 逐個確認，大部分不用動 |

**metrics 那一側沒有程式差異**：兩種來源只差 `configs/baseline/series.yaml` 與 `configs/apm/series.yaml` 的索引、欄位、`scale`，已經是純資料。

## 設計

新資料夾 `data/source/`（階段 0 拆出來的 `data/`，一開始叫 `clean/`、使用者要求改名 `data/`：「讀進來並清成統一格式」是 ② 清資料的事；`common/` 不能依賴功能）：

```
data/source/
├─ adapter.py  SourceAdapter 介面：一種來源要回答什麼（原計畫叫 base.py／SourceFormat）
├─ otel.py     OTel（Collector 寫的 k8s-*、metrics-*.otel-*、traces-generic.otel-*）
├─ apm.py      Elastic APM（metrics-apm.*、traces-apm-*、logs-apm.*）
└─ __init__.py 登記表 REGISTRY：名字 → 模組；format_of(來源名) 取出
```

**介面（草案，動手前要對一次實際呼叫端再定）**：

| 提供什麼 | 現在在哪 | 說明 |
|---|---|---|
| `name` | 環境變數字串 | `"otel"`／`"apm"` |
| `trace_fields` | `_SPAN_FIELDS`／`_APM_FIELDS` | 查 raw trace 要撈哪些欄位 |
| `trace_id_field` | `trace_id_field()` | `trace_id` 或 `trace.id` |
| `to_span(doc)` | `to_span`／`_apm_span` | raw 文件 → 統一的 span（現有的 dict 格式，不改） |
| `owns(index)` | `schema_of` 的 `"apm" in index` | 這個索引是不是我的格式（取代索引名猜測） |
| `match_aliases` | `manual_adjust` 的別名表 | 調整 `match` 的欄位別名 |
| `probe(es)` | （沒有） | **新增**：抓一筆各索引的文件，檢查預期欄位在不在；不符就回錯誤訊息 |

不變的：統一的 span 格式、`traces-merged-*`、`aiops-anomalies`，所以 `incident/`、`learn/`、`llm/`、Grafana 都不動。

## Do（分階段，每階段測試要綠才進下一階段）

0. **階段 0：先把 ② 清資料從 `detect/` 拆成同層的 `data/`（使用者要求，已完成，細節見下節「階段 0」）。**
1. **階段 1：抽出來源模組，行為不變。** 建 `data/source/`，把 `models.py`、`trace_repo.py`、`manual_adjust.py` 的來源相關程式搬進去，原位置改成呼叫。`AIOPS_SOURCE` 仍是整個程序選一種。
2. **階段 2：啟動時自我檢查。** `probe` 接到 `detect-status`：宣告 `apm` 但資料沒有 `processor.event`，就在狀態文件標失敗、印一行，Grafana 現有的「基準線不見」類規則看得到。
3. **階段 3（要先問使用者，見下）：依索引選模組，並存。** `schema_of` 的「索引名猜格式」換成各來源模組的 `owns(index)`；序列、`raw_index` 可以混寫兩種來源。

## Check（預定）

| 為了確認什麼 | 怎麼測 |
|---|---|
| 搬動前後行為不變 | 搬之前先對 `tests/apm_synth.py` 合成資料與現有 OTel 測試資料跑一次 `trace-merge`、`rules`、`investigator alert`，存下輸出（異常、`traces-merged-*` 文件、告警摘要）；每階段後同一份資料再跑、逐筆比對 |
| 兩種來源的 `to_span` 對同一個請求出一樣的統一 span | 用 `apm_synth` 與 OTel 版造同一條呼叫鏈（mes-api → wip-worker → Oracle），斷言兩邊轉出的 `service`、`name`、`kind`、`dur_ms`、`parent` 一致 |
| 宣告錯了會報 | 單元測試：給 APM 形狀的文件但宣告 `otel`，`probe` 回錯誤；`detect-status` 標失敗 |
| 層次沒被破壞 | `tests/test_layers.py`（`data/source/` 不 import `incident`、`learn`、`llm`、`interface`） |
| 沒有第二份來源判斷 | 加 AST 測試：`apm`／`otel` 字串判斷只允許出現在 `data/source/` 與 `common/scenario/` 的 yaml |
| 真實跑起來沒壞 | 現有 e2e（`adaptive`、`synth`、`chain`、`factory`、`fine`、`peer_edge`）加 Grafana 實測（上一輪的流程） |

## 風險與要問使用者的事

1. **要不要做階段 3（並存）？** 「來源跟著索引走」會碰到：同一個服務兩邊都有資料時，`service.name` 對不對得上、併件會不會變兩件（目前沒有對照規則）；兩邊粒度不同（30 秒對 1 分鐘）時借組基準線會不會混。如果只是「現在 OTel、之後換 APM」，做到階段 2 就夠。
2. **`probe` 要不要擋住啟動？** 建議只標失敗不擋（讓 engine 照跑，但 Grafana 會叫），因為公司 ES 暫時連不上時不該讓 pod crash-loop；你若要擋也可以。
3. **公司的 APM 資料是不是真的長我們假設的樣子**仍沒驗過；`probe` 做完後，第一次在公司 ES 上跑的輸出就是這個驗證。
4. **OTel 還要不要留？** 根 `README.md`「還沒定案」第 5 點。這次不刪，做完模組化後要刪只是刪 `otel.py` 與對應設定。

## Act

- 動手前先回答上面第 1、2 點。
- 這份計畫的行數與檔案清單是 `grep` 數出來的，**沒有逐個檔案讀完**；階段 1 開始時要先逐處確認，才知道哪些「一次」其實是註解、哪些是真的邏輯。

## 階段 0：拆出 `data/`（② 清資料），做完了

- **為什麼**：`detect/` 同時有「把 raw 資料清成統一格式」和「判斷異常」兩件事；來源模組（OTel／APM）要放前者，放在 `detect/` 裡等於讓判斷層擁有來源的細節。拆成同層、更下面的 `data/`，依賴方向變成 `interface → learn → incident → llm → detect → data → common`。
- **Do**：`git mv` ——
  - `detect/trace/{span_merge,path_builder,path_service}.py` → `data/trace/`
  - `detect/store/{trace_repo,series_repo,owner_repo}.py` → `data/store/`
  - `detect/config/paths.py` → `data/config/`
  - `detect/store/models.py` 從 `# ── trace ──` 那一行切開：後半（`Trace`、`to_span`／`_apm_span`、`schema_of`、`group_traces`、`topology`…，25 個名字）→ `data/store/models.py`；前半（`Anomaly`、baseline 的 mapping、`entity_of`）留在 `detect/store/models.py`。兩段原本互相不用，所以是乾淨的切。
  - `NS_KEYS`、`NODE_KEYS` 定義搬到 `data/store/owner_repo.py`，`detect/store/models.py` 的 `entity_of` 從那裡 import（下層被上層用，方向對）。
  - `data/trace/span_merge.py` 原本吃整個 `TraceConfig`（在 detect 的設定），只用到 `raw_index` 與 `merge.settle_seconds`；改成直接收這兩個值（`merge(repo, raw_index, …)`、`run(repo, raw_index, settle_seconds, …)`），呼叫端 `detect/trace/latency_stats.py` 傳進去。這樣 `data/` 不依賴 `detect/`。
  - 20 個檔案的 import 用腳本依「名字屬於哪一半」重寫；`common/infra/repos.py` 與 `common/config/loader.py` 的名稱表（`"module:Class"`）改到新路徑；`tests/test_layers.py` 的層次加 `clean`。
  - 文件：`CLAUDE.md`、根 `README.md`、`aiops-engine/README.md`（結構樹）、`docs/architecture.md`、`common/README.md` 等 6 份的路徑與結構描述。
- **Check**：①`tests/test_layers.py`（掃 src 全部 import）過：`data/` 沒有 import `detect`、`incident`、`learn`、`llm`、`interface`。②全測試（含 helm）823 個過。③e2e：`adaptive`、`synth`、`chain`、`factory`、`fine`、`peer_edge` 過；`trace-merge` 對重造的 APM 合成資料實跑（合了 360 個 trace）。④`tools/check-docs.py` 五項過（路徑檢查抓到 9 處舊路徑，全改了）。
- **Act**：①`peer_edge_e2e` 第一次跑失敗是 ES 重啟後 time-series data stream 的可寫時間範圍過期（`document timestamp is outside of ranges of currently writable indices`），不是這次改動；刪掉 `metrics-*.otel-*` 的 data stream 後重跑通過。②（已處理）`cmd_trace_merge` 原本在 `detect/trace/latency_stats.py`（順手呼叫合成）。使用者要求搬走：它收的是 `TraceConfig`（detect 的設定），搬進 `data/` 會讓 `data/` 依賴 `detect/`，所以拆成兩半——合成的邏輯（`run`、進度狀態）本來就在 `data/trace/span_merge.py`；指令的接線 `cmd_trace_merge(trace_config, args)` 搬到 `interface/cli/detect.py`，只把 `raw_index`、`settle_seconds` 傳進去。跟 `paths-merge` 同一個做法（接線在 `interface/cli/fab.py`，邏輯在 `data/trace/path_service.py`）。`latency_stats.py` 現在只剩 trace 的統計與偵測。③`quality_check`、`fab_ingest`、`reference_data` 留在 `detect/`：它們產生 `Anomaly` 或是判斷用的參考資料，搬下去會讓 `data/` 反過來依賴 `detect/`。④`detect/store/` 的 `models.py` 現在只剩異常與基準線，檔頭說明已更新。

## 階段 1：抽出來源模組，行為不變，做完了

- **Do**：新 `data/source/`：`base.py`（`SourceFormat`：`trace_fields`、`trace_sort`、`trace_id_field`、`span_id_field`、`aliases`、`span_ids_filter`、`is_doc`、`owns_index`、`to_span`；加 `duration_ms`、`start_ms` 兩個共用的轉換）、`otel.py`（`OtelFormat`）、`apm.py`（`ApmFormat`，含 `_apm_kind`、`_apm_span`）、`__init__.py`（登記表 `FORMATS`、`format_of`、`for_index`、`to_span`、`ALIASES`）。搬走的程式：`data/store/models.py` 的 `_SPAN_FIELDS`、`_APM_FIELDS`、`to_span`／`_apm_span`／`_apm_kind`／`is_apm_doc`、`_duration_ms`／`_start_ms`；`schema_of`、`trace_id_field` 留在 models 但改成委派給 `for_index`（`is_merged` 看索引名，不是來源）；`trace_repo.py` 的三處「依 schema 選欄位／排序／查詢」改問 `fmt = for_index(index, 宣告的來源)`（APM 查 span id 要同時比 `span.id` 與 `transaction.id`，這個差異變成 `span_ids_filter`）；`manual_adjust.py` 的 `ALIASES` 表改由 `data.source.ALIASES`（各來源的聯集）提供。
- **Check（為了確認行為沒變）**：①**搬動前後逐筆比對**：用 `tests/apm_synth.py` 造 300 個請求（事故與正常各半）、共 3600 筆 APM 文件（nested 與 dotted 兩種寫法），OTel 也造 1200 筆（含壞耗時值 `"bad"`、link、`db.system.name`），分別用 master（`bf8b5a9`，搬動前）與這個分支的 `to_span` 轉，輸出 JSON `cmp` 完全相同。②全測試（含 helm）829 個過。③e2e：`adaptive`、`synth`、`chain`、`factory`（15/15、`metrics_only` 13/13）、`fine`、`peer_edge` 過；`loop_e2e` 只有 master 上就有的 2 個舊斷言失敗。④對 APM 合成資料跑 `investigator investigate`，報告仍找到 `SELECT LOT_HISTORY → oracle` 那一段。⑤`tests/test_source.py`：登記表與選法、「同一條呼叫鏈在兩種格式轉出同一種統一 span」（用 `apm_synth` 的 APM 文件，另造對應的 OTel 文件）、AST：`"apm"`／`"otel"` 字串與 `_apm_span` 等名字只允許在 `data/source/`、`common/config/settings.py`、`common/scenario/base.py`。
- **Check 抓到的一個錯**：第一版 `for_index` 對不在登記表的宣告來源丟例外，但工廠情境的 `source` 是 `"factory"`（MES 履歷，沒有 raw trace）。單元測試全過，`factory_e2e` 掉到 11/15——報告段落吞了例外。改成「沒有來源認領、宣告的來源也不在登記表就用預設（OTel）」，跟抽出來之前「非 apm 都當 otel」一樣，並加兩條單元測試（`for_index("lot-paths-…", "factory")`、`schema_of` 對四種索引的答案）。
- **跟計畫不同的地方**：①計畫寫 `probe(es)`（啟動時自我檢查）是階段 2，沒做。②計畫寫的「`match_aliases`」落成 `aliases` 欄位；APM 目前是空表（行為不變），所以用 `match` 寫 `service=…` 時，APM 序列的 key 若是 `service.name` 仍找不到別名——這是**原本就有**的缺口，不是這次造成，要補只要加在 `apm.py`。③模組放在 `data/source/`，不是計畫原本寫的 `detect/source/`（階段 0 之後改名）。
- **Act**：①階段 2（`probe`）、階段 3（依索引並存）還沒做，等你回答計畫裡的兩個問題。②`incident/store/log_repo.py`、`detect/store/owner_repo`（已搬 `data/store/`）仍靠 `Scenario` 的 yaml（`it_otel.yaml`／`it_apm.yaml`）給欄位對照；沒有搬進 `SourceFormat`，因為那是「每個情境一份資料」，不是程式；若要並存，這兩塊要改成「依索引選對照」。③這次只有 trace 側有程式差異可抽；metrics 側本來就只靠 `series.yaml` 的 `index`／`by`／`scale`。

## 階段 1 之後的修正（使用者審稿的意見）

- **命名**：`base.py` → `adapter.py`，`SourceFormat` → `SourceAdapter`，`OtelFormat`／`ApmFormat` → `OtelAdapter`／`ApmAdapter`，`FORMATS`／`format_of` → `ADAPTERS`／`adapter_of`，`is_doc` → `can_handle`。理由：原本的名字只說了實作手段（基底類別），沒說出它在業務上是什麼；這是 Adapter 模式，名字要讓人一眼看出。
- **不再默默退回 OTel**：`to_span` 原本是「不是 APM 就當 OTel」。改成問每個適配器 `can_handle`（只在確定是自己的格式時才認），都不認得就丟 `UnknownSourceError`，訊息列出文件有的欄位。OTel 的 `can_handle` 從 `"trace_id" in doc or "resource" in doc` 收緊成 `"trace_id" in doc and "span_id" in doc`（`to_span` 本來就直接取這兩個）。**這是行為變動，但只影響「本來就會壞」的文件**：不認得的文件原本會在 OTel 的 `to_span` 丟 `KeyError: 'trace_id'`，看不出是格式問題；現在丟看得懂的錯。沒有放寬任何本來能處理的文件。
- **文件**：新增 [data-sources-reference.md](../data-sources-reference.md)（規格：統一的 span、OTel 與 APM 逐項對照、適配器要回答什麼、怎麼選、限制）與 [data-sources-how-to.md](../data-sources-how-to.md)（操作：新增一種來源的步驟與常見的坑）。審稿意見建議合成一份；照專案的 Diátaxis 規則（一份文件只做一種事）拆成規格與操作兩份。
- **Check**：①搬動前後 `to_span` 逐筆比對重跑（APM 3600、OTel 1200 筆）仍完全相同。②全測試 816 個過；新增兩條：不認得的文件丟 `UnknownSourceError` 並列出欄位、每個適配器只認領自己的樣本。③e2e `synth`、`chain`、`factory`（15/15）、`adaptive` 過。④`check-docs` 過。

