# aiops-engine：AIOps 分析核心（④～⑨）

> **類型：操作 How-To**　原理見 [docs/rca](../docs/rca/README.md)、[docs/feedback](../docs/feedback/README.md)、[architecture.md §4.3 部署角色](../docs/architecture.md#43-部署角色apibeatworkersuperset-式)；設計紀錄 [single-pod-scheduler](../docs/pdca/phase3-locate/2026-10-single-pod-scheduler.md)（一隻 pod）、[api-beat-worker](../docs/pdca/meta/2026-11-api-beat-worker.md)（分散）

| 資料夾 | 內容 |
|---|---|
| `code/` | 一個 Python 專案：`core/`（共用：ES 連線、設定檔驗證、`Anomaly`、plugin 擴充點、LLM）、`detector/`（一個功能一個資料夾：`baseline/` 動態基準線學習、`rules/` 規則、`adjust/` 調整與回測、`feedback/` 回饋與量化、`prune.py`；Pod 同儕比對、資料品質、trace、慢性病的命令由 observability plugin 掛）、`investigator/`（`alerts/` 事件整合與嚴重度、`rca/` 根因分析、`similar/` 相似事故與匯入）、[`api/`](code/api/__init__.py)（沒有頁面的薄 API：ML／FDC 推異常、重跑根因、Grafana 表單的後端；備用回饋網頁）、`scheduler/`（排程器；`--enqueue` 是 beat）、[`tasks/`](code/tasks/__init__.py)（佇列：Celery＋Redis，worker）、[`sources/`](code/sources/README.md)（資料來源：ES 以外的檔案、HTTP、程式給的 DataFrame 也能餵）、`configs/`（[baseline](code/configs/baseline/README.md)、[rca](code/configs/rca/README.md)、[schedule](code/configs/schedule/schedule.yaml)）、`interfaces/`、`fake-data/`、`tests/`、`Dockerfile`（一個 image 裝全部，`ENTRYPOINT python3 -m`，`args` 給套件名和指令） |
| `helmchart/` | 一個 image、一份 values，`mode` 選：**`single`** 一隻 pod 兩個 container（`scheduler` 自己跑定期工作、`api`）；**`distributed`** Superset 式：`aiops-api`、`aiops-beat`（只丟工作）、`aiops-worker-realtime／alert／batch`、`aiops-redis`。Service `aiops-api:8080` |

本機跑：`cd aiops-engine/code && python3 -m detector <指令>`、`python3 -m investigator investigate`；單元測試 `python3 -m pytest -q`。

## 怎麼上

```bash
cd aiops-engine/code && docker build -t <nexus>/aiops-engine:0.1.0 . && docker push <nexus>/aiops-engine:0.1.0   # 內網：Dockerfile 的 pip 改 --index-url 指到 Nexus
kubectl create namespace aiops
kubectl -n aiops create secret generic aiops-secrets \
  --from-literal=ES_API_KEY=<elasticsearch/kibana-dev-tools.txt 第 3 段發的 engine key>
cp aiops-engine/helmchart/values-local.example.yaml aiops-engine/helmchart/values-local.yaml   # 填 LLM 的 provider、model、baseUrl、apiKey；不進 git
helm upgrade --install aiops aiops-engine/helmchart -n aiops -f aiops-engine/helmchart/values-local.yaml \
  --set image.repository=<nexus>/aiops-engine --set image.tag=0.1.0 --set esUrl=https://<中央 ES>:9200
kubectl -n aiops get pod                               # 一隻 aiops-engine-…，2/2
kubectl -n aiops logs deploy/aiops-engine -c scheduler -f | grep '\[scheduler\]'   # 每次執行一行：工作、排定分鐘、結果、秒數
```

- `rca.yaml` 的 `alerts.feedback_url`、`report_url` 填 api 的網址（Service `aiops-api:8080`；要讓人從公司網路點得到再加 Ingress）。
- 分散模式：`--set mode=distributed`（下一節）；ML／FDC 要推異常進來時再 `--set api.token=<隨機字串>`（寫在 `values-local.yaml`）。
- 情境（plugin）：預設 `observability`（IT）；工廠 `--set plugin=factory`，只有 ML／FDC 指標 `--set plugin=metrics_only`（[code/plugins/README.md](code/plugins/README.md)）。各裝一個 release、各自一隻 pod。
- 資料來源（observability 才看）：預設讀我們 Collector 寫的 OTel 格式。公司 ES 裡是 Elastic APM 的資料（`traces-apm-*`、`logs-apm.*`、`metrics-apm.*`）時 `--set source=apm`，設定檔與怎麼接見 [code/configs/apm/README.md](code/configs/apm/README.md)。
- LLM：`values.yaml` 的 `llm.provider`（`template` 不叫模型）、`llm.model`（空的用程式預設 openai `gpt-4o-mini`、anthropic `claude-sonnet-4-5`）、`llm.baseUrl`（內網閘道，不含 `/v1`）放 ConfigMap `aiops-env`；`llm.apiKey` 由 chart 建成 Secret `aiops-llm`。**key 只寫在 `values-local.yaml`**（`.gitignore` 擋掉），`values.yaml` 永遠留空。改了重跑 helm，pod 會自己重啟（`checksum/env`）。
- 設定檔要跟 image 裡的不一樣：`--set-file configOverrides.baseline.series=aiops-engine/code/configs/baseline/series.yaml`（掛在 `/app/configs/baseline/series.yaml`）；排程表是 `configOverrides.schedule.schedule`。
- **換 ES key 要重啟 pod**（`kubectl -n aiops rollout restart deploy/aiops-engine`）：環境變數是 pod 起來時讀的，不像以前每個 CronJob 每分鐘重讀。
- 手動跑一次某個工作（例如剛部署要先有 baseline）：`kubectl -n aiops exec deploy/aiops-engine -c scheduler -- python3 -m scheduler --config configs/schedule/schedule.yaml --run train`。

## 部署模式：一隻 pod，或 api／beat／worker

一個 image、一份 `values.yaml`，角色由啟動指令決定，`mode` 選怎麼裝（[pdca/meta/2026-11-api-beat-worker.md](../docs/pdca/meta/2026-11-api-beat-worker.md)）：

| | `mode: single`（預設） | `mode: distributed` |
|---|---|---|
| 裝出來 | 1 隻 pod：`scheduler`（排程器自己開子行程跑工作）＋`api` | `aiops-api`（N 份）、`aiops-beat`（1 份，每分鐘只把工作丟進佇列）、`aiops-worker-realtime`／`-alert`／`-batch`（各自 `replicas`、`resources`）、`aiops-redis` |
| 要 Redis | 不用 | 要（chart 裝一個，或 `redis.url` 指公司現有的） |
| 適合 | 開發、離線廠區、資料量小 | `train` 不能拖慢每分鐘的 `detect`、api 要跟排程分開掛、ML／FDC 要即時推異常 |
| 工作怎麼跑 | 排程器自己跑 | worker 從佇列拿，一樣是開子行程 `python3 -m <args>`；detector／investigator 一行不用改 |
| 「上一次沒跑完就跳過」 | 排程器看執行緒 | 工作帶 `expires`＝下一次排定前 5 秒，忙到過期的直接丟 |

```bash
helm upgrade --install aiops aiops-engine/helmchart -n aiops -f aiops-engine/helmchart/values-local.yaml --set mode=distributed \
  --set workers.realtime.replicas=2 --set api.corsOrigin=https://<Grafana 網址>
kubectl -n aiops get deploy      # aiops-api、aiops-beat、aiops-worker-realtime、aiops-worker-alert、aiops-worker-batch、aiops-redis
kubectl -n aiops logs deploy/aiops-beat -f | grep '\[beat\]'               # 每分鐘：工作 → 佇列
kubectl -n aiops logs deploy/aiops-worker-alert -f | grep '\[scheduler\]'  # 工作真的跑了：結果、秒數
```

佇列三個（`schedule.yaml` 每個工作的 `queue`）：`realtime` 每幾分鐘的（rules、detect、trace-merge、ingest、peer、trace-*、quality、paths-merge）、`alert` 收斂＋根因（**固定 1 份、concurrency 1**，chart 擋著：兩筆異常同時進來才不會各開一件）、`batch` 每天／每週的（train 4Gi、trace-train、chronic、prune、shift）。`alert` 跑太慢只會晚一點，不會重複開件。

## API（`python3 -m api`，Service `aiops-api`）

畫面全在 Grafana（[grafana/](../grafana/README.md)），api 只收請求。端點清單在 [code/api/__init__.py](code/api/__init__.py)：

| 端點 | 誰打 | 做什麼 |
|---|---|---|
| `POST /api/anomalies` `{"input": "ml", "rows": [...]}` | ML／FDC 系統 | 推判好的異常進 ⑤（跟 `detector ingest` 同一套對照，`configs/<plugin>/ingest.yaml` 的 `inputs`）；要 `Authorization: Bearer <AIOPS_API_TOKEN>`（`api.token`）；分散模式會立刻丟一個 `alert` 工作，幾秒內開件，不用等每分鐘的 `ingest` |
| `POST /api/rca` `{"alert_id": "..."}` | Grafana 回饋儀表板的「重跑根因分析」按鈕 | 在開著的那件上標 `rca_requested`，下一輪 `alert` 強制重跑根因（含相似事故、摘要）；分散模式立刻丟 `alert`。結束的件回 409 |
| `GET /api/alerts?status=open&size=50` | 別的系統（工單、入口網站） | 件的清單，不含報告全文 |
| `GET /api/alert`、`POST /api/feedback`、`GET /api/missed-form`、`POST /api/missed` | Grafana 的外掛表單（選用） | 預填、寫回饋（[docs/feedback](../docs/feedback/README.md)） |
| `GET /feedback?alert=…`（`POST /feedback`、`POST /rca`）、`GET /report?alert=…`、`GET /missed`（`POST /missed`） | Grafana 儀表板 `aiops-feedback` 的連結、通知裡的連結 | **預設的回饋入口**：回饋表單（開著的件最上面有「重跑根因分析」）、報告全文、回報漏報 |

`/api/*` 給瀏覽器打的要開 CORS：`api.corsOrigin=<Grafana 網址>`。

## 排程：第一階段只開 4 個

排程器（`python3 -m scheduler`）每分鐘醒來一次，照 [`configs/schedule/schedule.yaml`](code/configs/schedule/schedule.yaml) 把該跑的工作各開一個子行程（`python3 -m <args>`；分散模式加 `--enqueue` 就只丟進佇列）。行為跟原本的 K8s CronJob 一樣：同一個工作上一次沒跑完就跳過這次、超過 `timeout` 砍掉、錯過的分鐘不補跑、時區 Asia/Taipei、每天／每週的失敗在同一次內重試 2 次（每幾分鐘的不重試，下一輪就是重試）。一個工作卡住不影響別的（各自一條執行緒）。

能跑通「告警 → 回饋 → 調門檻」這一圈的最少集合是 4 個；偵測來源少，第一個月誤報來了容易知道是哪裡出的。四個在真實資料上跑穩、看過一輪誤報，再 `--set scheduler.stage=2` 開其他 6 個（建議順序：quality → peer → trace 三個 → chronic）。

| 工作 | 階段 | 頻率 | timeout | 指令 | 為什麼 |
|---|---|---|---|---|---|
| `rules` | 1 | 每 1 分鐘 | 50 秒 | `detector rules` | 狀態變化（容器重啟、錯誤率 5%、p95 2 秒保底）要快；老師傅核准的規則也在這裡跑 |
| `detect` | 1 | 每 1 分鐘 | 50 秒 | `detector detect --window 10m` | 每分鐘看最近 10 分鐘；同一筆重跑會覆蓋。**窗口就是上限**：排程器停超過 10 分鐘的那段不補判，Collector 補送的舊資料也不判、只進隔天的 `train` |
| `alert` | 1 | 每 1 分鐘 | 600 秒 | `investigator alert` | 合成一件、根因分析、相似事故、LLM 摘要、**心跳**。一件新的含 LLM 最多 60 秒，一輪可能好幾件 |
| `train` | 1 | 每天 02:00 | 3600 秒 | `detector train` | 學過去 4 週（4Gi） |
| `quality` | 2 | 每 10 分鐘 | 500 秒 | `detector quality` | 資料品質（第一個月人天天看，先不用它報） |
| `peer` | 2 | 每 5 分鐘 | 240 秒 | `detector peer` | Pod 同儕橫向比對：多一種告警來源 |
| `trace-collect`、`trace-train`、`trace-detect` | 2 | 每 5 分鐘／每天 02:20／每 5 分鐘 | 240／3600／240 秒 | `detector trace-*` | trace 每一段的 baseline；從 raw（100% 收、2 天）抽樣 |
| `chronic` | 2 | 每週一 08:00 | 1800 秒 | `detector chronic` | 慢性病清單，要幾週資料才有意義 |
| `prune` | 2 | 每天 03:30 | 600 秒 | `detector prune` | `aiops-anomalies`、`aiops-trace-segments`、已結案的 `aiops-alerts` 超過保留天數的每天清一次（[prune.yaml](code/configs/baseline/prune.yaml)；這三個是一般索引、不能用 ILM）。人工判斷的索引不清、只備份（[data-storage-reference](../docs/data-storage-reference.md)） |
| `shift` | 2 | 每天 04:00 | 300 秒 | `detector shift` | 一階跳變（換版後延遲停在新水準、每點都報）→ 提 `reset_point` 建議（proposed，人核准才生效）；ES 的 `change_point` 聚合要付費授權，這裡看 `aiops-anomalies` 裡已判出的點 |

改頻率、逾時改 `schedule.yaml` 再 build image（或用 `configOverrides.schedule.schedule` 蓋）。

**資源與故障範圍（single）**：`scheduler` container limit 6Gi（`train` 要 4Gi，同時每分鐘的三個還在跑；平時用不到），`api` 512Mi，各自獨立。pod 只能 1 個副本（多一個每個工作就跑兩份）；換 image 先停舊的（`Recreate`），中間缺幾十秒、錯過的分鐘不補。pod 被 OOM／驅逐時所有排程一起停，心跳也停——下面那條告警就是抓這個。排程器（beat 也是）每分鐘更新 `/tmp/scheduler-alive`，超過 3 分鐘沒更新 liveness probe 會重啟 container。分散模式下 `train` 在 `batch` worker（6Gi）、每分鐘的在 `realtime` worker，互不影響。

## 重建 ES、換 ES 之後

`aiops-baseline` 是 `train` 每天重算的衍生資料，但 ES 重建（或只有這個索引掉了）到隔天 02:00 之間，`detect` 讀到空的 baseline 會印一行「沒有 baseline，先跑 train」就跳過那條序列，不報錯；動態基準線這一路靜默停擺，規則、Pod 同儕橫向比對、資料品質不受影響。所以重建 ES 的標準步驟是：

1. 重貼 [elasticsearch/kibana-dev-tools.txt](../elasticsearch/kibana-dev-tools.txt)（ILM、template、key）。
2. 等 Collector 送進至少 2 週的 metrics（不然 `train` 學不出 `dow_hour` 這一層，見 [docs/rca](../docs/rca/README.md)）；要立刻有 baseline 就先接受只有 `hour` 或 `all` 層。
3. **手動跑一次 `train`**（上面的 `--run train`），不要等隔天 02:00。
4. `aiops-incidents`、`aiops-feedback`、`aiops-tuning` 是人工判斷，重算不回來；有 snapshot 就先還原這三個（備份本身還沒做，見 [open-issues 第 7 節](../docs/pdca/meta/2026-10-open-issues.md)）。

## 「AIOps 停了」怎麼知道

`investigator alert` 每次跑都往 `aiops-alerts` 寫一筆心跳（`_id: heartbeat`、`status: ok`，不是一件事）。Grafana 的規則 `aiops-heartbeat`（[grafana/helmchart/files/alert-rules.yaml](../grafana/helmchart/files/alert-rules.yaml)）：最近 10 分鐘沒有心跳就送平台。

基準線不見時排程沒停、心跳照寫，上面那條不響。`detector detect` 每輪另寫一筆 `detect-status`（`_id: detect-status`、`status: ok`、`missing_baseline`＝這輪沒有 baseline 的序列數、`series`＝它們的名字），規則 `aiops-baseline-missing` 查 `missing_baseline > 0` 持續 5 分鐘就送平台；處置就是上一節的手動 `train`。

## 先只寫入、不通知

上線前 2～4 週 Grafana 先不接 contact point（或全部送到沒人看的 webhook），讓它跑、累積 `aiops-alerts`、`aiops-feedback`。期間用 `python3 -m detector metrics --from … --to …` 記下基準（每天幾件、誤報率），再開通知。

## 驗證過的

- image 本機 docker build 成功；`investigator alert`、`detector metrics`、`api` 對著測試 ES 正常；容器內非 root（uid 10001）。
- 排程器的單元測試（`tests/test_scheduler.py`）：cron 比對、不重疊、逾時砍掉、重試、排程表跟設計一致；佇列（`tests/test_tasks.py`）：工作進哪個佇列、過期時間、beat 只丟不跑；chart 兩種 mode（`tests/test_deploy.py`）。
- 分散模式對測試 ES＋Redis 實跑（`python -X utf8 -m tests.distributed_e2e`）：beat 丟、worker 跑、佇列不堆積、`POST /api/anomalies` 幾秒內開件、`POST /api/rca` 重跑根因——結果在 [pdca/meta/2026-11-api-beat-worker.md](../docs/pdca/meta/2026-11-api-beat-worker.md)。**沒真的用 helm 裝進 K8s**。
- 本機 K8s 連測試 ES 的實跑見 [pdca/phase3-locate/2026-10-single-pod-scheduler.md](../docs/pdca/phase3-locate/2026-10-single-pod-scheduler.md)（CronJob 版本的 30 分鐘實跑在 [adjust-schedule-verify](../docs/pdca/phase2-anomaly/2026-10-adjust-schedule-verify.md)）。
