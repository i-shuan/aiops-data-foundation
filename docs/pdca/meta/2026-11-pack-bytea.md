# 打包的資料改存 bytea（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/pack-bytea`（從 `claude/prune-pack` 開，那串還沒合回 master）；上一輪 [2026-11-prune-pack.md](2026-11-prune-pack.md)
**模組**：③（存）

## Plan

- **需求**：使用者問 PostgreSQL 有沒有 MongoDB 檔案（GridFS）的概念；說明 `bytea` 後說「改成 bytea」。
- **目的**：`aiops_packed` 直接存 gzip 的二進位，不再轉 base64（多 1/3）。
- **範圍**：只動 `aiops_packed` 的儲存格式；打包流程、`restore` 指令、設定不變。Large Object 不用（一包最多約 20 MB 未壓縮，`bytea` 夠）。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/common/infra/pack.py` | `pack` 回 `bytes`、`unpack` 吃 `bytes`；`PackedRepository` 自己建表（多一個 `data bytea` 欄）、自己寫 `add`／`packs`；`doc` 只放說明（table、day、n） | 共用基底只有 jsonb，bytea 要自己的 DDL 與 SQL |
| `aiops-engine/src/interface/cli/db.py`、`tests/test_prune.py` | `restore` 用 `packs(..., data=True)`；壓縮比測試改成 1/5 以內、確認是 `bytes` | |
| `docs/data-storage-reference.md`、`docs/deploy-how-to.md`、`docs/pdca/meta/2026-11-prune-pack.md` | 大小、欄位說明 | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 行為沒變 | `tests/test_prune.py`（打包、日期範圍讀回、寫不進去、`pack: null`、過期） | 全過 |
| 真的資料、大小 | 本機資料庫複製一份跑 `detector prune --at 2026-12-12`，再 `db restore`，前後比 md5 | 612 筆 jsonb 196 KB → 2 包共 **23.4 KB**（base64 版 31.5 KB，省 1/4）；讀回後整張表 md5 跟原本完全一樣 |

## Act

沒有。舊的 base64 版沒有部署過，不需要搬。
