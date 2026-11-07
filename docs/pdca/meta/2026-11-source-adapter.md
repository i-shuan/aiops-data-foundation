# 收值模組化：OTel 與 APM 各一個來源模組，可選、之後可並存（2026-11）

**狀態**：進行中（階段 0 已完成：拆出 `data/` 資料夾；階段 1～3 還沒做）　　**日期**：2026-11-07　　**相關**：分支 `claude/source-adapter`（從 master `bf8b5a9` 開）
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
├─ base.py     SourceFormat 介面：一種來源要提供什麼
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

