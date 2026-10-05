# requirements

每個需求一個資料夾，用**英文 kebab-case** 命名。流程定義在
[`.agents/shared/workflow.md`](../.agents/shared/workflow.md)。

```bash
cp -r requirements/_template requirements/add-export-to-excel
```

## 目前有哪些

branch `explore/elastic-all-in` 上方向改了：三種資料全進 Elasticsearch，不用 VictoriaMetrics。

| 資料夾 | 內容 | 狀態 |
| --- | --- | --- |
| [`elastic-unified-observability/`](elastic-unified-observability/1-intent.md) | 新方向的總綱：監控程式與 server 的健康度，一套收集程式、全進 Elasticsearch、AIOps 自己訓練，目標是找根因 | 方向已確認 |
| [`health-metrics-to-elastic/`](health-metrics-to-elastic/1-intent.md) | 新方向第 1 步：範例程式，健康度數值經 OpenTelemetry Collector 進 Elasticsearch | spec 草稿，待確認 |
| [`multi-site-metrics-write/`](multi-site-metrics-write/1-intent.md) | 原本的總綱：metrics 寫 VictoriaMetrics | 被新方向取代，留作紀錄 |
| [`validate-manual-apps/`](validate-manual-apps/1-intent.md) | 原需求 1：檢查工具認得不走 ArgoCD 的 app | 完成，新方向照用 |
| [`tenant-label-routing/`](tenant-label-routing/1-intent.md) | 原需求 2：依部門寫進 VictoriaMetrics 的租戶 | 完成並實測。新方向要在 Elasticsearch 上重做 |
| [`dual-site-write/`](dual-site-write/1-intent.md) | 原需求 3：中科、二林兩站各寫一份 | 完成並實測。新方向要重做 |
| [`ap1105-new-overlay/`](ap1105-new-overlay/1-intent.md) | 原需求 4：AP1105 換上新設定 | 停在 spec，不繼續 |
| `_template/` | 開新需求時複製的範本 | — |

[`architecture.html`](architecture.html) 是給主管看的動態架構圖，畫了原本的設計與新方向，並附多廠區時的資料量試算。用瀏覽器直接開（要連網）。

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
