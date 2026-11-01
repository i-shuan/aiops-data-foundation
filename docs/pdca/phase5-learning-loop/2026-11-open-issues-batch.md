# open-issues 可平行做的七項（2026-11）

**狀態**：進行中　　**日期**：2026-11-01　　**相關**：分支 `claude/parallel-items`；來源 [meta/2026-10-open-issues.md](../meta/2026-10-open-issues.md) 第 2、5、7、9.2、9.3 節
**模組**：④ 判斷、⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：open-issues 累積了一批「範圍明確、不用叢集、不碰 trace 合併分支正在改的檔案」的小項目。HA 測試佔著 es-test 叢集，這些項目只靠單元測試就能驗，先做掉。
- **目的**：下面七項各有程式或文件的改動，單元測試可驗；e2e 等叢集空了再補跑。
- **範圍**：

| # | 項目 | 來源 | 主要改的檔案 |
|---|---|---|---|
| 1 | 文件：重建 ES 要列 `train` 為標準步驟；`detect` 窗口 10 分鐘的限制 | 第 2、7 節 | `aiops-engine/README.md` |
| 2 | B6a：`service.name` 等於 container 名稱算缺 | 9.2 | `configs/baseline/quality.yaml`、`detector/quality.py` |
| 3 | `tune --show`：看某條序列、某個對象最終生效的值與來源 | 第 5 節「想補」 | `detector/cmd_baseline.py`、`detector/model.py`、`detector/__main__.py` |
| 4 | 每筆異常記是哪一版產生的（`engine_version`） | 9.2 | `detector/es.py`、`detector/cmd_baseline.py`、`detector/owner.py`、`helmchart/templates/env.yaml` |
| 5 | 基準線不見的告警：`detect` 寫 `detect-status`，Grafana 規則送平台 | 第 7 節 | `detector/cmd_baseline.py`、`grafana/code/alert-rules.yaml` |
| 6 | 漏報的入口：整合進 Grafana 回饋儀表板，不另做網頁（使用者決定） | 9.3 | `feedback_web/app.py`、`feedback_web/__main__.py`、`grafana/code/dashboards/aiops-feedback.json` |
| 7 | code-review 三個殘項：metrics 回饋跨段、回測重複查 ES、相似事故 ES 前 20 筆截斷 | 9.3 | `detector/cmd_metrics.py`、`detector/cmd_backtest.py`、`investigator/similar.py` |

  不做：`sample_per_bucket`、ES chart 的 setup Job、snapshot、根因分析限制、retention 文件、Collector、Pod 同儕 A2（原因見 open-issues 的「先別平行」）。不跑 es-test 叢集的 e2e。不合回 master。

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
