# 設計弱點盤點（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/weakness-inventory`（從 master 開）
**模組**：①～⑨

## Plan

- **需求**：使用者要求「檢查一下目前設計的弱點，盤點就好」，再要求寫進 md。
- **目的**：一份可以回頭對的弱點清單。
- **範圍**：只盤點、寫文件；不改程式、不排優先順序以外的決定。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `docs/weakness-inventory.md`（新） | 22 項弱點，分「會出事的」「沒驗證過的」「還沒做的」 | 使用者要求 |
| `docs/README.md` | 加一列 | CLAUDE.md：新增 md 同一個 commit 更新索引 |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 清單的依據 | 對照 `docs/pdca/README.md` 的待辦與本輪做過的事（PostgreSQL、積木、壓縮、決定不做的項目） | 22 項都對得到來源；「engine 停了沒告警」「提調整的人可以核准自己提的」是看程式與 Grafana 規則推的，**未逐一驗證** |
| 文件規則 | `python3 tools/check-docs.py` | 全過 |
| #1、#2 逐項查證（使用者追問「詳細說說」） | 讀 `alert-rules.yaml`、`runner.py`、`pg.py`、排程表；本機 PostgreSQL 16 用只有 DML 權限的角色跑 `CREATE TABLE IF NOT EXISTS` | **更正**：原寫「沒有 engine 停了的告警」不對，`aiops-heartbeat` 規則存在，真正的缺口是單一工作失敗沒人知道；**新發現**：沒有 CREATE 權限時 `IF NOT EXISTS` 也會 `permission denied`，`deploy-how-to.md` 原寫「事先 `db init` 之後只需要讀寫」不成立，已更正 |

## Act

無。清單不是待辦，要做哪幾項由使用者決定。
