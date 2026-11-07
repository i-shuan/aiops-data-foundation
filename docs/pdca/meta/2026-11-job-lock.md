# 同一個工作同時只跑一份（搶鎖）；換版改 RollingUpdate（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/job-lock`（從 `claude/weakness-inventory` 開，還沒合回 master）；弱點盤點 [weakness-inventory.md](../../weakness-inventory.md) 第 1 項
**模組**：④⑥⑧（排程、收斂）

## Plan

- **需求**：使用者問「換版空窗」與「改成 RollingUpdate 會怎樣」。說明 RollingUpdate 新舊重疊時兩個排程器會同時跑同一個工作（`alert` 可能多開一件、LLM 跑兩次），使用者要求「重複跑要驗證，執行期間先判斷有沒有在執行」。
- **目的**：不管幾個排程器或 worker（換版重疊、手動多開），同一個工作同一時間只有一份在跑；然後換版改成先起新的再停舊的，沒有空窗。
- **範圍**：鎖做在 `run_once`（single 的排程器、distributed 的 worker、`tasks run-once` 都經過它）；single 的 Deployment 與 distributed 的 alert worker 改 RollingUpdate。beat 維持 Recreate（理由見 Do）。卡死、OOM、機器維修造成的重啟空窗不在這次範圍。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/common/infra/pg.py` | `job_lock(name)`：專用連線上的 `pg_try_advisory_lock(LOCK_SPACE, hashtext('job:<名字>'))`；跑完關連線就放掉；TCP keepalive（約 1 分鐘認出斷線）；沒設 `AIOPS_PG_DSN` 不鎖 | 鎖跟著連線：拿鎖的程式死掉、連線斷掉，PostgreSQL 自動放掉，不會留下卡住的鎖（不用自己寫過期、清理）。鎖在資料庫裡，Pod 之間不用另外的協調服務（不加 Redis、不用 K8s lease） |
| `aiops-engine/src/interface/jobs/runner.py` | `run_once` 先搶鎖：搶不到印「跳過：別的排程器或 worker 正在跑這個工作」、回成功；連不到 PostgreSQL 印「沒跑」、回失敗、不開子行程 | 連不到資料庫就不知道別人有沒有在跑；而且每個工作本身都要資料庫，跑了也是失敗 |
| `aiops-engine/helmchart/templates/deployment.yaml` | `Recreate` → `RollingUpdate`（`maxSurge: 1`、`maxUnavailable: 0`） | 先起新的、好了才停舊的：沒有空窗；重疊靠鎖 |
| `aiops-engine/helmchart/templates/distributed.yaml` | alert worker 改 RollingUpdate；beat 維持 Recreate 並註明理由 | 鎖只擋「同時」：兩個 beat 重疊會把同一分鐘的工作丟兩份，前一份跑完後一份還會再跑（重跑是覆蓋，只是白跑）；beat 重啟很快、空窗只是晚幾秒丟 |
| `aiops-engine/helmchart/values.yaml`、`aiops-engine/README.md`、`docs/weakness-inventory.md` | 說明改成 RollingUpdate＋鎖；盤點第 1 項標「換版空窗已處理」 | |
| `aiops-engine/tests/test_job_lock.py`（新）、`tests/test_deploy.py` | 見 Check；helm 測試改成 RollingUpdate | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 兩個排程器同時跑同一個工作，只有一個真的跑 | `tests/test_job_lock.py`（真的 PostgreSQL）：另一個**獨立行程**先跑 `alert`（睡 3 秒），這邊同時再跑 | 這邊印「跳過」、回成功；那邊 `ok`；子行程只開了一次（記號檔只有一個） |
| 不同工作不互擋、跑完放掉 | 同上：`train` 跑著時跑 `detect`；`train` 跑完再跑一次 | `detect` 照跑；第二次 `train` 沒有跳過 |
| 拿鎖的程式被 kill -9 不會卡住 | 另一個行程拿著 `train` 的鎖睡 60 秒，`SIGKILL` 它 | 殺之前搶不到；殺之後 10 秒內搶得到（本機實際立刻） |
| 連不到資料庫 | DSN 指到不存在的埠 | 回失敗、印「沒跑：連不到 PostgreSQL」、子行程沒開 |
| 沒設 DSN | `AIOPS_PG_DSN` 空 | 不鎖，同名的鎖兩次都拿得到（本機開發照舊） |
| **真的兩個排程器** | 兩個 `python3 -m main scheduler` 同時跑同一份排程表（`alert` 每分鐘、每次睡 20 秒），連同一個 PostgreSQL，跑 2 分半 | 17:04、17:05 兩分鐘：B 每分鐘都真的跑（`ok 20.1s`），A 每分鐘都印「跳過」；**沒有一分鐘跑了兩份** |
| 沒弄壞別的 | 全部單元測試（含原本的排程器測試）、`tools/check-docs.py` | 全過 |

- **沒測到的**：helm 渲染（這個環境沒有 helm，`test_deploy.py` 跳過）；真的叢集上 RollingUpdate 換一次版、看新舊重疊時的 log；DBA 設了 `idle_session_timeout` 時，跑很久的工作（`train`）的鎖連線被踢掉，會讓同一個工作有機會跑兩份（未驗證，要問 DBA 有沒有設）。

## Act

| 沒解決的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 鎖只擋「同時」，不擋「同一分鐘跑兩次」 | 設計 beat 時 | 兩個 beat 重疊、或手動在同一分鐘多跑一次，會白跑一次（結果覆蓋，不會錯） | beat 維持 Recreate；要擋得記「這一分鐘跑過了」，目前不值得 |
| 卡死、OOM、機器維修的重啟空窗 | 盤點第 1 項 | 還是會漏幾分鐘 | 有了鎖，可以開 2 份排程器互相備援（同一個工作只跑一份）；要做再開工 |
| 單一工作失敗沒告警 | 盤點第 1 項 | 同上 | 另外做 |
