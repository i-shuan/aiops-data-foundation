# requirements

每個需求一個資料夾，用**英文 kebab-case** 命名。流程定義在
[`.agents/shared/workflow.md`](../.agents/shared/workflow.md)。

```bash
cp -r requirements/_template requirements/add-export-to-excel
```

## 目前有哪些

三種資料（數值、log、trace）全進 Elasticsearch。總綱一份，底下每一步一個資料夾。

| 資料夾 | 內容 | 狀態 |
| --- | --- | --- |
| [`elastic-unified-observability/`](elastic-unified-observability/1-intent.md) | 新方向的總綱：監控程式與 server 的健康度，一套收集程式、全進 Elasticsearch、AIOps 自己訓練，目標是找根因 | 方向已確認 |
| [`health-metrics-to-elastic/`](health-metrics-to-elastic/1-intent.md) | 新方向第 1 步：範例程式，健康度數值經 OpenTelemetry Collector 進 Elasticsearch | 完成，本機 k3d 實測 |
| [`logs-to-elastic/`](logs-to-elastic/1-intent.md) | 新方向第 2 步：每個容器的 log 經同一套收集程式進 Elasticsearch | 完成，本機 k3d 實測 |
| [`traces-to-elastic/`](traces-to-elastic/1-intent.md) | 新方向第 4 步：trace 靠自動埋點送出，經同一套收集程式進 Elasticsearch | 完成，本機 k3d 實測 |
| [`logs-high-volume/`](logs-high-volume/1-intent.md) | 補第 2 步：log 每秒幾千行時不掉、不重複，量出延遲與資源 | intent 待確認 |
| `_template/` | 開新需求時複製的範本 | — |

原本寫 VictoriaMetrics 那個做法的五份需求（總綱、檢查工具、依部門分租戶、兩站雙寫、AP1105）已經從這裡刪掉。
要看的話在 branch `chore/ai-native-scaffold`，或這條 branch 的 git 歷史。

[`architecture.html`](architecture.html) 是給主管看的動態架構圖，畫了原本的設計、新方向、已經實測的測試架構，並附多廠區時的資料量試算。用瀏覽器直接開（要連網）。

## 每個需求的四份文件

| 檔案 | 回答 |
| --- | --- |
| `1-intent.md` | 為什麼要做、解決誰的什麼問題、**不做什麼** |
| `2-spec.md` | 要做成什麼樣、影響到哪些既有行為 |
| `3-plan.md` | 怎麼做、會動到哪些檔案、風險與退路 |
| `4-acceptance.md` | 怎麼算完成 —— 可執行的指令加預期輸出 |

⚠️ 一次只往前一個階段，每份要人確認才寫下一份。一口氣四份生出來的話，
intent 還沒談定，plan 已經假設了一堆沒講好的事。

⚠️ 檔名用 `1-intent.md` 不是 `1. intent.md`。檔名裡有空格的話，`cd`、`cat`、
`git add` 都要加引號，而示範指令被照貼進終端機時會壞掉。
