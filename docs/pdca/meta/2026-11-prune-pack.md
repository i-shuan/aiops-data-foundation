# 過期資料改成壓縮留在資料庫、不歸檔（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/prune-pack`（從 `claude/prune-no-pvc` 開，那串還沒合回 master）；取代 [2026-11-prune-archive.md](2026-11-prune-archive.md) 的檔案歸檔與 [2026-11-prune-no-pvc.md](2026-11-prune-no-pvc.md)
**模組**：③（存）

## Plan

- **需求**：使用者說「35 天之前的檔案做壓縮，不需要歸檔」；問「壓縮方式」時選「壓縮後留在資料庫」。
- **目的**：超過保留天數的資料不寫檔、不用資料夾、不用 PVC，而是壓縮後仍在 PostgreSQL 裡，查得回來。
- **範圍**：拿掉檔案歸檔（`archive.py`、`AIOPS_ARCHIVE_DIR`、helm 的 `archiveDir`）；`prune` 改成「一天打包成一筆 gzip 放 `aiops_packed` → 才刪細筆」；`db restore` 改從 `aiops_packed` 讀回。三張被 prune 的表（異常 90、trace 統計 35、結案告警 180 天）都適用。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/common/infra/archive.py`（刪）→ `pack.py`（新） | `pack`／`unpack`（gzip + base64 的 JSON Lines）；`PackedRepository`（`aiops_packed`，`add` 依天分包、`packs` 依表／日期查） | 使用者不要檔案；資料庫存二進位要 bytea，但 repository 基底是 jsonb，所以用 base64（多 1/3）換不動基底 |
| `aiops-engine/src/common/infra/prune.py` | 一批一批「讀 → 打包寫進 `aiops_packed` → 才刪」；打包寫失敗那一批不刪、回 1；`pack.keep_days` 之後刪打包資料；`pack: null` 直接刪 | 同上一輪的「先寫好才刪」，只是目的地從檔案變資料庫 |
| `aiops-engine/src/common/config/prune.py` | `PrunePack`（`keep_days` 預設 365、`batch`）取代 `PruneArchive`；預設就開 | 使用者要的是預設就壓縮 |
| `aiops-engine/src/common/infra/repos.py`、`interface/cli/db.py` | 登記 `packed`；`db restore --table --from --to --dry-run` 改從 `aiops_packed` 讀 | 不再要路徑 |
| `aiops-engine/src/common/config/settings.py`、`helmchart/`（`env.yaml`、`values.yaml`） | 拿掉 `AIOPS_ARCHIVE_DIR`、`archiveDir` | 沒有資料夾了 |
| `aiops-engine/configs/baseline/prune.yaml`、`tests/fixtures/config_snapshot.json` | `pack: {keep_days: 365}` | |
| `aiops-engine/tests/test_prune.py` | 打包、讀回、寫不進去、`pack: null`、打包資料過期、壓縮比 | |
| `docs/data-storage-reference.md`、`docs/deploy-how-to.md`、`docs/module-map.md`、`CLAUDE.md`、`aiops-engine/README.md`、`aiops-engine/src/common/README.md` | 「歸檔」改成「壓縮」；新增 `aiops_packed` 一列 | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 打包不漏不重、依天分開 | `tests/test_prune.py::Pack`（真的 PostgreSQL）：6 筆舊異常、一次 2 筆分 3 批 | 解開共 6 筆 id 全對；日期是台北時間（UTC 7/1 23:30 算 7/2）；新的那筆沒動；告警只打包已結案的 |
| 讀回跟原本一模一樣、日期範圍有效 | 同上，prune 後 `db restore --from/--to`，再全部讀回 | `--from 2026-07-02 --to 2026-07-02` 只回 3 筆；全部讀回後每筆文件相同、`ts` 照原本 |
| 寫不進去不刪 | 讓 `PackedRepository.add` 丟「disk full」 | 回 1、印原因、6 筆都還在 |
| `pack: null`、打包資料過期 | 同上 | null 時 6 筆直接刪、`aiops_packed` 空；2027-06-01 時 2026-03-01 的包被刪、365 天內的留著 |
| 真的指令跑一遍 | 本機資料庫複製一份，`detector prune --at 2026-12-12`、`db restore`，前後比 md5 | trace 統計 612 筆（jsonb 196 KB）打包成 2 包共 31.5 KB（**約 1/6**）；讀回後 1,328 筆，整張表 md5 跟原本**完全一樣** |
| 全部沒壞 | 全部單元測試、`tools/check-docs.py` | 全過；設定快照只有 `archive` → `pack` |

- **沒測到的**：正式環境的量（中型規模每天 72 萬筆打包後約 35 MB，是照實測比例推的）；一天資料量極大時，一包的大小（一包最多 `batch` 筆，約 20 MB 未壓縮）對 PostgreSQL 的影響。

## Act

| 沒解決的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 打包的資料仍佔資料庫空間（約原本的 1/6），不是搬走 | 設計時：使用者要「壓縮、不歸檔」 | 一年約 12 GB（中型規模，推論） | `pack.keep_days` 可以調短；要完全搬出資料庫再另外討論（物件儲存） |
| base64 比直接存 bytea 多 1/3 | 實作時 | 大小多 1/3 | 之後 `PgRepository` 支援 bytea 欄再換 |
