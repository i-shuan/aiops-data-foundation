# AIOps engine 改成一隻常駐 pod：排程器＋回饋網頁兩個 container（2026-10）

**狀態**：進行中　　**日期**：2026-10-31　　**相關**：分支 `claude/single-pod-scheduler`；前一版 [2026-10-schedule.md](2026-10-schedule.md)（10 個 CronJob）；結構 [restructure-components](../meta/2026-10-restructure-components.md)
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
- **範圔**：新增 `aiops-engine/code/scheduler/`（排程器，只用標準函式庫）、`aiops-engine/code/configs/schedule/schedule.yaml`（排程表，取代原本 chart values 的 `jobs`，跟其他設定一樣放進 image、chart 可用 `configOverrides` 蓋）；`aiops-engine/helmchart/` 的 CronJob 換成一個 Deployment；`e2e/k8s/run-schedule-e2e.sh` 改驗 pod 的 log。
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
| `aiops-engine/code/scheduler/`（新：`cron.py`、`runner.py`、`__main__.py`、`sleep.py`） | 排程器，只用標準函式庫。`cron.py`：5 欄 cron 的「這一分鐘該不該跑」（`*`、`*/n`、範圍、清單、週 0～7）。`runner.py`：`tick()` 每分鐘對每個工作比 cron，符合的各開一條執行緒跑 `python3 -m <args>` 子行程；上一條執行緒還活著就印「跳過：上一次還在跑」；`run_once()` 用 `subprocess.run(timeout)` 砍逾時、依 `retries` 重試；`serve()` 睡到下一個整分、更新 alive 檔、收到停止就不再排、等還在跑的。`__main__`：`--stage`、`--run <工作>`（手動跑一次）、`--list`；SIGTERM／SIGINT 設停止旗標。`sleep.py`：e2e 用的假工作（睡幾秒、指定 exit code） | 不用 APScheduler 之類的套件：要的只有「整分醒來、比 cron、開子行程」；子行程而不是 import 進來跑，一個工作 OOM 或卡死不會拖垮排程器，timeout 砍得乾淨。「錯過不補跑」是自然結果：只看醒來的那一分鐘 |
| `aiops-engine/code/configs/schedule/schedule.yaml`（新） | 排程表：`timezone`、每個工作的 `name`、`stage`、`cron`、`timeout`、`args`、`why`；`retries` 沒寫就照原規則（分鐘欄 `*` 給 0、否則 2）。內容就是原 chart values 的 `jobs` | 排程表跟其他設定一樣放進 image、chart 用 `configOverrides.schedule.schedule` 蓋；本機 `python3 -m scheduler` 也讀得到，不必在 chart 和程式兩邊各一份 |
| `aiops-engine/code/Dockerfile` | `COPY scheduler/` | — |
| `aiops-engine/helmchart/templates/deployment.yaml`（新；刪 `cronjobs.yaml`、`feedback-web.yaml`） | 一個 Deployment、`replicas: 1`、`Recreate`、兩個 container：`scheduler`（args `scheduler --config … --stage N --alive-file /tmp/scheduler-alive`，liveness 看 alive 檔 3 分鐘內有更新，limit 6Gi）、`feedback`（原 `feedback_web` 的 container，`/health` 探測，limit 512Mi）；兩個都掛 `configOverrides`；Service `aiops-feedback` 照舊 | 一隻 pod。兩個 container 而不是同一行程開執行緒：各有記憶體上限，`train` 撐爆 `scheduler` 時網頁不受影響；K8s 本來就分別重啟、探測 |
| `aiops-engine/helmchart/values.yaml` | 拿掉 `jobs`、`jobResources`、`stage2`；加 `scheduler.stage`、`scheduler.resources`、`terminationGracePeriodSeconds` | 排程表搬進 `code/configs/schedule/` |
| `aiops-engine/code/tests/test_scheduler.py`、`tests/_run_code.py`（新） | 13 個測試：cron（每分、每 5 分、每天、每週 0／7、清單範圍步進、壞的表達式）；runner 用 `tests._run_code` 跑小段程式：成功／失敗的 log 行、重試到成功、重試用盡放棄、逾時 1 秒內砍掉、上一次還在跑時跳過且別的工作照跑、不符合 cron 的不跑；排程表本身：stage 1 只有 4 個、全部 10 個、alert 的 timeout 夠跑 LLM、retries 照頻率、每個工作的 `--config` 檔都存在 | 原本 `test_deploy.py` 對 CronJob 驗的 Forbid／逾時／重試，現在是排程器的行為，要在排程器上驗 |
| `aiops-engine/code/tests/test_deploy.py` | 改驗 chart render 出一隻 pod 兩個 container、stage 參數、liveness 看 alive 檔、記憶體、`feedback.enabled=false` 拿掉 container 與 Service、`configOverrides` 兩個 container 都掛 | chart 變了 |
| `e2e/k8s/run-schedule-e2e.sh` | 全部改寫：紀錄來源從 Job 改成 pod log 的 `[scheduler]` 行。驗每分鐘都跑／沒漏／沒跳過／沒重複、心跳、alive 檔；手動 `--run train`；用 `configOverrides.schedule.schedule` 換一張排程表（`scheduler.sleep` 當假工作）驗不重疊、逾時、重試；壞 key → 失敗、修好重啟 → ok | 沒有 Job 可看了 |
| `aiops-engine/README.md`、`docs/architecture.md`、`docs/troubleshooting.md`、`docs/rca/README.md`、`docs/README.md`、`configs/baseline/README.md`、`reference.md`、`Chart.yaml` | CronJob 的描述改成一隻 pod 的排程器；troubleshooting 加三列（換 key 要重啟 pod、手動跑一次、看到「跳過」怎麼辦） | — |
| `README.md`（根目錄） | 目錄改成四個元件的兩欄表（使用者指定的格式）＋每個資料夾一列 | — |


## Check：測試的目的與結果

（實作後補）

## Act：Check 後沒改善的、還沒解決的

（實作後補）
