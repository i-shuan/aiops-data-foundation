# 歸檔不建 PVC，預設不歸檔（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/prune-no-pvc`（從 `claude/prune-common` 開）；上一輪 [2026-11-prune-archive.md](2026-11-prune-archive.md)
**模組**：③（存）

## Plan

- **需求**：使用者看了 PVC 的說明後說「不要加 pvc」。
- **目的**：chart 不建儲存空間。歸檔仍可用，但要使用者自己指定一個重建不會消失的資料夾；沒指定就像以前直接刪。
- **範圍**：拿掉 helm 的 PVC、掛載、fsGroup；`prune.yaml` 的 `archive` 改成預設不開（註解範例）；helm 加 `archiveDir`（空 = 不歸檔）。歸檔的程式（`archive.py`、`prune.py`、`db restore`）不動。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/helmchart/`（`templates/archive.yaml` 刪、`_helpers.tpl`、`deployment.yaml`、`distributed.yaml`、`env.yaml`、`values.yaml`） | 還原成加 PVC 之前的樣子；`archiveDir` 有填才設 `AIOPS_ARCHIVE_DIR` | 使用者不要 PVC |
| `aiops-engine/configs/baseline/prune.yaml`、`src/common/config/prune.py`、`tests/fixtures/config_snapshot.json` | `archive` 註解掉（預設不歸檔）；預設路徑改 `/mnt/aiops-archive` | 沒有 PVC 時預設路徑會寫在容器裡，換版就沒了、資料庫又已刪，比不歸檔更糟 |
| `aiops-engine/tests/test_deploy.py` | 拿掉 PVC 的測試 | 跟著 helm |
| `docs/data-storage-reference.md`、`docs/deploy-how-to.md`、`docs/module-map.md` | 改成「預設不歸檔，要歸檔自己指定資料夾」 | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 歸檔的程式沒壞 | `tests/test_prune.py`（歸檔、讀回、寫不進去、環境變數）、全部單元測試 | 全部過；設定快照只有 `archive` 變成 null |
| helm 還原乾淨 | `git diff 7780468 -- aiops-engine/helmchart`（`archiveDir` 之外） | 與加 PVC 前相同；helm 本身這個環境沒有，未渲染 |

## Act

| 沒解決的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 要歸檔得自己掛資料夾，chart 沒有提供掛載方式 | 拿掉 PVC 時 | 想歸檔的人要自己改 Deployment（或請平台加） | 真的要用時再決定：公司的 NFS 掛載，或改送物件儲存 |
