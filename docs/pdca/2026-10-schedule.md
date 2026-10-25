# 排程上線：定期跑、停了要知道、先只寫入不通知（2026-10）

**狀態**：程式與設定完成，K8s 上還沒跑過　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-complete`；主線 [product-loop](2026-10-product-loop.md) 順序 1 之後的「上線」
**模組**：④⑤⑥（排程）、⑧（Grafana 多一條告警）

## Plan：這一次要達到什麼

- **需求**：rules、detect、quality、peer、alert、train、chronic 現在都是手動打指令。沒有排程，正式環境跑不起來；`investigator alert` 停了，Grafana 會以為所有件都恢復（心跳沒了），沒有人會知道。
- **目的**：
  1. 一份 K8s 的 CronJob 設定（跟 Collector 一樣用 kustomize），每個指令照它該跑的頻率跑；同一個指令不會兩份同時跑。
  2. Grafana 多一條「AIOps 停了」的告警：`aiops-alerts` 10 分鐘沒有任何寫入就送平台。
  3. 正式環境**先只寫入、不開通知**跑 2～4 週，記下基準（[量化](2026-10-metrics-kpi.md) 的 `metrics` 指令算）。
- **範圍**：engine 的 Dockerfile、CronJob、ConfigMap／Secret 的接法、Grafana 規則。不做：自動擴充、多副本。

| 指令 | 頻率 | 為什麼 |
|---|---|---|
| `detector rules` | 每 1 分鐘 | 狀態變化要快 |
| `detector detect` | 每 1 分鐘（`--window 10m`） | 30 秒一點的序列，每分鐘看最近 10 分鐘；同一筆重跑會覆蓋 |
| `detector peer` | 每 5 分鐘 | 跟兄弟比看最近 10 分鐘 |
| `detector quality` | 每 10 分鐘 | 資料品質 |
| `investigator alert` | 每 1 分鐘 | 合成一件、心跳 |
| `detector train`、`trace-train` | 每天 02:00 | 學過去 4 週 |
| `detector trace-collect`、`trace-detect` | 每 5 分鐘 | trace 每一段 |
| `detector chronic` | 每週一 08:00 | 慢性病清單 |

`concurrencyPolicy: Forbid`：上一次還沒跑完就跳過這次，不會兩份同時寫。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/Dockerfile`（新） | `python:3.13-slim`，裝 `requirements.txt`，放進 `core/`、`detector/`、`investigator/`、`feedback_web/`、`configs/`、`interfaces/`、`fake-data/`；`ENTRYPOINT python3 -m`；非 root（uid 10001） | 一個 image 跑全部指令，CronJob 的 `args` 只差套件名和指令 |
| `engine/deploy/kustomization.yaml`、`namespace.yaml`（新） | namespace `aiops`；ConfigMap `aiops-env`（`ES_URL`、`LLM_PROVIDER`、`TZ`）；Secret `aiops-secrets` 手動建（`ES_API_KEY`、`LLM_API_KEY`）；image 路徑；設定檔要蓋掉 image 裡的就打開 `aiops-configs` | 跟 Collector 一樣用 kustomize；key 不進 git |
| `engine/deploy/cronjobs.yaml`、`gen-cronjobs.py`（新） | 10 個 CronJob 照 Plan 的表：rules／detect／alert 每 1 分鐘、peer／trace-collect／trace-detect 每 5 分鐘、quality 每 10 分鐘、train／trace-train 每天 02:00／02:20、chronic 週一 08:00；`concurrencyPolicy: Forbid`、`startingDeadlineSeconds`、`activeDeadlineSeconds`、`backoffLimit: 0`、`timeZone: Asia/Taipei`；用腳本產生，10 個長得一樣 | 同一個指令不會兩份同時寫；卡住會被砍；錯過不補跑 |
| `engine/deploy/feedback-web.yaml`（新） | `feedback_web` 的 Deployment＋Service `aiops-feedback:8080`，`/health` 當 liveness／readiness | 回饋入口（[expert-feedback](2026-10-expert-feedback.md)）要有地方跑 |
| `engine/investigator/cmd_alert.py` | 每次跑都寫一筆心跳到 `aiops-alerts`（`_id: heartbeat`、`category: heartbeat`、`status: ok`、沒有異常） | Grafana 查 `status:open` 不會抓到它；但「10 分鐘沒寫入」就查得出來 |
| `engine/configs/grafana/alert-rules.yaml` | 第二條規則 `aiops-heartbeat`：最近 10 分鐘 `alert_id:heartbeat` 的筆數 < 1 → 告警，`noDataState: Alerting`、`for: 2m`、labels 沒有 dept（走 policies 的預設 receiver platform） | 「AIOps 停了」要有人知道，不然所有件都像恢復了 |
| `engine/detector/cmd_metrics.py` | 量化把 `category` 不是 `service` 的（心跳、資料品質）排除 | 心跳不是一件事，不能算進件數 |
| `engine/deploy/README.md`、`configs/grafana/README.md` | 怎麼上、表、「先只寫入不通知」怎麼做 | |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| image 建得起來、跑得動 | 本機 `docker build`（pip 經公司 proxy 要 `--cert` 給 CA）；用 image 對著測試 ES 跑 `investigator alert --at …`、`detector metrics`、`feedback_web`（curl `/health`、`/feedback?alert=…`） | 建成 391 MB；三個都正常（alert 印出「進行中 0 件」、metrics 印出表、網頁 200）；容器內非 root |
| 心跳有寫、不會被當成一件事 | `loop_e2e`：跑一次 alert 後查 `aiops-alerts/_doc/heartbeat`；`metrics` 的件數 | 有（`status: ok`）；件數 6 件不含心跳 |
| yaml 都是合法的 | `yaml.safe_load` 讀 4 個 deploy yaml、Grafana rules | 都能讀，Grafana rules 兩條 uid `aiops-incident`、`aiops-heartbeat` |
| 產生器跟檔案一致 | 跑 `gen-cronjobs.py` 再 diff | 一樣 |

## Act：Check 後沒改善的、還沒解決的

- **沒有在 K8s 上跑過**（在什麼情況下發現：這台沒有 cluster，只有 docker）。CronJob 真的每分鐘跑、`Forbid` 真的擋住重疊、`activeDeadlineSeconds` 真的砍、ConfigMap 蓋設定檔那段註解打開後對不對，都要到有 k3s 的環境驗（`e2e/local-k3s` 可以接著做）。
- **Grafana 的心跳規則沒實測**（上一輪實測 Grafana 12.2 只有第一條）：`noDataState: Alerting` 配 `date_histogram` 在沒有任何文件時 Grafana 回的是 NoData 還是 0，兩種寫法都會響，但 `for: 2m` 的行為要看一次。
- **detect 每分鐘 `--window 10m`** 是估的：30 秒一點的序列每分鐘重算最近 20 點，量大時一分鐘跑不完就會被 `Forbid` 跳過；跑過一週看 Job 的時間再調。
- **先只寫入不通知**是在 Grafana 那邊做（不接 contact point），程式沒有開關；2～4 週後記得開。
