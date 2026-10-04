# Spec — 驗證不走 ArgoCD 的 app

> 這一份要人確認之後，才寫 `3-plan.md`。
> 「行為」表的每一列都會變成 `4-acceptance.md` 的一條驗收項。

## 怎麼分辨兩種 app

不另外維護清單，看 app 自己的檔案：

| app 的 chart | 判定 | namespace label 誰貼 |
| --- | --- | --- |
| `values.yaml` 有 `argocd:` 區塊 | 走 ArgoCD | ArgoCD（`platform/applicationset.yaml`） |
| 沒有 `argocd:`，有 `templates/namespace.yaml` | 手動 Helm | chart 自己 |
| 兩個都有 | 錯誤 | 兩邊搶同一個 namespace |
| 兩個都沒有 | 錯誤 | 沒有人貼，metrics 全部進兜底租戶 |

## 行為

| 情境 | 預期行為 |
| --- | --- |
| 手動 app，`values.yaml` 正確 | 第 1 段 OK，標示「手動 Helm」。不查 `argocd` 欄位，照查 `observability` 與 chart 渲染 |
| 手動 app，`observability` 寫錯（例如 `group: imcc`） | 第 1 段 FAIL，訊息跟走 ArgoCD 的 app 一樣 |
| 手動 app，沒寫 `namespace` | 第 1 段 FAIL |
| 兩種 app 宣告同一個 namespace | 第 1 段 FAIL，跟現在兩個 ArgoCD app 撞名一樣 |
| 有 `argocd:` 又有 `templates/namespace.yaml` | 第 1 段 FAIL |
| 沒有 `argocd:` 也沒有 `templates/namespace.yaml` | 第 1 段 FAIL |
| 第 3 段，手動 app | 用 `helm template` 渲染 chart，取出 Namespace 的 label，要跟 `observability` 一樣；沒寫的欄位不能變成空字串 |
| 第 3 段，走 ArgoCD 的 app | 跟現在一樣，用 `templatePatch` 渲染 |
| 目前的 repo | 五段跑完，最後一行 `PASS`，exit 0 |

## 邊界與限制

* 輸入的合法範圍：手動 app 的 chart 剛好渲染出一個 `kind: Namespace`。
* 超出範圍時怎麼處理：渲染出零個或多個 Namespace，第 3 段 FAIL。
* 併發 / 權限 / 資料量上的限制：無。不需要 cluster，只要 `helm` 和 `python`。

## 影響到哪些既有行為

| 既有行為 | 會變成怎樣 | 需要改既有測試嗎 |
| --- | --- | --- |
| `eqp-collector` 在第 1 段 FAIL | OK（手動 Helm） | 改 `tests/validate.sh` |
| 第 3 段 traceback，第 4、5 段跑不到 | 五段都跑完 | 同上 |
| 缺 `argocd:` 一律 FAIL | 有 `templates/namespace.yaml` 就當手動 app；兩個都沒有才 FAIL | 同上 |
| 第 4 段的自我檢查 | 多兩條：兩個都有、兩個都沒有，都要被擋下 | 同上 |
| 第 5 段的自我檢查 | 多一組：手動 app 的渲染要照原值、沒寫 `system` 不能出現空 label | 同上 |
| `app/eqp-collector/README.md` 寫「`validate.sh` 會失敗（已知、刻意保留）」 | 敘述過期，要改寫 | 不適用 |

⚠️ 「缺 `argocd:` 一律 FAIL」被放寬了。之後有人新增走 ArgoCD 的 app 卻忘了寫
`argocd:`，會落到「兩個都沒有」被擋下，訊息會同時提示兩種補法。

## 這份 spec 不涵蓋的

| 不涵蓋 | 為什麼 |
| --- | --- |
| 幫 `eqp-collector` 補 `argocd:` 區塊 | 見 `1-intent.md` |
| ApplicationSet 掃到 `eqp-collector` 會報錯 | ArgoCD 那條路這次不做。`app/eqp-collector/README.md` 現有的警告保留 |
| 把其他三個 app 改成手動 Helm | 另一個需求 |
| `group` 改 `tenant` | 總綱拆法第 2 項 |
| 真的部署到 cluster 確認 label | `helm template` 只證明 chart 會產生對的 Namespace，不證明 cluster 上的結果。那是需求 2、3 的 k3d 測試 |
