# Intent — 驗證不走 ArgoCD 的 app

> 內容已在對話中確認。總綱見
> [`multi-site-metrics-write`](../multi-site-metrics-write/1-intent.md) 的拆法第 1 項。

## 解決誰的什麼問題

改 `applications/` 或 `platform/` 的人跑 `./tests/validate.sh` 時，現在一定
是紅的。腳本假設每個 app 都由 ArgoCD 部署；`eqp-collector` 是人下 `helm`
部署的，沒有 `argocd:` 區塊，腳本在第 3 段直接 traceback。

## 不做會怎樣

| 現況 | 代價 |
| --- | --- |
| 腳本永遠 exit 1 | 接下來 `group` 改 `tenant` 的重構沒有基準，分不出是舊債還是新錯 |
| 第 4、5 段跑不到 | 「錯誤寫法一定被擋下」這層保護目前沒在運作 |
| 不走 ArgoCD 的 app 的 namespace label 沒人檢查 | 這次要先做的就是這條路，而它目前零驗證 |

## 成功長什麼樣

`./tests/validate.sh` 在目前的 repo 上跑完五段、exit 0，其中
`eqp-collector` 的 namespace label 有被檢查到跟 `values.yaml` 寫的一樣。

## 明確不做什麼

| 不做 | 為什麼 |
| --- | --- |
| 幫 `eqp-collector` 補 `argocd:` 區塊 | 先做不走 ArgoCD 的路 |
| 刪掉 ArgoCD 那條路的檔案 | 之後還要做，留著 |
| 讓 ApplicationSet 排除 `eqp-collector` | 屬於 ArgoCD 那條路，等做那條路時處理 |
| 把其他三個 app 改成不走 ArgoCD | 另一個需求，這次只讓驗證變綠 |
| `group` 改 `tenant` | 拆法第 2 項 |
