# Grafana：用 initContainer 裝回饋表單外掛

給 Grafana 跑在 K8s 上的情況。步驟、要改的地方、出問題怎麼看，在 [engine/configs/grafana/INSTALL-form-plugin.md](../../configs/grafana/INSTALL-form-plugin.md)。

| 檔案 | 用途 |
|---|---|
| [install-plugin.sh](install-plugin.sh) | initContainer 跑的腳本：從 Nexus 下載外掛 zip → 核對 SHA-256（不符就失敗）→ 解開到外掛資料夾；同一個 volume 已經裝過同版本就跳過 |
| [plugin-init-patch.yaml](plugin-init-patch.yaml) | 加進公司現有 Grafana Deployment 的部分 |
| [grafana-example.yaml](grafana-example.yaml) | 完整範例：Grafana＋外掛＋回饋表單儀表板 |

驗證紀錄：[docs/pdca/2026-10-grafana-plugin-init.md](../../../docs/pdca/2026-10-grafana-plugin-init.md)。
