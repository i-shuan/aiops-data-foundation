# Intent — 健康度數值經 OpenTelemetry Collector 進 Elasticsearch

> 內容已在對話中確認。總綱見
> [`elastic-unified-observability`](../elastic-unified-observability/1-intent.md) 的拆法第 1 步。

## 解決誰的什麼問題

要做後面每一步的人。異常偵測、找根因都需要「程式與 server 的健康度數值已經在
Elasticsearch 裡，而且帶著正確的部門標籤」。現在這條路不存在：數值是 vmagent 送進
VictoriaMetrics，範例程式是模擬機台收值的。

## 不做會怎樣

| 現況 | 代價 |
| --- | --- |
| 沒有東西把數值送進 Elasticsearch | 後面五步都沒有資料可用 |
| 沒有跟機台無關、可以弄壞的範例程式 | 沒辦法製造已知的故障來驗證偵測與找根因 |
| 分部門、擋冒充、斷線補送只在 vmagent 上測過 | 換一套軟體之後這些都不能假設成立 |

## 成功長什麼樣

本機 k3d 上，兩支互相呼叫的範例程式和它們所在的節點，健康度數值經
OpenTelemetry Collector 進到 Elasticsearch，而且：

| 檢查 | 預期 |
| --- | --- |
| 程式的數值 | 照 namespace 的門牌分部門 |
| 節點的數值 | 標成 `icsd` |
| 沒掛門牌的 | 進 `platform` |
| 停掉 Elasticsearch 再開回來 | 停機期間的數值補回去 |

## 明確不做什麼

| 不做 | 為什麼 |
| --- | --- |
| log 與 trace 的收集 | 第 2、4 步。範例程式這一步就會吐，但先不收 |
| 異常偵測 | 第 3 步 |
| 刪掉 VictoriaMetrics 的設定、舊測試與 `eqp-collector` | 第 6 步。這一步做完之前，repo 裡要一直有一條能跑的路 |
| 部門之間「誰能看誰」的權限 | 這一步只確保資料分對地方。權限要看免費版有哪些功能，另外定 |
| 兩站各存一份 | 這一步先確認「可以送兩個目的地」，不建第二座 |
| 資料的分層保留與壓縮 | 原則已定在總綱，實作放在第 3 步之前另開 |
| 正式環境的 Elasticsearch | 不在這個 repo。本機用一座單節點的 |
