# aiops-engine：AIOps 分析核心（④～⑨）

> **類型：操作 How-To**　原理見 [docs/rca](../docs/rca/README.md)、[docs/feedback](../docs/feedback/README.md)；排程的設計紀錄 [pdca/phase3-locate/2026-10-schedule.md](../docs/pdca/phase3-locate/2026-10-schedule.md)

| 資料夾 | 內容 |
|---|---|
| `code/` | 一個 Python 專案：`core/`（共用：ES、trace、LLM）、`detector/`（動態基準線學習、規則、Pod 同儕比對、資料品質、回饋、回測、量化）、`investigator/`（事件整合、根因分析、相似事故、告警）、`feedback_web/`（老師傅的回饋網頁）、`configs/`（[baseline](code/configs/baseline/README.md)、[rca](code/configs/rca/README.md)）、`interfaces/`、`fake-data/`、`tests/`、`Dockerfile`（一個 image 裝全部，`ENTRYPOINT python3 -m`，`args` 給套件名和指令） |
| `helmchart/` | 每個指令一個 CronJob＋回饋網頁的 Deployment／Service。`values.yaml` 的 `jobs` 就是排程表（頻率、逾時、記憶體） |

本機跑：`cd aiops-engine/code && python3 -m detector <指令>`、`python3 -m investigator investigate`；單元測試 `python3 -m pytest -q`。

## 怎麼上

```bash
cd aiops-engine/code && docker build -t <nexus>/aiops-engine:0.1.0 . && docker push <nexus>/aiops-engine:0.1.0   # 內網：Dockerfile 的 pip 改 --index-url 指到 Nexus
kubectl create namespace aiops
kubectl -n aiops create secret generic aiops-secrets \
  --from-literal=ES_API_KEY=<elasticsearch-setup.sh 發的「AIOps baseline key」> \
  --from-literal=LLM_API_KEY=                          # 沒有模型就空的（llmProvider=template）
helm upgrade --install aiops aiops-engine/helmchart -n aiops \
  --set image.repository=<nexus>/aiops-engine --set image.tag=0.1.0 --set esUrl=https://<中央 ES>:9200
kubectl -n aiops get cronjob
```

- `rca.yaml` 的 `alerts.feedback_url`、`report_url` 填回饋網頁的網址（Service `aiops-feedback:8080`；要讓人從公司網路點得到再加 Ingress）。
- 設定檔要跟 image 裡的不一樣：`--set-file configOverrides.baseline.series=aiops-engine/code/configs/baseline/series.yaml`（掛在 `/app/configs/baseline/series.yaml`）。
- 本機測試：`e2e/k8s/run-schedule-e2e.sh`（`values-test.yaml`：連 es-test、image 用 nerdctl 建進 containerd、不裝回饋網頁）。

## 排程：第一階段只開 4 個

能跑通「告警 → 回饋 → 調門檻」這一圈的最少集合。偵測來源少，第一個月誤報來了容易知道是哪裡出的；四個在真實資料上跑穩、看過一輪誤報，再 `--set stage2=true` 開其他 6 個（建議順序：quality → peer → trace 三個 → chronic）。

| CronJob | 階段 | 頻率 | 指令 | 為什麼 |
|---|---|---|---|---|
| `aiops-rules` | 1 | 每 1 分鐘 | `detector rules` | 狀態變化（容器重啟、錯誤率 5%、p95 2 秒保底）要快；老師傅核准的規則也在這裡跑 |
| `aiops-detect` | 1 | 每 1 分鐘 | `detector detect --window 10m` | 每分鐘看最近 10 分鐘；同一筆重跑會覆蓋 |
| `aiops-alert` | 1 | 每 1 分鐘 | `investigator alert` | 合成一件、根因分析、相似事故、LLM 摘要、**心跳** |
| `aiops-train` | 1 | 每天 02:00 | `detector train` | 學過去 4 週（4Gi） |
| `aiops-quality` | 2 | 每 10 分鐘 | `detector quality` | 資料品質（第一個月人天天看，先不用它報） |
| `aiops-peer` | 2 | 每 5 分鐘 | `detector peer` | Pod 同儕橫向比對：多一種告警來源 |
| `aiops-trace-collect`、`aiops-trace-train`、`aiops-trace-detect` | 2 | 每 5 分鐘／每天 02:20／每 5 分鐘 | `detector trace-*` | trace 每一段的 baseline；trace 有取樣、只留 7 天，學出來本來就偏 |
| `aiops-chronic` | 2 | 每週一 08:00 | `detector chronic` | 慢性病清單，要幾週資料才有意義 |

共同點：`concurrencyPolicy: Forbid`（上一次沒跑完就跳過這次）、`startingDeadlineSeconds`（錯過不補跑）、`activeDeadlineSeconds`（卡住就砍）、每幾分鐘的 `backoffLimit 0`（下一輪就是重試）、每天／每週的 2（不然要等明天）。改頻率改 `helmchart/values.yaml` 的 `jobs`。

## 「AIOps 停了」怎麼知道

`investigator alert` 每次跑都往 `aiops-alerts` 寫一筆心跳（`_id: heartbeat`、`status: ok`，不是一件事）。Grafana 的規則 `aiops-heartbeat`（[grafana/code/alert-rules.yaml](../grafana/code/alert-rules.yaml)）：最近 10 分鐘沒有心跳就送平台。

## 先只寫入、不通知

上線前 2～4 週 Grafana 先不接 contact point（或全部送到沒人看的 webhook），讓它跑、累積 `aiops-alerts`、`aiops-feedback`。期間用 `python3 -m detector metrics --from … --to …` 記下基準（每天幾件、誤報率），再開通知。

## 驗證過的

- image 本機 docker build 成功；`investigator alert`、`detector metrics`、`feedback_web` 對著測試 ES 正常；容器內非 root（uid 10001）。
- 本機 K8s 連測試 ES 跑 30 分鐘（`e2e/k8s/run-schedule-e2e.sh`，[adjust-schedule-verify](../docs/pdca/phase2-anomaly/2026-10-adjust-schedule-verify.md)）：rules、detect、alert 每分鐘各 29 次都成功沒漏；train 依 `timeZone` 台北時間那一分鐘跑；Forbid、activeDeadlineSeconds、失敗重試、心跳每分鐘更新都如預期。
