# trace 的資料放回 ES（2026-11）

**狀態**：進行中　　**日期**：2026-11-07　　**相關**：分支 `claude/trace-to-es`
**模組**：② 清資料、③ 存、④ 判斷

## Plan：這一次要達到什麼

- **需求**：trace 只有可觀測性（OTel／APM）情境會用到，工廠、只有指標的情境根本沒有 trace；但 trace 每段統計（`aiops_trace_segments`，中型規模每天約 72 萬筆）與 `trace-merge` 的進度（`aiops_state` 的一筆）現在放在公司的 PostgreSQL，讓不用 trace 的部署也背這張大表。
- **目的**：PostgreSQL 完全不碰 trace；trace 的合成（已在 ES）、每段統計、合成進度全在 ES。不用 trace 的情境，PostgreSQL 裡看不到任何 trace 的東西。
- **範圍**：
  - 做：`aiops_trace_segments` → ES 索引 `aiops-trace-segments`；`aiops_state` 拆兩處，`trace-merge` 進度 → ES 索引 `aiops-trace-state`，`detect-watermark` 留 PostgreSQL（所有偵測情境都用）；保留天數改由 ES ILM；engine 的 ES key 補寫入權限。
  - 不做：已在 PostgreSQL 的舊 `aiops_trace_segments` 資料不搬（35 天內會自己過期，要的人另跑 SQL 即可）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detect/store/segment_repo.py` | 改回 ES 版 `SegmentRepository`（索引 `aiops-trace-segments`、dynamic false 的 mapping） | 每段統計是時間序列、量大，ES 本來就適合；且只有 trace 用 |
| `data/store/trace_state_repo.py`（新） | ES 版 `TraceStateRepository`（索引 `aiops-trace-state`），`get`／`set` | trace-merge 進度跟 trace 放一起；`repo.trace_state` |
| `data/trace/span_merge.py` | 進度改讀寫 `repo.trace_state` | PostgreSQL 不碰 trace |
| `common/infra/repos.py`、`interface/cli/db.py`、`common/infra/es_import.py`、`common/config/prune.py`、`configs/baseline/prune.yaml` | 名稱表加 `trace_state`；`db` 子命令與 prune 拿掉 `segments`；import-es 不再匯入 `aiops-trace-segments` | segments 不在 PostgreSQL 了；ES 的保留靠 ILM |
| `elasticsearch/kibana-dev-tools.txt` | 加 `aiops-trace-segments` 的 ILM（35d）與 `aiops-trace-state`；engine key 對 `aiops-trace-*` 加寫入 | 取代原本的 `detector prune` |
| `tests/fakes.py`、`tests/test_layers.py`、相關測試 | 記憶體版同步；`aiops_trace_segments` 不再是 PostgreSQL 表 | 測試跟著結構走 |
| `docs/data-storage-reference.md`、`docs/deploy-how-to.md`、`aiops-engine/src/common/README.md`、`CLAUDE.md`、`docs/pdca/records.md` | 同步現況 | 文件不留舊說法 |

## Check：測試的目的與結果

（做完補）

## Act：沒解決的

（做完補）
