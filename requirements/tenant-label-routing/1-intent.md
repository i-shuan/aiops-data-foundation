# Intent — 依 `tenant` label 寫進對應的 AccountID

> 內容已在對話中確認。總綱見
> [`multi-site-metrics-write`](../multi-site-metrics-write/1-intent.md) 的拆法第 2 項。

## 解決誰的什麼問題

`tsid`、`bsid`、`icsd`、`imc` 四個部門的 namespace 混住在同一個 cluster。
現在分流只認 `it` / `imc` / `platform` 三個值，每多一個租戶就要多一條
`remoteWrite` 和一把 token。

## 不做會怎樣

| 現況 | 代價 |
| --- | --- |
| `tsid`、`bsid`、`icsd` 沒有對應的分流 | 它們的 metrics 全部落到兜底租戶，跟平台的資料混在一起 |
| 租戶數 × 目的地數 = `remoteWrite` 條數 | 需求 3 做雙寫時，五個租戶會變成十條 |

## 成功長什麼樣

本機 k3d 上，namespace 貼 `tenant: icsd` 的 app，metrics 只出現在 AccountID
1003；沒貼或貼錯的只出現在 1000。vmagent 只有一條 `remoteWrite`。

## 明確不做什麼

| 不做 | 為什麼 |
| --- | --- |
| `site` label、第二條 `remoteWrite`、PVC 與 buffer | 總綱拆法第 3 項 |
| 修好 `tests/routing`、`tests/ap1105-sim` | 它們測的是舊的三把 token 分流。前者屬於 ArgoCD 那條路，後者等第 4 項 |
| 中央的 VictoriaMetrics 與 vmauth | 不在這個 repo。本機用模擬的 |
| log 的分流（`platform/logging/fluent.conf`） | 它看的是 `dept`，這次不動 |
| 把 `mes-dispatch` 等三個 app 改成手動 Helm | 另一個需求 |
| 一個 namespace 送多個租戶 | 已決定不再支援 `it.imc` |
