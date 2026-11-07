# 過期資料先壓縮歸檔再刪；trace 每段的統計改留 35 天（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/prune-archive`（從 `claude/blocks` 開，那條還沒合回 master）
**模組**：③（存）

## Plan

- **需求**：使用者問 trace 每段的統計（`aiops_trace_segments`）會不會爆。說明它筆數固定、但 90 天中型規模約 6,500 萬筆後，要求「改成 35 天」，並問「db 多餘的資料可以自動壓縮到某個資料夾內嗎」。
- **目的**：
  1. `aiops_trace_segments` 只留 35 天（trace-train 最多回看 4 週，多留的沒人讀）。
  2. `detector prune` 刪之前，先把要刪的那幾筆壓縮存成檔案（一天一個 `.jsonl.gz`）；要查舊資料時可以讀回 PostgreSQL。
  3. 歸檔寫不進去（資料夾不存在、沒權限、磁碟滿）時**一筆都不刪**，下一次再試。
- **範圍**：做歸檔、讀回（`detector db restore`）、歸檔檔案本身的保留天數（選用）、helm 的 PVC。不做：送到物件儲存（S3／MinIO）、事故等人工判斷的表（本來就不刪）。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/configs/baseline/prune.yaml` | `segments` 90 → 35 天；加 `archive`（`dir: /var/lib/aiops/archive`、`keep_days: 365`） | trace-train 最多回看 4 週，多留的 55 天沒人讀；歸檔檔案留一年是估的，看磁碟調 |
| `aiops-engine/src/common/infra/archive.py`（新） | 寫（一天一個 `.jsonl.gz`，同一天接在後面、寫完 fsync）、讀（不完整的尾巴跳過）、列檔、依檔名日期刪舊檔 | 歸檔跟哪張表無關，放 common；格式選 JSON Lines + gzip：`zcat` 就看得懂、不用工具，跟 PostgreSQL 版本無關 |
| `aiops-engine/src/common/infra/pg.py` | `PgRepository` 加 `PRUNE_WHERE`、`prune`、`prune_count`、`prune_rows`（最舊的 N 筆，含 `ts`）、`delete_ids`、`restore`（照原本的 id、ts 寫回） | 三個 repository 原本各寫一份 `prune`、`prune_count`；歸檔要「讀出來的那幾筆」跟「刪的那幾筆」是同一個條件，收成一處 |
| `aiops-engine/src/detect/store/anomaly_repo.py`、`segment_repo.py`、`incident/store/alert_repo.py` | 拿掉各自的 `prune`／`prune_count`；告警改設 `PRUNE_WHERE`（只刪已結案、依結案時間） | 同上，語意不變 |
| `aiops-engine/src/detect/config/prune.py` | `PruneArchive`（`dir`、`keep_days`、`batch`）；`PruneConfig.archive` 不寫 = 不歸檔 | |
| `aiops-engine/src/common/config/settings.py` | `AIOPS_ARCHIVE_DIR`：蓋過 `archive.dir`，`off` = 不歸檔 | helm 要依有沒有 PVC 切換；本機開發沒有 PVC 時可以關掉 |
| `aiops-engine/src/detect/housekeeping.py` | 有歸檔時：先試寫資料夾，寫不進去一筆都不刪、回 1；一批一批「讀 → 寫檔 → 刪」；最後刪超過 `keep_days` 的歸檔檔案。改用 `Repos(None, …)` | 檔案寫好才刪，當掉也不會掉資料。實跑時發現 prune 只動 PostgreSQL 卻要 `ES_URL`，順便拿掉 |
| `aiops-engine/src/interface/cli/db.py` | `detector db restore <檔案或資料夾…> [--table] [--dry-run]`：一次寫 5,000 筆 | 一天的 trace 統計可能幾十萬筆，整個檔讀進記憶體會太大 |
| `aiops-engine/helmchart/`（`values.yaml`、`templates/archive.yaml`（新）、`_helpers.tpl`、`deployment.yaml`、`distributed.yaml`、`env.yaml`） | `archive.enabled`（預設開）建 PVC `aiops-archive`（`helm.sh/resource-policy: keep`）；single 掛在 scheduler、distributed 只掛在 batch worker（prune 在 batch 佇列）；`fsGroup: 10001`；`AIOPS_ARCHIVE_DIR` 有 PVC 時是路徑、沒有時是 `off`；可以填 `existingClaim` | 容器裡的資料夾 Pod 重建就沒了，要 PVC；歸檔是唯一的一份，uninstall 不能跟著刪 |
| `aiops-engine/tests/test_prune.py`、`test_deploy.py`、`fixtures/config_snapshot.json` | 歸檔、讀回、寫不進去、環境變數、截斷、過期檔案；helm 的 PVC、掛載、環境變數 | |
| `docs/data-storage-reference.md`、`docs/deploy-how-to.md`、`docs/module-map.md`、`CLAUDE.md`、`aiops-engine/src/common/README.md` | 35 天；「過期資料的歸檔」（規格）；「過期資料的歸檔讀回」（步驟） | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 歸檔不漏不重、依天分檔 | `tests/test_prune.py::Archive`（真的 PostgreSQL）：6 筆舊異常、一次 2 筆分 3 批 | 兩個檔（7/1、7/2；UTC 23:30 依台北時間算到 7/2）共 6 筆、id 都對；新的那筆沒動；告警只歸檔已結案的 |
| 讀回跟原本一模一樣 | 同上，prune 後 `db restore --table aiops_anomalies` | 每筆文件相同；`ts` 照原本的（依時間查得到）；`--table` 只讀回指定的表 |
| 寫不進去不刪 | 路徑底下是檔案（建不了資料夾）；`ARC.write` 丟「No space left on device」 | 回 1、印出原因、6 筆都還在 |
| 環境變數蓋過、off | `AIOPS_ARCHIVE_DIR=<資料夾>`（設定沒寫 archive）、`AIOPS_ARCHIVE_DIR=off` | 前者歸檔到那個資料夾；後者直接刪、沒建資料夾 |
| 寫到一半當掉讀得到前面的 | 同一檔接三段，第三段砍到只剩 15 bytes | 讀回前兩段的 3 筆，沒丟錯 |
| 歸檔檔案過期 | 2025-10-01、2026-06-01 兩個檔，`keep_days` 365、今天 2026-11-01 | 只刪 2025-10-01 那個 |
| 真的指令跑一遍 | 本機資料庫複製一份，`detector prune --at 2026-12-12 --dry-run`、正式跑、`zcat`、`db restore`，前後比 md5 | 35 天的 cutoff 對；trace 統計 612 筆歸檔成 2 個檔（2026-11-06 13.7 KB、11-07 9.8 KB，原本 jsonb 196 KB，**約壓成 1/8**）、資料庫剩 716 筆；`zcat` 看得懂；讀回後 1,328 筆，整張表 md5 跟原本**完全一樣**；`db restore /nope` 印「讀不到」 |
| 全部沒壞 | `cd aiops-engine && python3 -m pytest -q`；`python3 tools/check-docs.py` | 全部過；設定快照只有 35 天與 archive 兩處變了 |

- **實跑時發現的問題**：`detector prune` 只動 PostgreSQL，卻要設 `ES_URL` 才能跑（`Repos(ES(), …)`），改成 `Repos(None, …)`；`db restore` 給錯路徑時是 Python traceback，改成一句話。
- **沒測到的**：helm 的渲染（這個環境沒有 helm，下載被擋）：`tests/test_deploy.py` 加了 PVC、掛載、環境變數、`existingClaim` 的測試，有 helm 的地方會跑，**這次是跳過的**；PVC 在真的叢集上的權限（`fsGroup`）未驗證。

## Act

| 沒解決的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| helm 的改動沒有渲染過 | 這個環境沒有 helm | 模板寫錯要到部署（或 CI）才知道 | 在有 helm 的地方跑 `python3 -m pytest -q tests/test_deploy.py` |
| 歸檔只在一個 PVC，沒有第二份 | 設計時 | PVC 壞了歸檔就沒了 | 要更保險：送物件儲存（S3／MinIO），或請 DBA／平台把 PVC 一起備份 |
| distributed 的 batch worker 多於 1 份時，`ReadWriteOnce` 的 PVC 掛不上 | 設計時 | batch worker 擴到 2 份會起不來 | values 註解寫了；要擴就改 `archive.accessMode: ReadWriteMany` |
| 讀回來的舊資料隔天會被 prune 再歸檔、刪掉（歸檔檔案多一份重複的） | 設計時 | 重複的同 id，再讀回是覆蓋，不影響正確性 | 要查久一點先停 prune（deploy-how-to 寫了） |
