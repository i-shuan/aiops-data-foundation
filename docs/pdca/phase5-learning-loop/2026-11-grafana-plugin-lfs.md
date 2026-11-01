# Grafana 回饋表單外掛 zip 放進 repo（git lfs），部署時從 repo 帶進 cluster（2026-11）

**狀態**：進行中　　**日期**：2026-11-01　　**相關**：分支 `claude/grafana-plugin-lfs`；前案 [grafana-plugin-init](2026-10-grafana-plugin-init.md)
**模組**：⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：回饋表單外掛（Business Forms `volkovlabs-form-panel` 6.3.1）的 zip 放在公司 Nexus，chart 的 initContainer 從 Nexus 下載。連不到 Nexus 的地方（本機測試機、部分環境）就裝不了外掛，回饋表單儀表板只能顯示「找不到面板」。使用者要 zip 用 git lfs 放進 repo，部署時用相對路徑從 repo 帶進去。
- **目的**：
  - zip 放在 `grafana/plugins/volkovlabs-form-panel-6.3.1.zip`，`.gitattributes` 讓 `grafana/plugins/*.zip` 走 git lfs；SHA-256 跟 `values.yaml` 的 `plugin.sha256` 一致。
  - 部署時從 repo 帶進去：ConfigMap、Secret、Helm release 都有 1MB 上限，10MB 的 zip 放不進 chart。新腳本 `grafana/helmchart/load-plugin.sh` 用相對路徑讀 repo 裡的 zip，切成每段不到 1MB、建成 ConfigMap `grafana-plugin-zip-<序號>`；chart 的 `plugin.source: configmap` 時 initContainer 把各段接回、核對 SHA-256、解開。Nexus 那條路（`plugin.source: url`，預設）不動。
- **範圍**：
  - 做：`.gitattributes`、zip（使用者提供，這台機器連不到 Nexus）、`load-plugin.sh`、chart（`plugin.source`、`plugin.parts`、`install-plugin.sh` 讀分段）、`grafana/README.md` 與 `INSTALL-form-plugin.md`；測試 cluster 上實裝。
  - 不做：把外掛做進 Grafana image；`run-rca-demo.sh` 預設開外掛（示範 Grafana 照舊不裝，要的話照新步驟加）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `.gitattributes`（新） | `grafana/plugins/*.zip` 走 git lfs | 10MB 的二進位檔不進一般 git 歷史 |
| `grafana/helmchart/load-plugin.sh`（新） | 預設讀腳本旁邊的 `../plugins/volkovlabs-form-panel-6.3.1.zip`（相對路徑）；是 git lfs 指標檔就停；算 SHA-256、跟 `values.yaml` 不同就提醒；`split` 成每段 900KB、建 `grafana-plugin-zip-<序號>`（標 `app.kubernetes.io/part-of=grafana-plugin-zip`），用 server-side apply；上次多出來的段刪掉；印出 `--set plugin.source=configmap --set plugin.parts=<段數>` | ConfigMap、Secret、Helm release 都有 1MB 上限；client-side `kubectl apply` 會把整份內容存進 annotation（上限 256KB），實測第一段就被擋 |
| `grafana/helmchart/values.yaml`、`templates/grafana.yaml` | `plugin.source`（`url` 預設／`configmap`）、`plugin.parts`；configmap 時 initContainer 不給 `PLUGIN_URL`、改給 `PLUGIN_PARTS_DIR=/plugin-zip`，掛 projected volume（各 ConfigMap 的 `part` 依序放成 `part-000`…）；沒給 `parts` 時 `helm template` 直接失敗並說明 | Nexus 那條路不動；段數由腳本決定，chart 只照著掛 |
| `grafana/helmchart/files/install-plugin.sh` | 有 `PLUGIN_PARTS_DIR` 就 `cat part-*` 接回 zip，不下載；之後核對 SHA-256、解開都跟原本一樣 | 同一支腳本兩種來源 |
| `grafana/README.md`、`grafana/code/INSTALL-form-plugin.md` | 「連不到 Nexus」的步驟 | 操作文件 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| chart 兩種模式渲染對、沒給段數會擋 | `helm template`：預設、`source=configmap parts=3`、`source=configmap` 不給 parts | 預設照舊帶 `PLUGIN_URL`；configmap 帶 `PLUGIN_PARTS_DIR`、projected volume 三段 `part-000…002`；不給 parts 失敗並印「要給 plugin.parts」 |
| 切段、帶進 cluster、接回、核對、解開 | 測試 cluster 另開 namespace `plugin-test`：2.5MB 假外掛 zip（`volkovlabs-form-panel/plugin.json`＋亂數）跑 `load-plugin.sh`，`helm upgrade --install` 帶它印的參數與假 zip 的 SHA-256 | 第一次 `kubectl apply` 被擋（annotation 超過 256KB），改 server-side apply 後：建 3 個 ConfigMap；initContainer「從 /plugin-zip 的 3 段接回 zip」「已裝 volkovlabs-form-panel」，Grafana 起來；測完刪掉 namespace |

## Act：Check 後沒改善的、還沒解決的

（進行中）
