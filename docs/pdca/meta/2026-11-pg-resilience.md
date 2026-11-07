# PostgreSQL 斷線、權限、單點的處理（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/pg-resilience`（從 master `bb8c15b` 開）；弱點盤點 [weakness-inventory.md](../../weakness-inventory.md) 第 1、2 項
**模組**：③（存）、④（判斷）、⑧（出口）

## Plan

- **需求**：盤點出 PostgreSQL 是單點、權限要求比文件寫的高、斷線 0.6 秒就失敗、斷線超過 10 分鐘的偵測結果補不回來、資料庫掛了 Grafana 的告警也跟著失靈。使用者貼了一份業界做法，要求「全部都做」；第三點（斷線時的本機暫存）「可以先加 PVC，本地端先放一份，然後同步寫到 DB」。
- **目的**：
  1. DBA 只給讀寫權限（表事先用 `detector db init` 建好）也能跑。
  2. 資料庫重啟、主從切換的十幾秒撐得過去，不會 0.6 秒就失敗。
  3. 斷線超過 10 分鐘，恢復後 `detect` 自動補算空白的那段（有上限）；每一筆寫入先記在本機（PVC），再寫資料庫，寫不進去的之後照順序補寫；資料庫連不上一段時間，排程器直接打 webhook 通知（不經過 Grafana）。
  4. Grafana 可以改讀唯讀副本；有一個不靠資料庫的外部看門狗；DBA 申請清單加主從切換。
