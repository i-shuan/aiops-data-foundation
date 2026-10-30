# 版本釘選成 ES 8.19.23、Grafana 13.0.7，補測剩下的故障情境（2026-10）

**狀態**：進行中　　**日期**：2026-10-30　　**相關**：分支 `claude/pin-es8-grafana13`；接續 [collector-es-exporter](2026-10-collector-es-exporter.md)、[compat-es8-grafana13](2026-10-compat-es8-grafana13.md)
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

（測試進行中，結果補在這裡。）

## Act：Check 後沒改善的、還沒解決的

（待補。）
