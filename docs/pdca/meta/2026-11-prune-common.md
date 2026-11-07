# prune（保留天數、歸檔）搬進 common（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/prune-common`（從 `claude/prune-archive` 開，那串還沒合回 master）；上一輪 [2026-11-prune-archive.md](2026-11-prune-archive.md)
**模組**：③（存）

## Plan

- **需求**：使用者問歸檔「這些都是寫在 common 裡面喔」。說明只有歸檔的工具（`archive.py`、`PgRepository`、環境變數）在 common，prune 的流程與設定在 `detect/` 之後，使用者要搬。
- **目的**：`detector prune` 的流程、設定模型都在 common；指令與行為不變。
- **範圍**：只搬檔、改 import 與文件；不改邏輯。`detector prune` 指令留在 `interface/cli/detect.py`（指令一律在 interface）。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/detect/housekeeping.py` → `src/common/infra/prune.py` | 搬（`git mv`），內容只改 import | prune 清的是異常、trace 每段的統計（detect）與告警（incident），不專屬 detect；只靠 common 的 `Repos`、`archive`、`settings`，放 common 不違反分層 |
| `aiops-engine/src/detect/config/prune.py` → `src/common/config/prune.py` | 搬 | 跟著流程 |
| `aiops-engine/src/common/config/loader.py`、`interface/cli/detect.py`、`common/infra/pg.py`、`tests/test_prune.py` | 改 import、註解 | |
| `CLAUDE.md`、`aiops-engine/README.md`、`aiops-engine/src/common/README.md` | 結構說明改到 common | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| common 沒有往上依賴 detect／incident | `tests/test_layers.py` | 過（`common/infra/prune.py` 只 import common） |
| 行為不變 | 全部單元測試（含 `tests/test_prune.py` 的歸檔、讀回、寫不進去）、`tests/test_config_snapshot.py`（`prune.yaml` 載入結果） | 全部過；快照沒變 |
| 指令還在 | `python3 -m main detector prune --help` | 照舊 |

## Act

無。
