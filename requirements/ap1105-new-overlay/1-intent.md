# Intent — 在 AP1105 的模擬環境套新的 overlay

> 內容已在對話中確認。總綱見
> [`multi-site-metrics-write`](../multi-site-metrics-write/1-intent.md) 的拆法第 4 項。

## 解決誰的什麼問題

要在 AP1105 套新設定的人。AP1105 是已經在跑的環境：vmstack 是別人裝的、最新一版的
Helm release 是 failed、vmagent 現在只有一條 `remoteWrite` 寫自己的租戶 0。新的
overlay 只在乾淨的 k3d 上測過，沒有在長得像 AP1105 的環境走過一遍。

## 不做會怎樣

| 現況 | 代價 |
| --- | --- |
| `tests/ap1105-sim` 測的是舊 overlay（三把 token），現在跑不過 | AP1105 這種環境完全沒有測試保護 |
| 沒試過在 failed 的 release 上套新 overlay | 第一次試就是在真的機器上 |
| 沒試過 vmagent 從 Deployment 換成 StatefulSet 的過程 | 不知道中間會不會有一段沒在抓資料 |
| 沒試過沒有 StorageClass 會怎樣 | 這是目前已知最大的風險，只有文件上的警告 |
| 沒試過 `helm rollback` 回不回得去 | 出事時沒有確定的退路 |

## 成功長什麼樣

`./tests/ap1105-sim/run.sh` 在本機重現 AP1105 的現況後，照
`platform/README.md` 的步驟套新 overlay：資料依 `tenant` 進兩站各自的 AccountID，
`helm rollback` 回得到原本的單一寫入，每一步都有檢查。

## 明確不做什麼

| 不做 | 為什麼 |
| --- | --- |
| 在真的 AP1105 上執行 | 只產出驗證過的步驟，正式環境由人執行 |
| 搬租戶 0 的舊資料 | 總綱已定：留在原處等保留期到期 |
| 修 `tests/routing`（ArgoCD 的路） | 總綱已定：等 ArgoCD 那條路補做 |
| 兩站真正的網址、token 的發放 | 待 Infra。模擬環境用本機的兩座 |
| 重新驗 hook 失敗與 `syncJob` 的處理 | 舊測試的發現 2 跟分流無關，結論不變。只確認新 overlay 在 failed 的 release 上套得上去 |