- **範圍**：PgBouncer 做成 helm 選用的 sidecar，**只用 session 模式**（transaction 模式跟工作鎖、連線設定不相容，見 [2026-11-job-lock.md](2026-11-job-lock.md)）。補算只做 `detect`（`rules`、`peer`、`trace-detect` 看的是「現在的狀態」，補舊的意義不大，Act 記下）。資料庫高可用本身是 DBA 的事，只寫進申請清單。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/common/infra/pg.py` | `ensure` 先用 `to_regclass` 查表在不在，在就不送 DDL；`ddl()`、`missing()` 給子類別改；`ensure(force=True)` 給 `db init` | PostgreSQL 的 `CREATE TABLE IF NOT EXISTS` 會先檢查 schema 的 `CREATE` 權限，表在不在都一樣（PG 15 起 public 預設也不給 CREATE） |
| `aiops-engine/src/common/infra/pack.py`、`incident/store/vector_repo.py`、`interface/cli/db.py` | 改成給 `ddl()`；向量表另外看 `embedding` 欄在不在；`db init` 用 `force` | 同上 |
| `aiops-engine/src/common/infra/pg.py` | `connect_with_retry`：連不上等 1、2、4、8、8… 秒（±20%），最多 `AIOPS_PG_RETRY_SECONDS`（30）；連線斷了才重連重跑，查詢逾時（`QueryCanceled`）不重跑；工作鎖的連線也用它 | 連線被拒絕只要幾毫秒，以前不等就重連一次，0.6 秒就失敗；DB 重啟、主從切換要十幾秒 |
| `aiops-engine/helmchart/`（`_helpers.tpl`、`deployment.yaml`、`distributed.yaml`、`values.yaml`） | 選用的 PgBouncer sidecar：固定 `POOL_MODE=session`、`SERVER_RESET_QUERY=DISCARD ALL`；engine 的容器改連 `AIOPS_PG_DSN_POOLED`；beat 不經過 | transaction 模式下工作鎖會失效（實測，見 Check）；`DISCARD ALL` 讓還給池子的連線放掉 SET 與鎖 |
| `aiops-engine/src/detect/config/series.py`、`detect/detect_service.py` | `catchup: {max: 6h, step: 30m}`；`detect_window`（拆出來）、`catchup_plan`；進度存 `aiops_state` 的 `detect-watermark:<設定檔>`（`covered_until`、空白的 `gap_from`／`gap_to`）；`--at` 回放不動進度 | 以前固定看最近 10 分鐘，停超過 10 分鐘那段永遠沒判。每輪只多補 30 分鐘：detect 逾時 50 秒，一次補太多會被砍、連進度都沒記 |
| `aiops-engine/src/common/infra/journal.py`（新）、`common/infra/pg.py` | 本機日誌：INSERT／UPDATE／DELETE 先記檔（fsync）再照順序寫資料庫，都寫進去就清空；寫不進去留著，同程式下次寫入先補、別的程式改名認領補寫；本機寫不進去照樣寫資料庫 | 使用者要「本地端先放一份，然後同步寫到 DB」。演練第一次發現：日誌資料夾權限錯時連資料庫都寫不進，改成本機那份只是額外保險 |
| `aiops-engine/src/interface/jobs/guard.py`（新）、`interface/jobs/runner.py` | 排程器每分鐘在背景看資料庫：連得上就補寫別人留下的日誌、打外部看門狗；連不上超過 `AIOPS_FALLBACK_AFTER_MINUTES`（5）直接 POST `AIOPS_FALLBACK_WEBHOOK`，之後每 60 分鐘提醒、恢復再通知 | Grafana 的告警也讀同一個資料庫，資料庫掛了它看不到；心跳規則靠資料庫，看門狗不靠 |
| `aiops-engine/src/common/config/settings.py` | `AIOPS_PG_RETRY_SECONDS`、`AIOPS_JOURNAL_DIR`、`AIOPS_FALLBACK_WEBHOOK`、`AIOPS_FALLBACK_AFTER_MINUTES`、`AIOPS_WATCHDOG_URL` | 環境變數只在這裡讀 |
| `aiops-engine/helmchart/`（`templates/journal.yaml` 新、`env.yaml`、`_helpers.tpl`、`deployment.yaml`、`distributed.yaml`、`values.yaml`） | `journal`：PVC `aiops-journal`（uninstall 不刪）、掛 scheduler 與 api、`fsGroup`；**ReadWriteOnce 時 single 改回 Recreate**（新 Pod 排到別台掛不上）、ReadWriteMany 維持 RollingUpdate；distributed 不是 ReadWriteMany 就每個 Pod 用 emptyDir；`fallback`、`watchdog` | 使用者同意加 PVC |
| `grafana/helmchart/files/datasource.yaml`、`grafana/README.md` | `AIOPS_PG_HOST` 可以填唯讀副本 | Grafana 的 Secret 本來就跟 engine 分開，填副本就好 |
| `docs/deploy-how-to.md` | 申請清單：建表的帳號（`db init`）與 engine 的帳號（只要讀寫）分開；高可用、多台的 DSN；唯讀副本 | |
| `aiops-engine/tests/`（`test_pg_repos.py`、`test_catchup.py`、`test_journal.py`、`test_guard.py` 新、`test_deploy.py`、`conftest.py`、`fixtures/config_snapshot.json`） | 見 Check；`conftest.py` 預設 `AIOPS_PG_RETRY_SECONDS=0`（故意測「DB 掛了」的測試不用等 30 秒） | |
| `docs/data-storage-reference.md`、`aiops-engine/configs/baseline/reference.md`、`docs/weakness-inventory.md`、`CLAUDE.md`、`aiops-engine/src/common/README.md` | 本機日誌、補算、`aiops_state` 的新用途；盤點第 2、9 項標已處理 | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 只有讀寫權限也能跑 | `test_pg_repos.py::LeastPrivilege`：拿掉 public 的 CREATE、角色只有 USAGE＋DML，表由有權限的帳號先建 | 新程式讀寫都過；**舊程式同一個測試失敗**（`permission denied for schema public`）；表沒建時錯誤是 `permission denied` |
| 重試真的有等、會放棄、會恢復 | `test_pg_repos.py::Retry` | 預算 3 秒：約 3 秒才放棄（不是 0.6 秒）；前兩次被拒絕：等了約 1、2 秒後連上；連線被 DBA 踢掉：自己重連、這次查詢照樣成功；查詢逾時不重連 |
| 多台的 DSN | 第一台是連不上的位址、第二台是本機資料庫，`target_session_attrs=read-write` | 連到第二台、`pg_is_in_recovery()` 是 false（主庫） |
| PgBouncer 跟工作鎖相容嗎 | 本機裝 PgBouncer，session 與 transaction 模式各開一個；A 拿著鎖 3 秒，第 1 秒 B 搶 | **session：B 搶不到（正確）；transaction：B 也搶到（鎖失效）**。另外池子只給 1 條時 B 會排隊等 A 用完才拿到——不是同時，但同一分鐘會跑兩次（**問題，見 Act；預設池子維持 10，不用放大池子當解法**） |
| **最緊的池子：1 條，兩個排程器同時跑會寫資料庫的工作**（不放大池子） | 本機 PgBouncer（session）`default_pool_size=1`、`query_wait_timeout=60`；兩個 `python3 -m main scheduler` 各跑同一份排程表，工作每分鐘一次、真的連資料庫寫一筆再睡 20 秒；跑 4 分鐘 | **0 筆寫進資料庫**：鎖佔掉唯一的連線，工作子行程等不到連線，60 秒後 `query_wait_timeout` 失敗；另一個排程器連鎖都搶不到（「沒跑：連不到 PostgreSQL」）；看守也判成資料庫連不上。**問題，見 Act** |
| **池子 2 條，工作跑 20 秒／75 秒**（對照；`alert` 有 LLM 時可能到這個長度） | 同上，池子 2 | 20 秒：5 分鐘 5 次、一次一個、沒有重複；75 秒：工作超過一分鐘，每次跑完下一輪才有人接（18、20、22 分各一次，另一個排程器每分鐘等一個空位後印「跳過」），沒有同一分鐘跑兩次。但跟單一排程器一樣，75 秒的工作每兩分鐘才跑一次。**池子 2 條夠一個工作，不夠兩個同時開工的** |
| 補算不漏不重 | `test_catchup.py`：停 2 小時、每輪補 30 分鐘；補到一半又停；停兩天；`max: 0s`；`--at` 回放 | 4 輪補完、一段接一段、補到發現空白那一輪的開頭為止（第一版會多補一段已經判過的，修了）；第二次停機併進同一段；停兩天只補 6 小時；回放不動進度 |
| 本機日誌 | `test_journal.py`（真的 PostgreSQL） | 平常檔案清空、結束刪掉；寫不進去那筆留著，下次寫入先補、照順序（同 id 寫兩次最後是第二次的）；死掉的程式留下的被認領一次、補完刪掉、第二個人補不到；寫到一半的最後一行跳過；補到一半又失敗，檔案放回去下次接著補；bytea、時間寫得回去；日誌資料夾不能寫時照樣寫資料庫 |
| 看守 | `test_guard.py` | 停不到 5 分鐘不吵；第 5 分鐘通知、第 65 分鐘再提醒、恢復通知一次；看門狗只在連得上時打；webhook 壞了只記一行 |
| helm | 從原始碼編 helm 3.16（`go install`），跑 `tests/test_deploy.py`、`helm lint`（single、distributed＋PgBouncer＋ReadWriteMany） | 25 個全過、lint 過。**前幾輪沒渲染過的（PostgreSQL、embedding、RollingUpdate）這次也都渲染過了** |
| **真的停資料庫** | 本機排程器（看守開著，webhook、看門狗指到本機的假服務）＋每 2 秒寫一筆的程式，停 PostgreSQL 150 秒再開 | 停的期間 21 筆寫入失敗（各等 5 秒重試）、日誌檔裡有 1310 bytes；**恢復後全部補寫：寫了 110 筆、資料庫 110 筆**，日誌檔清空；停 1 分鐘後 webhook 收到 `db_down`、恢復收到 `db_recovered`；看門狗停機期間沒有 ping |
| 全部沒壞 | 全部單元測試（helm 在 PATH 上）、`tools/check-docs.py` | 全過（只有一個「root 不受資料夾權限限制」的跳過，那一項在演練裡以 postgres 帳號實際碰到） |

- **演練第一次就發現的 bug**：日誌資料夾權限設錯時，每一筆寫入都失敗（`PermissionError`），連資料庫好好的也寫不進去。改成本機寫不進去照樣寫資料庫、印一行警告。
- **沒測到的**：真的叢集（PVC 的 `fsGroup`、PgBouncer 的 image 用不用 `DATABASE_URL`、ReadWriteMany 的儲存）；detect 補算對真的 ES 資料（只用模擬的 `detect_window` 測了計畫與進度；一輪多判 30 分鐘會不會超過 50 秒逾時要看正式量）；DBA 的主從切換實際要多久。

## Act

| 沒解決的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 讀也要資料庫：資料庫斷線時工作一開始讀基準線、開著的件就失敗 | 設計本機日誌時 | 本機日誌只保住「讀完之後、寫的時候斷掉」的那幾筆；斷線期間的判斷要靠恢復後 detect 補算 | 要斷線時照樣判斷，得把基準線等也放本機一份（讀的快取），這次沒做 |
| ReadWriteOnce 的 PVC 讓 single 回到先停再起 | 加日誌 PVC 時 | 換版又有空窗 | 問平台有沒有 ReadWriteMany；有就改 `journal.accessMode` |
| 補算只做 detect | 範圍 | `rules`、`peer`、`trace-detect` 停過的那段不補 | 它們看的是「現在的狀態」，要補再說 |
| 補算出來的舊異常會照常開件、通知 | 使用者沒決定 | 停很久恢復時，可能一次來好幾件「幾小時前」的通知 | 看實際情況再決定要不要標「補算」、不通知 |
| 單一工作失敗沒告警 | 盤點第 1 項 | 同上一輪 | 另外做 |
| **PgBouncer 池子太小：工作鎖佔一條、工作自己再要一條，兩條都要才動得了；池子被鎖佔滿，工作全部卡死** | 使用者要求用最緊的資源測：本機 PgBouncer（session 模式）池子 1、2 條，兩個排程器同時跑一個會寫資料庫的工作（見 Check 最後兩列） | 池子 1 條：**4 分鐘一筆資料都沒寫進去**（鎖拿走唯一的連線，工作的程式等不到連線，卡到 `query_wait_timeout` 60 秒後失敗；另一個排程器連鎖都搶不到）。算實際排程：stage 2 同一分鐘（02:00）最多 **9 個工作同時開工 → 要 18 條**，預設池子 10 條就會卡死，stage 1 最多 5 個正好用掉全部 10 條（api、看守、補寫日誌的連線都沒位子）。**預設池子 10 不夠，而且放大池子不是解法**（CLAUDE.md） | 長久做法列在下面，**等使用者選** |

**預設不歸「放大池子」。這一輪預設值維持 10，已知會在 02:00 卡死（PgBouncer 預設關著，所以還沒影響任何部署），選了做法才修。**

**長久做法（等使用者選）**

| 做法 | 怎麼運作 | 改哪裡 | 代價、風險 | 能不能根治 |
|---|---|---|---|---|
| **A. 鎖走直連，工作走連線池**（建議） | 鎖的連線不經過 PgBouncer（多一個 `AIOPS_PG_DSN_LOCK`，直連 PostgreSQL），只佔一條閒置的連線；池子只給工作自己用，需求從「2 × 同時工作數」降成「1 × 同時工作數」 | `common/infra/pg.job_lock`、`common/config/settings.py`、helm 的 Secret／`pgEnv` | PostgreSQL 的連線數多：每個在跑的工作一條閒置連線（stage 2 最多 9 條，每個 Pod）；要多一個 DSN | 根治「鎖佔池子」；池子仍要 ≥ 同時工作數，要在文件寫清楚怎麼算 |
| B. 不用 PgBouncer | 子行程每分鐘開 5～9 條、各連一次，在這個規模 PostgreSQL 吃得下（連線每次建立約 10 毫秒）；拿掉 sidecar 與 `AIOPS_PG_DSN_POOLED` | helm、`values.yaml`、文件 | 沒有連線池；DBA 的 `max_connections` 要夠：每個 Pod 最多 ~20 條 | 根治（問題的來源拿掉）；等連線數真的成問題再加回來 |
| C. 鎖放在工作自己的連線上 | 不在排程器拿鎖，改在子行程啟動時用它本來就要開的那條連線拿鎖；一條連線做完所有事，工作結束連線就關、鎖就放 | 每個子命令入口（`main.py` 派發前）、`runner.run_once` | 連到資料庫前都沒有鎖，兩個排程器同時啟動兩個子行程再互相發現，浪費一次啟動；沒用資料庫的工作（測試用的）沒有鎖 | 根治「鎖佔池子」；跳過的成本變高 |
| D. 用租約（lease）取代連線鎖 | 在 `aiops_state` 寫「這個工作在 N 秒內歸我」，到期自動失效，不用一直握著連線；transaction 模式的 PgBouncer 也能用 | `job_lock` 重寫、`aiops_state` | 要處理過期時間與時鐘；工作跑超過租約要續約，續約失敗要處理 | 根治，連 transaction 模式都相容；改動最大 |

**另外：前面寫的「`alert` 的 LLM 會多花一次錢」不對（已更正）**：LLM 只在「新開的件、沒報告的件、人要求重跑／trace 到了重跑」時才叫（`incident/alert_service.investigate_alert`）。同一批異常的第二次跑，那件已經有報告，不會再叫模型。只有兩個 `alert` **同時**各自把同一件當成新的，才會叫兩次，這正是工作鎖要擋的。
