# common：各功能共用的底層，與 `Repos`（索引的讀寫，一個實體一個）

> **類型：規格 Reference**　為什麼這樣分層見 [docs/architecture.md](../../../docs/architecture.md)、計畫見 [docs/pdca/meta/2026-11-target-architecture.md](../../../docs/pdca/meta/2026-11-target-architecture.md)

`common/` 是各功能共用的底層，分四個資料夾，它不依賴任何功能資料夾（`tests/test_layers.py` 鎖著）：

- `clock/`：時間只在這裡解析一份。`parse.py`（`parse_duration` 30m／1h／7d→timedelta、`parse_instant` ISO→帶時區的 Timestamp、`pandas_freq`、`check_zone`）、`window.py`（`now_of`、`TimeWindow`、`show_duration`）、`stamp.py`（`stamp_of`）。字串只在邊界解析：命令列在 `interface/cli/common.py` 的 `at_opt`／`window_opt`（typer 的 parser，看不懂印用法、不是 traceback），設定檔在載入時（`Dur`、`Tz`）；往下傳的是 `pandas.Timestamp`、`Timedelta`、`TimeWindow`。`tests/test_no_time_strings.py` 鎖著。
- `infra/`：`env.py`（引擎版本、專案根目錄）、`es.py`（ES 連線、寫進 ES 的文件的基底 `Doc`、repository 基底 `Repository`）、`repos.py`（`Repos`）。
- `types/`：`incident.py`——事故與指紋的資料物件，`llm/`（過去相似）與 `incident/`（告警）都用它，放在任一邊都會循環。
- `scenario/`：`base.py`（`Scenario`、`load_scenario()`）、`*.yaml`（四份情境資料檔）。
- `config/`：只放「各功能共用」的設定：`common.py`（共用型別與 `Strict` 基底）、`loader.py`（`load_config(path, kind)` 讀檔驗證，模型名稱表 `REGISTRY` 第一次用到才載入各功能的模型，所以 common 不靜態依賴功能）、`settings.py`（環境變數，`settings()`）。**各種設定自己的模型放用它的功能資料夾**：`detect/config/`（series、rules、peer、quality、trace、chronic、prune、adjustments、calendar、paths、ingest，加 detect 內共用的 `types.py`）、`incident/config/rca.py`、`interface/config/schedule.py`。

索引名只出現在各功能資料夾的 `store/` 與 `common/`。每個功能的 repository 放在自己的 `store/`（`detect/store/`、`incident/store/`、`learn/store/`），上層透過 `Repos` 拿：

```python
from common.infra.es import ES
from common.infra.repos import Repos

repo = Repos(ES())                       # 指令開頭；測試給假 ES（只要有 req／search／bulk_index／ensure_index），或 tests/fakes.MemoryRepos 不連 ES
repo.alerts.open()                       # ⑥ 開著的件（Alert 模型）
repo.anomalies.write(docs)               # ⑤ 寫異常：補 engine_version、dept／group
repo.series.fetch(spec, start, end, "5m")   # 原始資料 → 一條序列（keys + ts + value + n）
```

| 屬性 | 檔案（相對 `src/`） | 索引 | 給誰用 |
|---|---|---|---|
| `anomalies` | `detect/store/anomaly_repo.py` | `aiops-anomalies` | ④ 五種偵測寫、⑤⑥⑨ 讀（between、get_many、status_of、tag_alert） |
| `alerts` | `incident/store/alert_repo.py` | `aiops-alerts` | ⑥ alert 每輪（open、resolved_anomaly_ids、bulk）、⑧ 網頁（get、list、request_rca）、detect-status |
| `incidents` | `incident/store/incident_repo.py` | `aiops-incidents` | ⑥ upsert_auto（只更新 auto 的）、⑦ similar／find（相似事故）、⑨ patch／put／recent |
| `tuning` | `learn/store/tuning_repo.py` | `aiops-tuning` | 調整：all（讀不到回 []）、proposed、get／exists、put（過 Adjustment 模型）、patch、delete |
| `feedback` | `learn/store/feedback_repo.py` | `aiops-feedback` | ⑨ since、missed_since、by_alert_ids、put、bulk |
| `baseline` | `detect/store/baseline_repo.py` | `aiops-baseline` | 動態基準線 load、load_group、save（寫完刪舊的） |
| `segments` | `detect/store/segment_repo.py` | `aiops-trace-segments` | trace 每段的統計 |
| `state` | `detect/store/state_repo.py` | `aiops-state` | 排程工作的進度（trace-merge 合到哪） |
| `chronic` | `detect/store/chronic_repo.py` | `aiops-chronic` | 慢性病清單 |
| `series` | `detect/store/series_repo.py` | 設定給（metrics、log 的索引） | 原始資料 → `fetch`（序列）、`alive_times`、`points`（規則）、`rows`／`rows_where`（ingest、paths-merge）；`value` 的寫法 → ES aggregation 在這裡 |
| `traces` | `detect/store/trace_repo.py` | 設定給（raw、merged、lot-paths） | spans、spans_by_id、updated_trace_ids、write_merged、merged_docs、entry_stats、sample_trace_ids、recent／service_trace_ids、sample_entries |
| `logs` | `incident/store/log_repo.py` | 設定給 | of_traces、changes、problems、daily_counts；欄位名依場景的 log 對照 |
| `change` | `incident/store/change_repo.py` | 設定給 | 換版：version_first_seen（IT）、latest（工廠的變更紀錄） |
| `quality` | `detect/store/quality_repo.py` | check 給 | 品質檢查讀的統計：cardinality、missing、counts、count、lag |
| `housekeeping` | `detect/store/housekeeping_repo.py` | 設定給 | count／delete_by_query（prune）、put_index_template |
| （函式） | `detect/store/owner_repo.py` | 設定給 | `load_owners`、`owner_of`：異常的 dept／group（AnomalyRepository.write 用） |

- mapping 跟著實體：`detect/store/{anomaly,baseline}.py`、`incident/store/models.py`、`common/types/incident.py`、`learn/store/models.py`；只有 repository 自己用的索引（segments、state、chronic）mapping 放在 repository 檔裡。
- 共用的翻頁（search_after、composite）、_mget、_update、_bulk update 在 `common/infra/es.py` 的 `Repository`。
- 不連 ES 的測試：`tests/fakes.MemoryRepos`（原始資料給 DataFrame，整形在 `tests/shaping.py`，語意跟 ES 端的 aggregation 一樣，`tests/test_shape.py`、`test_values.py` 對過）。

- `Repos` 的屬性名對 repository 的名稱表在 `common/infra/repos.py`（`REGISTRY`），第一次取用才載入，所以 `common/` 不靜態依賴任何功能；新增 repository：檔案放功能的 `store/`，在 `REGISTRY` 加一行。
