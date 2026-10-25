# 寫入簡化：一把寫入 key、ES failure store、靜態保底規則（2026-10）

**狀態**：進行中　　**日期**：2026-10-25　　**相關**：分支 `claude/single-writer-key`
**模組**：① 收值（寫入 key、Collector 出口）、③ 存（failure store）、④ 判斷（靜態保底規則）

## Plan：這一次要達到什麼

- **需求**：
  1. 現在每個部門 group（it、imc、unassigned）一把寫入 key、一個 Collector 出口。key 設錯或權限 pattern 漏了，ES 會回 200、把整批拒絕放在回應內容裡，Collector 算成「送成功」、沒有任何 log（[es-startup-401](2026-10-es-startup-401.md) 實測發現）。三把 key 就是三倍的出錯機會，換 key 也要做三次。組織**沒有部門間寫入隔離的硬性要求**（2026-10-25 確認）；「資料屬於哪個部門」本來就由 Collector 依 Pod 查 K8s 決定，不信應用自己填。
  2. 型別寫錯（mapping 衝突）、pipeline 失敗的文件，ES 目前直接拒絕，只有 Collector log 看得到。
  3. baseline 要累積資料才判斷得準（退階：同星期×同小時 2 週 → 每天同小時 3 天 → 不分時段 50 點）；上線第一天沒有任何「一定是壞了」的保底。
- **目的**：
  1. 一把寫入 key、一個出口。資料照樣依部門分到各自的 index（`data_stream.namespace` 由 Collector 依 group 設），讀取權限照樣依部門隔離。
  2. 被 ES 拒絕的文件留在 failure store 查得到、可以告警。
  3. 錯誤率、p95 有固定門檻的保底規則，第一天就能報。
- **範圍**：
  - 做：Collector 三份設定（common、node、sampler）、cluster 的出口；`elasticsearch-setup.sh` 發一把 key；`rotate-writer-key.sh` 改成一把；e2e 腳本；文件。failure store 開在 log、metrics、trace 的 index template。rules 加「一段時間內的比例／百分位」類型和兩條規則。
  - 不做：讀取權限（人看自己部門的角色）不動；`dept → group` 的分流規則不動；防冒充（Collector 依 Pod 補身分）不動。

| 不變的 | 為什麼還需要 |
|---|---|
| Collector 依 Pod 補 dept、group、service.name，刪掉應用自己帶的 | 決定資料屬於哪個部門；防填錯 |
| index 依 group 分（`k8s-<group>-*`、`*.otel-<group>`） | 讀取權限、保留天數依部門 |
| 給人用的唯讀角色每個 group 一個 | 人只看自己部門 |

| 變了的 | 之前 | 之後 |
|---|---|---|
| 寫入 key | 3 把（`es-writer-it`、`-imc`、`-unassigned`），各自只能寫自己的 index | 1 把（`es-writer`），能寫所有 `k8s-*`、`metrics-*`、`logs-*.otel-*`、`traces-*.otel-*` |
| Collector 出口 | 每個 group 一個，`routing` 依 group 分 | 一個，不用 routing |
| Secret `es-writer-keys` | 3 個欄位 | 1 個欄位 `ES_WRITER_API_KEY` |
| 換 key | 每個 group 各換一次 | 換一次 |
| 失去的 | 「IT 的 key 寫不進 IMC」：某個 cluster 的 Collector 設定被改壞時，最後一道防線 | — |

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）
