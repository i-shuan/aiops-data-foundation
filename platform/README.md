# platform — 平台這一側的設定

⚠️ 這條 branch 正在換方向。原本這裡有給平台團隊的 vmagent 操作說明，隨著
VictoriaMetrics 的設定一起移除了；舊的內容在 branch `chore/ai-native-scaffold`。

## 現在有什麼

| 資料夾 | 內容 | 狀態 |
| --- | --- | --- |
| `argocd/applicationset.yaml` | 有 ArgoCD 的環境用：一個 app 長出一個 Application，並把 `observability` 貼成 namespace 的 label | 留著，沒有實測 |
| `logging/fluent.conf` | Fluentd 的 log 分流，照 `dept` 分 | 舊的設定，之後由收集程式取代 |

## 之後會有什麼

| 資料夾 | 內容 | 狀態 |
| --- | --- | --- |
| `otel/` | OpenTelemetry Collector 的設定：收程式與 server 的健康度，貼標籤，送進 Elasticsearch | 還沒建。試驗用的版本在 [`requirements/health-metrics-to-elastic/spike/`](../requirements/health-metrics-to-elastic/spike/README.md) |

## 已經實測過、之後寫正式設定時要守住的

| 規則 | 違反的後果 |
| --- | --- |
| 重試不能設成 0 | Elasticsearch 停機時資料直接丟掉，不報錯 |
| 部門只認 namespace 的門牌，先丟掉程式自己帶的 | 任何程式都能自稱別的部門 |
| 沒掛門牌或不在清單裡的一律算 `platform` | 資料會進到名字奇怪的地方，或進不去 |
| server 的資料直接標成 `icsd` | server 沒有門牌可讀，不指定的話會全部進 `platform` |

第四條還沒實測，其餘三條的結果在
[`requirements/health-metrics-to-elastic/2-spec.md`](../requirements/health-metrics-to-elastic/2-spec.md)。
