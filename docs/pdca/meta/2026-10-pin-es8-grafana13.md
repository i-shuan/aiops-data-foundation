# 版本釘選成 ES 8.19.23、Grafana 13.0.7，補測剩下的故障情境（2026-10）

**狀態**：部分完成（版本釘選完成；補測只做完一部分，見 Act）　　**日期**：2026-10-30　　**相關**：分支 `claude/pin-es8-grafana13`；接續 [collector-es-exporter](2026-10-collector-es-exporter.md)、[compat-es8-grafana13](2026-10-compat-es8-grafana13.md)
**模組**：① 收值、③ 存、⑧ 出口

## Plan：這一次要達到什麼

- **需求**：公司的 ES 是 8.19.23、Grafana 是 13.0.7。上一輪只改了 Collector 出口，repo 裡的 manifest 和文件還寫 ES 9.5.5、Grafana 12.2；而且 ES 429、Collector 重啟或當掉、多節點、完整故障測試、Grafana 13 實際通知都還沒測。
- **目的**：repo 的測試環境與文件都對得上公司的版本；上面五項各有實測結果，沒過的要列出原因，不是寫「應該可以」。
- **範圍**：做——改 manifest 的 image 版本、改文件的版本說法、補測五項。不做——歷史 PDCA 紀錄裡的「ES 9.5.5」不回頭改（那是當時實際用的版本）；Grafana 外掛不升版（仍用驗過的 6.3.1）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `e2e/local-k3s/elasticsearch.yaml`、`elasticsearch-2node.yaml` | ES image 9.5.5 → 8.19.23 | 測試環境要跟公司版本一致 |
| `e2e/local-k3s/rca-demo/grafana.yaml`、`engine/deploy/grafana/*.yaml` | Grafana image 12.2.0 → 13.0.7 | 同上；外掛仍是 6.3.1 |
| `engine/configs/grafana/README.md`、`INSTALL-form-plugin.md`、`docs/architecture-reference.md`、`e2e/pipeline/run-alert-flap.py` | 版本說法：12.2、13.0.7 都實測過 | 原本寫「12.2 只能用 6.3.1」，已經不是全部事實 |
| `docs/metrics/how-to.md`、`docs/trace/how-to.md`、`docs/metrics/README.md` | ES 檢查步驟由 `/_otlp` 入口改成檢查內建 index template | Collector 出口已改，8.19 沒有 `/_otlp`，舊步驟會誤導 |

## Check：測試的目的與結果

環境：k3d 單節點，ES 8.19.23 在主機 docker，Collector 0.162.0（出口 `elasticsearch`），每個情境 log 200 行/秒、trace 20 個/秒。腳本：`e2e/local-k3d/run-durability.sh`（沿用 `e2e/local-k3s/load/run-durability.sh`）、`run-grafana-notify.sh`。

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| Grafana 13.0.7 用 repo 的規則、聯絡點、政策真的寄得出通知 | `run-grafana-notify.sh`：往 `aiops-alerts` 寫心跳與三件 open 事件（D3120、D1110、沒有 dept），看假 webhook 收到什麼 | 心跳不在 → 平台收到 firing；補心跳 → resolved。三件各送 it-oncall、imc-oncall、平台，一件一則共 3 則；改 resolved 後各收到 1 則 resolved |
| node Collector 當掉能自己接回，資料不掉 | `node-kill`（kill -9）、`node-delete`（強制刪 Pod） | log、trace 都 0 遺失；metrics 各缺 1 點（kill 當下 61 秒空檔） |
| trace-sampler 當掉、換 Pod、停著時 node Collector 重啟 | `sampler-kill`、`sampler-restart`、`sampler-down-node-restart` | restart 0 遺失；kill -9 掉 51 個 trace（當掉前 5 秒內，對照 k3s 基準 57 個，一致） |
| 三種 Collector 換版再換回不掉資料 | `rolling`（0.162.0 → 0.161.0 → 0.162.0） | log、trace 0 遺失；metrics 一個 44 秒空檔 |
| 模擬 drain、節點磁碟佇列目錄被刪 | `drain`、`hostpath-rm` | drain 0 遺失；刪佇列目錄掉 6,450 筆 log（Collector 停 30 秒期間，檢查點一起沒了），符合先前基準的預期 |
| node Collector 記憶體吃緊 | `node-oom`（上限 150Mi、5,000 行/秒） | **沒有真的 OOM**（OOMKilled 0 次），只證明 150Mi 在這個流量下夠用，不算測過 OOM |

**沒測到或沒跑完的**：見 Act。

## Act：Check 後沒改善的、還沒解決的

- **ES 429、ES 停機 600 秒、trace-sampler PVC 塞滿**：在 k3d 上啟動測試時被中斷，沒有結果。發現於：補測剩餘情境時。
- **多節點**：`add-nodes.sh` 與 `node-lost` 情境已寫好，但沒執行過，不能說多節點沒問題。
- **OOM**：要把上限降到 80Mi 左右、流量調高，才能真的觸發 OOM 再看佇列接回。
- **Collector 使用 elasticsearch 寫入元件後，ES `docker stop` 約掉 3%**：見 [collector-es-exporter](2026-10-collector-es-exporter.md)，仍是已知限制。
- **外掛仍是 6.3.1**：Grafana 13.0.7 實測可用；6.3.2 以後未驗，也沒重算 SHA-256。
- **歷史 PDCA 與「實測」段落**仍寫 ES 9.5.5、Grafana 12.2，那是當時實際用的版本，沒有改。
