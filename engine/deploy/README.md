# 排程上線：K8s CronJob、回饋網頁、「AIOps 停了」

[docs/pdca/phase3-locate/2026-10-schedule.md](../../docs/pdca/phase3-locate/2026-10-schedule.md)。engine 的每個指令現在都是手動打；這個資料夾讓它在 K8s 上照頻率跑。

| 檔案 | 內容 |
|---|---|
| [`../Dockerfile`](../Dockerfile) | 一個 image 裝 `detector`、`investigator`、`feedback_web` 和 `configs/`；`ENTRYPOINT python3 -m`，`args` 給套件名和指令 |
| [`kustomization.yaml`](kustomization.yaml) | namespace `aiops`、環境變數（`ES_URL`、`LLM_PROVIDER`、`TZ`）、image 路徑；設定檔要蓋掉 image 裡的就打開 `aiops-configs` |
| [`cronjobs.yaml`](cronjobs.yaml) | **第一階段 4 個**：rules、detect、alert、train（下表）。由 [`gen-cronjobs.py`](gen-cronjobs.py) 產生，改頻率改那裡 |
| [`cronjobs-stage2.yaml`](cronjobs-stage2.yaml) | 之後再開的 6 個：trace-collect／trace-train／trace-detect、peer、quality、chronic；`kustomization.yaml` 裡預設註解掉 |
| [`feedback-web.yaml`](feedback-web.yaml) | 老師傅回饋入口（`feedback_web`）的 Deployment＋Service `aiops-feedback:8080` |

## 怎麼上

```bash
cd engine
docker build -t <nexus>/aiops-engine:0.1.0 .          # 內網：Dockerfile 的 pip 改 --index-url 指到 Nexus
docker push <nexus>/aiops-engine:0.1.0
kubectl create namespace aiops
kubectl -n aiops create secret generic aiops-secrets \
  --from-literal=ES_API_KEY=<elasticsearch-setup.sh 發的「AIOps baseline key」> \
  --from-literal=LLM_API_KEY=                          # 沒有模型就空的（LLM_PROVIDER=template）
# 改 kustomization.yaml 的 ES_URL、images；rca.yaml 的 alerts.feedback_url 填 feedback 的網址
kubectl apply -k deploy/
kubectl -n aiops get cronjob
```

**第一階段只開 4 個**：能跑通「告警 → 回饋 → 調門檻」這一圈的最少集合。偵測來源少，第一個月誤報來了容易知道是哪裡出的；四個在真實資料上跑穩、看過一輪誤報，再把 stage2 的一個一個打開（建議順序：quality → peer → trace 三個 → chronic）。

| CronJob | 階段 | 頻率 | 指令 | 為什麼 |
|---|---|---|---|---|
| `aiops-rules` | 1 | 每 1 分鐘 | `detector rules` | 狀態變化（容器重啟、錯誤率 5%、p95 2 秒保底）要快；老師傅核准的規則也在這裡跑 |
| `aiops-detect` | 1 | 每 1 分鐘 | `detector detect --window 10m` | 每分鐘看最近 10 分鐘；同一筆重跑會覆蓋 |
| `aiops-alert` | 1 | 每 1 分鐘 | `investigator alert` | 合成一件、根因分析、相似事故、LLM 摘要、**心跳** |
| `aiops-train` | 1 | 每天 02:00 | `detector train` | 學過去 4 週 |
| `aiops-quality` | 2 | 每 10 分鐘 | `detector quality` | 資料品質（第一個月人天天看，先不用它報） |
| `aiops-peer` | 2 | 每 5 分鐘 | `detector peer` | 跟兄弟比：多一種告警來源 |
| `aiops-trace-collect`、`aiops-trace-train`、`aiops-trace-detect` | 2 | 每 5 分鐘／每天 02:20／每 5 分鐘 | `detector trace-*` | trace 每一段的 baseline；trace 有取樣、只留 7 天，學出來本來就偏 |
| `aiops-chronic` | 2 | 每週一 08:00 | `detector chronic` | 慢性病清單，要幾週資料才有意義 |

共同點：`concurrencyPolicy: Forbid`（上一次沒跑完就跳過這次，不會兩份同時寫）、`startingDeadlineSeconds`（錯過就不補跑）、`activeDeadlineSeconds`（卡住就砍）、`backoffLimit`（每幾分鐘的 0：下一輪就是重試；每天、每週的 2：不然要等明天）、留最近 1 次成功、3 次失敗的 Pod 看 log。

## 「AIOps 停了」怎麼知道

`investigator alert` 每次跑都往 `aiops-alerts` 寫一筆心跳（`_id: heartbeat`、`status: ok`，不是一件事）。Grafana 多一條規則 `aiops-heartbeat`（[configs/grafana/alert-rules.yaml](../configs/grafana/alert-rules.yaml)）：10 分鐘內沒有心跳 → 送平台。沒有這條，排程停了 Grafana 會以為所有件都恢復。

## 先只寫入、不通知

上線前 2～4 週 Grafana 先不接 contact point（或全部送到沒人看的 webhook），讓它跑、累積 `aiops-alerts`、`aiops-feedback`。期間用 `python3 -m detector metrics --from … --to …` 記下基準（每天幾件、誤報率），再開通知。

## 驗證過的

- 2026-10-25：image 在本機 docker build 成功（pip 要經公司 proxy 時，`--cert` 給 CA）；`investigator alert`、`detector metrics`、`feedback_web` 對著測試 ES 正常；容器內非 root（uid 10001）。
- 2026-10-28，本機 K8s（k3s v1.30）連測試 ES 跑 30 分鐘（[e2e/local-k3s/run-schedule-e2e.sh](../../e2e/local-k3s/run-schedule-e2e.sh)，結果見 [adjust-schedule-verify](../../docs/pdca/phase2-anomaly/2026-10-adjust-schedule-verify.md)）：

| 項目 | 結果 |
|---|---|
| rules、detect、alert 每分鐘 | 各 29 次都成功、沒漏；最久 8、10、7 秒 |
| train | 手動 21 秒；排程依 `timeZone` 台北時間那一分鐘跑 |
| `Forbid` | alert 卡 150 秒時後面兩分鐘不開新的；同時最多 1 個 |
| `activeDeadlineSeconds` | detect 卡住 50 秒被砍（`DeadlineExceeded`） |
| 失敗 | key 壞掉時 3 個都 Failed、修好下一分鐘成功；train 連不到 ES 跑 3 次 |
| 心跳 | 每分鐘更新，最長間隔 60 秒 |

本機測試用 overlay [e2e/local-k3s/engine-schedule/](../../e2e/local-k3s/engine-schedule/kustomization.yaml)：只換 namespace、`ES_URL`、image，image 用 `nerdctl -n k8s.io build` 直接進 containerd。
