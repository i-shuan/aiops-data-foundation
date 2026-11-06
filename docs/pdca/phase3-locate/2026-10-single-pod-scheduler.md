# AIOps engine 改成一隻常駐 pod：排程器＋回饋網頁兩個 container（2026-10）

**狀態**：已完成（`aiops` namespace 那輪驗證沒跑完）　　**日期**：2026-10-31　　**相關**：分支 `claude/single-pod-scheduler`；前一版 [2026-10-schedule.md](2026-10-schedule.md)（10 個 CronJob）；結構 [restructure-components](../meta/2026-10-restructure-components.md)
**模組**：④⑤⑥⑧（排程）

## Plan：這一次要達到什麼

- **需求**：engine 的定期工作現在是 10 個 CronJob（第一階段開 4 個），每分鐘起一個短命的 pod 跑一個指令。要維運的東西多（10 個 CronJob、一堆 Completed 的 Job／pod），看紀錄要一個一個 `kubectl get job`；工作本身都很小，不需要各自一個 pod。
- **目的**：engine 變成**一隻常駐 pod**（Deployment、1 個副本），裡面兩個 container，同一個 image：`scheduler`（內建排程器，照頻率跑定期工作）、`feedback`（現有的 `feedback_web`，程式不改）。排程的行為跟 CronJob 一樣：
  1. 同一個指令不會兩份同時跑（上一次沒跑完就跳過這次）。
  2. 卡住會被砍（逾時）。
  3. 錯過的不補跑。
  4. 時區 Asia/Taipei。
  5. 每天、每週跑一次的，失敗在同一次內重試 2 次；每幾分鐘跑的不重試（下一輪就是重試）。
  6. 一個指令卡住或失敗，不影響別的指令（尤其 `train` 跑 30 分鐘時，`rules`、`detect`、`alert` 照常每分鐘跑，心跳不斷）。
  7. 每次執行在 pod 的 log 留一行：指令、開始、耗時、結果。
- **範圔**：新增 `aiops-engine/scheduler/`（排程器，只用標準函式庫）、`aiops-engine/configs/schedule/schedule.yaml`（排程表，取代原本 chart values 的 `jobs`，跟其他設定一樣放進 image、chart 可用 `configOverrides` 蓋）；`aiops-engine/helmchart/` 的 CronJob 換成一個 Deployment；`e2e/k8s/run-schedule-e2e.sh` 改驗 pod 的 log。
- **為什麼兩個 container、不是同一個行程開執行緒**：`train` 吃 4Gi、會被 OOM 殺，分開 container 各有自己的記憶體上限，排程器被殺時回饋網頁不受影響；K8s 本來就分別重啟、分別探測，不用再加 supervisord。
- **不做**：不把回饋後端搬進 Grafana（併進同一隻 pod 已達成少一個 Deployment 的目的；搬進 Grafana 要再裝 Infinity 外掛、改回饋的資料格式，另案評估）；不改 `detector`、`investigator`、`feedback_web` 的程式；不做多副本（多副本會重複跑）。

