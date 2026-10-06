# Intent — log 經同一套收集程式進 Elasticsearch

> 這一份要人確認之後，才寫 `2-spec.md`。總綱見
> [`elastic-unified-observability`](../elastic-unified-observability/1-intent.md) 的拆法第 2 步。

## 解決誰的什麼問題

IT 維運人員。數值告訴他們「有事、什麼時候開始」，但要知道「為什麼」得看程式當時寫了什麼。
現在範例程式有印 log，沒有人收：出事之後只能進 cluster 一個 pod 一個 pod 看，pod 重啟就沒了。

## 不做會怎樣

| 現況 | 代價 |
| --- | --- |
| log 沒有進 Elasticsearch | 從異常的數值找不到同一時間、同一個服務的 log，只能靠人進 cluster 看 |
| 舊的 `platform/logging/fluent.conf` 照 `dept` 分，部門清單寫死在設定裡 | 跟數值用的 `tenant` 對不起來，是第二份要維護的對照 |
| log 與數值由兩套軟體各貼各的標籤 | 之後做異常偵測與找根因時，兩種資料串不起來 |

## 成功長什麼樣

本機 k3d 上，範例程式的 log 經節點那一份收集程式進到 Elasticsearch，而且：

| 檢查 | 預期 |
| --- | --- |
| 分部門 | 跟數值同一套規則：照 namespace 的門牌，沒掛或寫錯的進 `platform` |
| 標籤 | 每筆帶 `tenant`、`dept`、`system`、`site`、`cluster`，值跟同一個 pod 的數值一模一樣 |
| JSON 的 log | 拆成欄位：等級、訊息、`trace_id` 各自查得到 |
| 不是 JSON 的 log | 照樣收，整行當訊息，不丟掉 |
| 把 `demo-api` 弄成回錯誤 | Elasticsearch 裡查得到 `demo-api` 的錯誤 log，用同一個 `trace_id` 也查得到 `demo-web` 那一筆 |
| 停掉 Elasticsearch 再開回來，中間重啟收集程式 | 停機期間的 log 補回來，不重複 |
| 程式在 log 裡自稱別的部門 | 不算數，照門牌走 |

## 明確不做什麼

| 不做 | 為什麼 |
| --- | --- |
| trace 的收集 | 第 4 步。這一步只確認 log 裡的 `trace_id` 查得到 |
| 節點本身的系統 log（kubelet、作業系統） | 先做容器的。系統 log 的來源每種節點不一樣，本機的 k3d 驗不出真的 server 的樣子 |
| 只留某些等級、把相似的 log 歸類 | 總綱的分層保留原則，另外開。這一步全收 |
| log 的異常偵測 | 第 3 步 |
| 遮掉 log 裡的敏感內容 | 範例程式的 log 沒有。真的系統要不要遮、遮什麼，要各部門一起定 |
| 誰能看哪個部門的 log | 跟數值一樣，資料流分開、權限另外定 |
| Kibana 的畫面 | 跟上一步一樣，用查詢確認 |
| 幫 `mes-dispatch` 改寫法 | 另外決定 |

## 還沒談定的 ❓

| # | 問題 | 我的建議 |
| --- | --- | --- |
| 1 | 舊的 `platform/logging/fluent.conf` 怎麼辦 | 這一步驗收過了就刪。留著的話 repo 裡有兩套收 log 的設定，分部門的規則還不一樣。舊的在 branch `chore/ai-native-scaffold` 查得到 |
| 2 | 收哪些 namespace 的 log | 全收，跟數值一樣：沒掛門牌的進 `platform`。只收有門牌的話，新系統忘了掛門牌時 log 會無聲消失 |
| 3 | 收集程式與 Elasticsearch 自己的 log 收不收 | 收，進 `platform`。但要排除收集程式自己的 log，不然它每送一筆就多印一行，會自己餵自己 |
| 4 | 多行的 log（例如程式噴的錯誤堆疊） | 這一步不合併，每行一筆。合併的規則每種語言不一樣，範例程式是一行一筆 JSON，驗不到 |
