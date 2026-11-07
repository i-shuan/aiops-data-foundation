# 最緊資源測試

> **類型：操作 How-To**

CLAUDE.md「工作方式」：測試要在資源最不充裕的條件下做，把問題全部測出來；測出問題不准用放大容量當解法。這個資料夾是那一套測試，每次改了連線、排程、日誌、記憶體相關的程式就重跑。

要 root、本機有 PostgreSQL（`pg_ctlcluster 16 main`）、`pgbouncer`、cgroup v1（只有記憶體那一組用到；cgroup v2 的環境那一組會失敗）。沒有 ES：`detect`、`rules`、`train` 用替身（名字、逾時照真的排程，指令是只讀 PostgreSQL 的真指令 `prune --dry-run`）。

```bash
cd aiops-engine
tests/tight/run.sh storm     # 池子 20 條／4 CPU（對照）→ 池子 2 條／1 CPU，9 個工作同一分鐘開工；再拆開各自測
tests/tight/run.sh mem       # 容器記憶體 2000 MB（對照）→ 800 MB，train 替身（700 MB）放不下
tests/tight/run.sh journal   # 本機日誌的磁碟 256 KB、資料庫停 25 秒、重試 0 秒
tests/tight/run.sh blip      # 資料庫斷 20 秒、剛好在每日工作開工時：重試 0 秒＋連線逾時 1 秒 → 重試 30 秒
tests/tight/run.sh poison    # 日誌裡一筆永遠補不進去的寫入
tests/tight/run.sh all
```

每個情境印「對照」與「最緊」的結果。條件、結果、發現的問題與各自的長久做法見 [docs/pdca/meta/2026-11-tight-resource-test.md](../../../docs/pdca/meta/2026-11-tight-resource-test.md)。

- 資料庫用一個全新的 `aiops_tight`（每次重建、`detector db init` 建表），不碰別的資料。
- `blip`、`journal` 會 `pg_ctlcluster 16 main stop／start`：本機只有一個叢集時，別的測試同時在跑會被打斷。
- 沒做的：真的 `detect`／`rules`／`train` 的 CPU、記憶體尖峰（要 ES）；cgroup v2＋Kubernetes 1.28 以上 OOM 時殺整個容器。
