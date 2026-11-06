# core/repository：索引的讀寫，一個實體一個

> **類型：規格 Reference**　為什麼這樣分層見 [docs/architecture.md](../../../../../docs/architecture.md)、計畫見 [docs/pdca/meta/2026-11-target-architecture.md](../../../../../docs/pdca/meta/2026-11-target-architecture.md)

索引名與 ES 的查詢語法只出現在這一層（`tests/test_layers.py` 鎖著）。service 透過 `Repos` 拿各實體的 repository：

```python
from aiops.core.connection import ES
from aiops.core.repository import Repos

repo = Repos(ES())                       # 指令開頭；測試給假 ES（只要有 req／search／bulk_index／ensure_index），或 tests/fakes.MemoryRepos 不連 ES
repo.alerts.open()                       # ⑥ 開著的件（Alert 模型）
repo.anomalies.write(docs)               # ⑤ 寫異常：補 engine_version、dept／group
repo.series.fetch(spec, start, end, "5m")   # 原始資料 → 一條序列（keys + ts + value + n）
```

| 屬性 | 檔案 | 索引 | 給誰用 |
|---|---|---|---|
| `anomalies` | `anomaly.py` | `aiops-anomalies` | ④ 五種偵測寫、⑤⑥⑨ 讀（between、get_many、status_of、tag_alert） |
| `alerts` | `alert.py` | `aiops-alerts` | ⑥ alert 每輪（open、resolved_anomaly_ids、bulk）、⑧ 網頁（get、list、request_rca）、detect-status |
| `incidents` | `incident.py` | `aiops-incidents` | ⑥ upsert_auto（只更新 auto 的）、⑦ similar／find（相似事故）、⑨ patch／put／recent |
| `tuning` | `tuning.py` | `aiops-tuning` | 調整：all（讀不到回 []）、proposed、get／exists、put（過 Adjustment 模型）、patch、delete |
| `feedback` | `feedback.py` | `aiops-feedback` | ⑨ since、missed_since、by_alert_ids、put、bulk |
| `baseline` | `baseline.py` | `aiops-baseline` | 動態基準線 load、load_group、save（寫完刪舊的） |
| `segments` | `segment.py` | `aiops-trace-segments` | trace 每段的統計 |
| `state` | `state.py` | `aiops-state` | 排程工作的進度（trace-merge 合到哪） |
| `chronic` | `chronic.py` | `aiops-chronic` | 慢性病清單 |
| `series` | `series.py` | 設定給（metrics、log 的索引） | 原始資料 → `fetch`（序列）、`alive_times`、`points`（規則）、`rows`／`rows_where`（ingest、paths-merge）；`value` 的寫法 → ES aggregation 在這裡 |
| `traces` | `trace.py` | 設定給（raw、merged、lot-paths） | spans、spans_by_id、updated_trace_ids、write_merged、merged_docs、entry_stats、sample_trace_ids、recent／service_trace_ids、sample_entries |
| `logs` | `log.py` | 設定給 | of_traces、changes、problems、daily_counts；欄位名依場景的 log 對照 |
| `change` | `change.py` | 設定給 | 換版：version_first_seen（IT）、latest（工廠的變更紀錄） |
| `quality` | `quality.py` | check 給 | 品質檢查讀的統計：cardinality、missing、counts、count、lag |
| `housekeeping` | `housekeeping.py` | 設定給 | count／delete_by_query（prune）、put_index_template |
| （函式） | `owner.py` | 設定給 | `load_owners`、`owner_of`：異常的 dept／group（AnomalyRepository.write 用） |

- mapping 跟著實體：`core/entity/{anomaly,alert,incident,adjustment,feedback,baseline}.py`；只有 repository 自己用的索引（segments、state、chronic）mapping 放在 repository 檔裡。
- 共用的翻頁（search_after、composite）、_mget、_update、_bulk update 在 `_base.py` 的 `Repository`。
- 不連 ES 的測試：`tests/fakes.MemoryRepos`（原始資料給 DataFrame，整形在 `tests/shaping.py`，語意跟 ES 端的 aggregation 一樣，`tests/test_shape.py`、`test_values.py` 對過）。