| 取捨 | 說明 |
|---|---|
| 記憶體合起來算 | `train` 要 4Gi，其他 1Gi；`scheduler` container 的 limit 要抓約 6Gi。request 設小（512Mi），平時不佔著，只有 `train` 時才用到 |
| 一掛全掛 | pod 被 OOM／驅逐，所有排程一起停，心跳也停；Grafana 的「AIOps 停了」會響（這本來就是它的用途） |
| 看紀錄 | 以前 `kubectl get job`；之後看這隻 pod 的 log（每次一行） |
| 更新有空檔 | 只能 1 個副本，換 image 時先停舊的（`Recreate`），中間缺幾十秒；錯過的不補跑。回饋網頁跟著一起重啟，之前它是獨立的 |
| 資源 | `scheduler` request 512Mi／limit 6Gi；`feedback` request 128Mi／limit 512Mi。Service `aiops-feedback:8080` 指向 `feedback` container，Grafana 表單的網址不變 |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `aiops-engine/scheduler/`（新：`cron.py`、`runner.py`、`__main__.py`、`sleep.py`） | 排程器，只用標準函式庫。`cron.py`：5 欄 cron 的「這一分鐘該不該跑」（`*`、`*/n`、範圍、清單、週 0～7）。`runner.py`：`tick()` 每分鐘對每個工作比 cron，符合的各開一條執行緒跑 `python3 -m <args>` 子行程；上一條執行緒還活著就印「跳過：上一次還在跑」；`run_once()` 用 `subprocess.run(timeout)` 砍逾時、依 `retries` 重試；`serve()` 睡到下一個整分、更新 alive 檔、收到停止就不再排、等還在跑的。`__main__`：`--stage`、`--run <工作>`（手動跑一次）、`--list`；SIGTERM／SIGINT 設停止旗標。`sleep.py`：e2e 用的假工作（睡幾秒、指定 exit code） | 不用 APScheduler 之類的套件：要的只有「整分醒來、比 cron、開子行程」；子行程而不是 import 進來跑，一個工作 OOM 或卡死不會拖垮排程器，timeout 砍得乾淨。「錯過不補跑」是自然結果：只看醒來的那一分鐘 |
| `aiops-engine/configs/schedule/schedule.yaml`（新） | 排程表：`timezone`、每個工作的 `name`、`stage`、`cron`、`timeout`、`args`、`why`；`retries` 沒寫就照原規則（分鐘欄 `*` 給 0、否則 2）。內容就是原 chart values 的 `jobs` | 排程表跟其他設定一樣放進 image、chart 用 `configOverrides.schedule.schedule` 蓋；本機 `python3 -m scheduler` 也讀得到，不必在 chart 和程式兩邊各一份 |
| `aiops-engine/Dockerfile` | `COPY scheduler/` | — |
| `aiops-engine/helmchart/templates/deployment.yaml`（新；刪 `cronjobs.yaml`、`feedback-web.yaml`） | 一個 Deployment、`replicas: 1`、`Recreate`、兩個 container：`scheduler`（args `scheduler --config … --stage N --alive-file /tmp/scheduler-alive`，liveness 看 alive 檔 3 分鐘內有更新，limit 6Gi）、`feedback`（原 `feedback_web` 的 container，`/health` 探測，limit 512Mi）；兩個都掛 `configOverrides`；Service `aiops-feedback` 照舊 | 一隻 pod。兩個 container 而不是同一行程開執行緒：各有記憶體上限，`train` 撐爆 `scheduler` 時網頁不受影響；K8s 本來就分別重啟、探測 |
| `aiops-engine/helmchart/values.yaml` | 拿掉 `jobs`、`jobResources`、`stage2`；加 `scheduler.stage`、`scheduler.resources`、`terminationGracePeriodSeconds` | 排程表搬進 `code/configs/schedule/` |
| `aiops-engine/tests/test_scheduler.py`、`tests/_run_code.py`（新） | 13 個測試：cron（每分、每 5 分、每天、每週 0／7、清單範圍步進、壞的表達式）；runner 用 `tests._run_code` 跑小段程式：成功／失敗的 log 行、重試到成功、重試用盡放棄、逾時 1 秒內砍掉、上一次還在跑時跳過且別的工作照跑、不符合 cron 的不跑；排程表本身：stage 1 只有 4 個、全部 10 個、alert 的 timeout 夠跑 LLM、retries 照頻率、每個工作的 `--config` 檔都存在 | 原本 `test_deploy.py` 對 CronJob 驗的 Forbid／逾時／重試，現在是排程器的行為，要在排程器上驗 |
| `aiops-engine/tests/test_deploy.py` | 改驗 chart render 出一隻 pod 兩個 container、stage 參數、liveness 看 alive 檔、記憶體、`feedback.enabled=false` 拿掉 container 與 Service、`configOverrides` 兩個 container 都掛 | chart 變了 |
| `e2e/k8s/run-schedule-e2e.sh` | 全部改寫：紀錄來源從 Job 改成 pod log 的 `[scheduler]` 行。驗每分鐘都跑／沒漏／沒跳過／沒重複、心跳、alive 檔；手動 `--run train`；用 `configOverrides.schedule.schedule` 換一張排程表（`scheduler.sleep` 當假工作）驗不重疊、逾時、重試；壞 key → 失敗、修好重啟 → ok | 沒有 Job 可看了 |
| `aiops-engine/README.md`、`docs/architecture.md`、`docs/troubleshooting.md`、`docs/rca/README.md`、`docs/README.md`、`configs/baseline/README.md`、`reference.md`、`Chart.yaml` | CronJob 的描述改成一隻 pod 的排程器；troubleshooting 加三列（換 key 要重啟 pod、手動跑一次、看到「跳過」怎麼辦） | — |
| `README.md`（根目錄） | 目錄改成四個元件的兩欄表（使用者指定的格式）＋每個資料夾一列 | — |


## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 排程器的邏輯對：cron 比對、不重疊、逾時砍、重試、排程表跟原設計一致 | `tests/test_scheduler.py`（13 個），子行程用 `tests._run_code` 跑小段程式，不連 ES | 全過：每分／每 5 分／每天／每週（0 和 7 都是星期日）／清單範圍步進的 cron 比對、壞的表達式報錯；成功與失敗的 log 行；重試第 3 次成功、用盡印「已重試 2 次，放棄」；逾時 1 秒在 10 秒內砍掉；上一次還在跑時印「跳過」且別的工作照跑；排程表 stage 1 只有 4 個、全部 10 個、alert 的 timeout ≥ 360 秒、retries 照頻率、每個工作的 `--config` 檔都存在 |
| chart render 出一隻 pod 兩個 container | `tests/test_deploy.py`（`helm template`） | 1 個 Deployment（replicas 1、Recreate）、2 個 container；`scheduler.stage` 參數傳到 args；liveness 看 `/tmp/scheduler-alive`；記憶體 6Gi／512Mi；`feedback.enabled=false` 拿掉 container 與 Service；`configOverrides` 兩個 container 都掛在同一個路徑 |
| 全部單元測試沒被弄壞 | `cd aiops-engine && python -m pytest -q` | 369 passed（原 352 ＋ 排程器 13 ＋ `ensure_index` 4） |
| **在 K8s 上真的一隻 pod 照排程跑** | Rancher Desktop，`run-schedule-e2e.sh`：build image 進 k8s.io、helm 裝、連 es-test，pod 的 log 是紀錄來源 | 見下面三輪 |
| 第一輪（12 分鐘觀察） | 同上 | detect、alert 每分鐘 11／11 ok、沒漏沒跳過沒重複；心跳每分鐘更新（12 次、最長間隔 60 秒）；rules 的觸發 15 次、ES 裡 15 筆；`train` 手動 `--run` 10 秒成功；**不重疊**（alert 換成睡 150 秒 → 後面兩分鐘「跳過：上一次還在跑」、ok 只有 1 次）、**逾時**（detect 睡 120 秒 timeout 50 → 每分鐘被砍）、**重試**（retries 2 的假工作同一分鐘失敗 3 次、印「已重試 2 次，放棄」）、對照的 rules 照跑；**壞 key**（401）三個工作都失敗、**修好重啟**後下一分鐘 ok。**但發現一個真問題**：rules 第一分鐘失敗 1 次（見 Act 最後一列） |
| 第二、三輪（修 `ensure_index` 與腳本後） | 同上，6 分鐘、4 分鐘 | 第二輪 rules 5／5 ok、第一分鐘不再失敗；第三輪**全部 22 項 PASS、0 FAIL**，含 liveness（alive 檔每分鐘更新）、不重疊、逾時、重試、壞 key 與修好 |
| namespace 改成 `aiops`（跟正式一樣）後 | 第四輪 | 腳本改了名字、pod 在 `aiops` 裝起來後就被中止，**沒跑完**；其他沒動 |

- 測試腳本自己的三個錯（都修了，不是產品的）：`--list` 的欄位位置算錯（cron 佔 5 欄）所以 timeout 讀成 `*`；`kubectl exec … cat /tmp/…` 在 Git Bash 被換成 Windows 路徑所以 alive 檔讀不到（改 `sh -c`）；輸出帶 `
` 數字比對不到。
- **沒測到的**：`train` 在 02:00 由排程器自己跑（只測了手動 `--run`；cron 比對有單元測試，K8s 上沒等到半夜）；pod 被 OOM 殺掉後 liveness 重啟的行為；`terminationGracePeriodSeconds` 內收到 SIGTERM 時等還在跑的工作收尾（程式有寫，沒在 K8s 上驗）；真的 LLM（`llmProvider` 還是 template）；`feedback` container 在 pod 裡（e2e 的 `values-test.yaml` 把它關掉了）。


## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 換 ES key 要重啟 pod | 設計時：環境變數是 pod 起來時讀的；CronJob 每分鐘新 pod 會重讀，常駐 pod 不會 | 換 `aiops-secrets` 後要 `kubectl rollout restart`，中間缺幾十秒；忘了重啟會一直 401（心跳停、Grafana 會報「AIOps 停了」） | 寫進 README、troubleshooting。要免重啟得改成每次子行程啟動前重讀 Secret 掛載的檔，這次不做 |
| 手動 `--run` 跟常駐的排程器互不知道 | 設計時 | 手動跑 `train` 時排程剛好也在跑 `train`，兩份同時寫 baseline（寫入是覆蓋，不會重複，只是浪費） | README 寫「自己確認沒在跑」；`train` 一天一次、手動跑多在剛部署時，實際撞到的機會小 |
| image 裡多一個測試用模組 `scheduler.sleep` | 寫 e2e 時：要一個「睡 N 秒、指定 exit code」的工作，image 沒有 `tests/` | 多 5 行程式；正式排程表不會用到 | 留著，docstring 註明只給測試 |
| cron 的「日」和「週」都有限制時是 AND，K8s CronJob 是 OR | 寫 `cron.py` 時 | 排程表沒有同時寫日和週的；要是有人寫了，行為跟 CronJob 不同 | `cron.py` docstring 註明 |
| 幾個工作同一分鐘跑，子行程的輸出在 pod log 裡會交錯 | 看 e2e 的 pod log | 每次執行結束那一行 `[scheduler] …` 一定完整；中間 detector、investigator 自己印的東西可能跟別的工作交錯 | 要分清就給每個工作加前綴（要攔子行程的 stdout 再轉印），這次不做 |
| 同一秒起跑的工作搶建同一個 index（`aiops-anomalies`） | 第一輪 e2e：第一分鐘 `rules` 失敗 1 次（`resource_already_exists_exception`）；CronJob 時代 pod 起得有先後、沒撞過 | 新裝的第一分鐘有一個工作失敗一次，之後正常 | **已修**：`core/es.py` 的 `ensure_index` 收到這個錯改成補 mapping；加 4 個單元測試；第二、三輪沒再發生 |

- **決定**：排程器放進 engine 的 image、同一隻 pod 兩個 container；排程表在 `configs/schedule/schedule.yaml`，chart 不再有 `jobs`。
- **下一輪**：第一階段 4 個工作在真實資料上跑 2～4 週（先只寫入不通知），看每天幾件、誤報率。

