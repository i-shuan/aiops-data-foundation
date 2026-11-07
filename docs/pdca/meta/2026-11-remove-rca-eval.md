# 移除根因分析的考卷、資料庫的連線設定收在 common（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/remove-rca-eval`（從 `claude/postgres-store` 開，那條還沒合回 master）；考卷是 [2026-11-cases-tier-drop.md](2026-11-cases-tier-drop.md) 做的
**模組**：⑦（根因分析）、③（存）

## Plan

- **需求**：使用者問「考卷改的是什麼功能」，說明後要求「先移除」；另外「DB 做完」「連線設定要記得寫在 common」。
- **範圍**：拿掉 `investigator rca-eval` 與它的程式、測試、文件；資料庫的連線設定補齊並集中在 `common/config/settings.py`。歷史線索、先查清單、P0、往下掉提重新學都不動。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/incident/rca_eval.py`（刪）、`interface/cli/rca.py` | 拿掉 `rca-eval` 指令 | 使用者要求 |
| `aiops-engine/tests/test_case_hints.py`、`tests/cases_tier_drop_e2e.py` | 拿掉考卷的單元測試與 e2e 的 ⑤ | 跟著程式 |
| `CLAUDE.md`、`docs/module-map.md`、`aiops-engine/configs/rca/README.md`、`docs/pdca/README.md` | 拿掉考卷的說明；「最弱的地方」的考卷那一項一起拿掉 | |
| `aiops-engine/src/common/config/settings.py`、`common/infra/pg.py` | `AIOPS_PG_CONNECT_TIMEOUT`（10 秒）、`AIOPS_PG_STATEMENT_TIMEOUT`（120 秒）、`AIOPS_PG_APP_NAME`（`aiops-engine`）；連線時套用 | 資料庫慢或連不上時排程的工作不卡住；DBA 看得到是誰在連 |
| `docs/deploy-how-to.md` | 第 1b 節列出 engine 的連線設定在哪 | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 考卷拿乾淨、其他功能沒壞 | 全部單元測試（含 helm）；`grep rca-eval` | 913 個過；程式、設定、共用文件裡沒有殘留（PDCA 紀錄保留當時的寫法） |
| 連線設定真的套用、查詢超時會中斷 | `tests/test_pg_repos.py::Connection`（真的 PostgreSQL） | `application_name`、`statement_timeout` 是設定的值；超過時間的查詢丟錯、不會卡住 |

## Act

- 考卷的程式在 git 歷史（`claude/rca-cases-severity-shift`）裡，之後要回歸測試根因分析時可以拿回來。
- 改 e2e 時一度把檔案內容重複貼了一份（切字串的位置找錯），pyflakes 抓到「函式重複定義」才發現，已從 git 還原重改。
