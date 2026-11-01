# aiops-* 的保留天數與備份（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/aiops-retention-backup`；來源 [open-issues 第 7 節](../meta/2026-10-open-issues.md)、[data-storage-reference.md](../../data-storage-reference.md)
**模組**：③ 存、⑤ 異常總表、⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：AIOps 自己寫的索引裡，`aiops-alerts`、`aiops-anomalies`、`aiops-trace-segments` 沒有保留天數，會一直長；`aiops-incidents`、`aiops-feedback`、`aiops-tuning` 是人工判斷，掉了不能重算，卻沒有任何備份。
- **目的**：
  - 衍生資料有保留天數：超過天數的異常、trace 段統計、**已結案**的事故每天清一次；開著的件、心跳、`detect-status`、人工判斷的三個索引不動。
  - 人工判斷有每天的 snapshot：setup 腳本註冊 snapshot 儲存庫與 SLM 政策；測試環境用節點目錄當儲存庫，實測「備份 → 刪索引 → 還原 → 筆數對得上」。
- **範圍**：
  - 做：`detector prune`（排程每天跑）、`configs/baseline/prune.yaml`、setup 腳本的 snapshot 段、測試 chart 掛儲存庫目錄、還原步驟寫進 `elasticsearch/README.md`。
  - 不做：正式環境的儲存庫類型（S3、NFS）與位置，由管 ES 的人決定（腳本只在給了設定時才註冊）；`aiops-incidents`、`aiops-feedback`、`aiops-tuning` 不設保留天數（事故紀錄要長留）。
  - 不用 ILM：這三個是一般索引（`_id` 固定、每輪覆蓋），不是 rollover 的資料流；ILM 的 delete 會刪整個索引。用 `delete_by_query` 照文件的時間刪。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/cmd_prune.py`（新）、`detector/__main__.py` | `detector prune --config … [--dry-run] [--at]`：每個索引 `time_field < now − keep_days`（加上設定的 filter）先 `_count`，有才 `_delete_by_query`（`conflicts=proceed`、`refresh=true`、`slices=auto`），印每個索引刪了幾筆 | 一般索引不能用 ILM；`refresh` 是為了刪完馬上查得到、下一輪不重算 |
| `configs/baseline/prune.yaml`（新） | `aiops-anomalies` 90 天、`aiops-trace-segments` 90 天、`aiops-alerts` 結案（`status: resolved`、看 `resolved_at`）180 天 | 開著的件、心跳、`detect-status` 沒有 `resolved_at`，不會被刪；人工判斷的索引不列。天數是估的 |
| `configs/schedule/schedule.yaml` | 加 `prune`（stage 2、每天 03:30、600 秒） | 跟 `train`（02:00）錯開 |
| `tests/test_prune.py`（新）、`tests/test_scheduler.py` | 單元測試（cutoff、filter、dry-run、沒東西不打 delete、出貨設定只碰衍生索引）；排程表多一個工作（合進 master 後共 13 個） | |
| `elasticsearch/code/scripts/retention.conf` | `SNAPSHOT_INDICES`、`SNAPSHOT_KEEP=30d`、`SNAPSHOT_SCHEDULE` | 天數跟其他保留天數放一起 |
| `elasticsearch/code/scripts/elasticsearch-setup.sh` | 給了 `SNAPSHOT_REPO_TYPE`＋`SNAPSHOT_REPO_SETTINGS` 才：註冊儲存庫、`_verify`、SLM 政策 `aiops-daily`（`<aiops-{now/d}>`，`retention` 30 天、最少 7 最多 60 份）、`_slm/start`；沒給就印一行提醒跳過 | 儲存庫類型與位置由管 ES 的人決定；腳本不能替他們猜 |
| `elasticsearch/helmchart`（測試用） | `snapshots.enabled`（預設開）：節點目錄 hostPath 掛進每個 Pod 的 `/snapshots`、`path.repo`、initContainer 一起 chown | fs 儲存庫要所有 master、data 節點看得到同一個目錄；單節點的 Rancher Desktop 用 hostPath 就是同一個 |
| `elasticsearch/README.md` | 新增「備份與還原（aiops-* 的人工判斷）」：怎麼給儲存庫、看備份、馬上備一次、還原一個索引、還原到別的名字 | 操作文件 |
| `docs/data-storage-reference.md`、`docs/retention-reference.md`、`aiops-engine/README.md`、open-issues 第 7 節 | 保留與備份的現況改成做好的樣子 | 跟著程式 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| `prune` 算對 cutoff、帶 filter、dry-run 不刪、沒東西不打 delete、出貨設定只碰衍生索引 | `tests/test_prune.py` | 通過 |
| 真的刪得掉、不該刪的留著、重跑不重算 | 測試 ES 上另建三個臨時索引灌新舊文件：舊異常、舊 trace 段、已結案的舊告警、**開著的**舊告警、新文件；跑 `prune` 兩次 | 第一次：舊的三種刪掉、開著的舊告警與新文件留著；第二次：每個索引「0 筆要刪」，沒再打 delete |
| 排程表多了 `prune` | `tests/test_scheduler.py` | 通過；全部單元測試通過 |
| 測試 chart 的三個節點都看得到儲存庫目錄 | 重佈 chart，`GET _nodes/settings` | `elasticsearch-0`、`-1`、`voter-0` 的 `path.repo` 都是 `/snapshots` |
| setup 註冊儲存庫與 SLM 政策 | `SKIP_KEYS=1 SNAPSHOT_REPO_TYPE=fs SNAPSHOT_REPO_SETTINGS='{"location":"/snapshots"}'` 跑 setup | 儲存庫 `aiops`（fs）、`_verify` 過；政策 `aiops-daily`：每天 03:30、五個索引、留 30 天／最少 7／最多 60 份 |
| 備份 → 刪 → 還原，筆數對得上 | `POST _slm/policy/aiops-daily/_execute` → `DELETE aiops-tuning` → `_restore` 只還 `aiops-tuning` | 快照 5 個索引、5 個 shard 全成功、1.2 秒；刪後 404；還原後 4 筆 = 備份前 4 筆（其餘四個索引 59／14／26,040／43 筆沒動） |

**沒測到的**：排程器上 `prune`（03:30）、SLM（03:30）沒等到隔天實跑；snapshot 的 `retention`（30 天後刪舊快照）要等 30 天；正式環境的儲存庫類型（S3、NFS）沒碰；大索引（`aiops-baseline` 2.6 萬筆是 MB 等級）以外的量沒量。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 保留天數 90／90／180 天是估的 | 設計時 | 太短會砍掉還想回看的異常；太長只是多佔空間 | 試點後看查詢習慣再調 `prune.yaml` |
| 正式環境的儲存庫沒決定 | 設計時 | setup 沒給設定就沒有備份，只印一行提醒 | 管 ES 的人決定類型與位置，寫進部署步驟 |
| snapshot 與 `prune` 都排在 03:30 | 設計時 | `prune` 刪 `aiops-alerts` 的同一刻 SLM 可能在拍它 | 影響只是那天的快照多幾筆或少幾筆已結案告警，不處理 |
| 測試 chart 用 hostPath，只在單節點的 Rancher Desktop 成立 | 設計時 | 多節點的測試叢集要換共享儲存 | 不處理（chart 只給測試用） |
