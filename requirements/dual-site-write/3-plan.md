# Plan — 兩廠雙寫

> `2-spec.md` 已確認。分三批，前一批驗收過才做下一批，每批一個 commit。
> 檔案數超過 3，沿用需求 2 的做法：每批各自驗證。

## 第一批：設定與靜態檢查

做完 `./tests/validate.sh` 要 `PASS`，而且每條新檢查改壞都會 FAIL。

1. `platform/vmstack-values.yaml`：`remoteWrite` 改成兩條，各帶自己的 token（`vmauth-token` 的 `ctsp`、`erlin`）。兩個網址都是佔位，檔案裡標明。
2. 同一份：開 `statefulMode`，`statefulStorage` 要 50 GiB，`remoteWriteSettings.maxDiskUsagePerURL` 設 20 GiB。旁邊寫暫存大小的算法與要看的指標。
3. 新增 `platform/site-ctsp.yaml`、`platform/site-erlin.yaml`，各只有 `vmagent.spec.externalLabels.site`。
4. `platform/vmagent-routing-overlay.yaml`：跟著 `vmstack-values.yaml` 重新產生，多帶三個欄位。
5. `tests/validate.sh`：分流檢查改成兩條 `remoteWrite`，加上網址不能一樣、token 的 key 不能一樣、`statefulMode` 要開、暫存上限要設、PVC 要裝得下兩個目的地的上限。
6. 同一份：檢查 `platform/site-*.yaml` 只有 `site`、值在允許的清單裡、檔名跟值一致。
7. 每條新檢查加「改壞一定被擋下」的自我檢查。

| 檔案 | 改什麼 |
| --- | --- |
| `platform/vmstack-values.yaml` | 步驟 1、2 |
| `platform/site-ctsp.yaml`、`platform/site-erlin.yaml` | 新增 |
| `platform/vmagent-routing-overlay.yaml` | 步驟 4 |
| `tests/validate.sh` | 步驟 5 ~ 7 |

⚠️ 動到 `tests/`：需求 2 的自我檢查「`remoteWrite` 兩條要被擋下」會改成
「不是兩條要被擋下」。這是 spec 列出的行為變更，不是為了讓測試通過。

## 第二批：k3d 實測

1. `tests/tenant-routing/backends.yaml`：加第二座（`tr-site2`：VictoriaMetrics cluster 加 vmauth），token 改成兩個 key，偽造的 pod 多自稱一個 `site`。
2. `check.py`：產生 VMAgent 時疊上 `platform/site-ctsp.yaml`，兩條網址各換成本機的兩座。
3. `check.py`：分流檢查每個情境兩座都查，要一樣；再查每筆樣本的 `site`，以及沒有 `site` 的樣本數是 0。
4. `check.py`、`run.sh`：vmagent 變成 StatefulSet 之後，找 pod 與等它就緒的方式要改。
5. `run.sh outage`：停掉站 1 的入口 → 確認站 2 照常 → 中間刪掉 vmagent 的 pod → 恢復 → 比兩座在停機期間的筆數。
6. `run.sh outage` 不管成功失敗，結束時都把站 1 的入口開回來。
7. 反向驗證：用改壞的複本（拿掉 PVC、只剩一條 `remoteWrite`）跑，確認會 FAIL。

| 檔案 | 改什麼 |
| --- | --- |
| `tests/tenant-routing/backends.yaml` | 步驟 1 |
| `tests/tenant-routing/check.py` | 步驟 2 ~ 5 |
| `tests/tenant-routing/run.sh` | 步驟 4 ~ 6 |

## 第三批：文件

1. `README.md`：開頭的流程、分流那一節、Tests、Known limitations。
2. `applications/eqp-collector/README.md`：需求 2 寫的「只有一條 `remoteWrite`」那幾處。
3. `.agents/project/commands.md`：加 `run.sh outage`。
4. `requirements/multi-site-metrics-write/architecture.html`：「下一步」那張改成已實測，拿掉「規劃中」的標示，重新發佈到同一個網址。

| 檔案 | 改什麼 |
| --- | --- |
| `README.md`、`applications/eqp-collector/README.md` | 步驟 1、2 |
| `.agents/project/commands.md` | 步驟 3 |
| `requirements/multi-site-metrics-write/architecture.html` | 步驟 4 |

## 風險與退路

| 風險 | 怎麼發現 | 退路 |
| --- | --- | --- |
| cluster 沒有可用的 StorageClass，vmagent 卡在 Pending，**整個 cluster 的 metrics 停掉** | `kubectl get pvc` 是 Pending。本機 k3d 有預設的，測不到這個 | `vmstack-values.yaml` 的註解與 `README.md` 寫明：套之前先 `kubectl get storageclass`。AP1105 的在需求 4 確認 |
| 補送的檢查不穩，偶爾誤判 | 需求 2 遇過同類問題：查詢視窗算進上一輪的資料 | 比的是「停機開始到恢復」這段固定時間窗的筆數，門檻 95%，不是比最近幾秒 |
| `run.sh outage` 跑到一半被中斷，站 1 的入口留在關閉狀態 | 之後跑 `run.sh` 分流測試站 1 全部沒資料 | 步驟 6 用 `trap` 還原；`run.sh` 開頭也會把入口開回來 |
| PVC 比兩個上限的總和小 | 一個目的地的佇列把磁碟吃滿，另一個也寫不進去 | 第一批步驟 5 的檢查 |
| 兩條 `remoteWrite` 的 token key 寫成同一個 | 站 2 一直 401，站 1 正常。看起來像站 2 壞了 | 第一批步驟 5 的檢查；k3d 的兩座用不同的 token，寫錯會測到 |
| overlay 跟 `vmstack-values.yaml` 漂掉 | `tests/validate.sh` 既有的一致性檢查 | 無 |
| 本機記憶體不夠起兩座 | pod 一直 Pending 或 OOM。現在 `tenant-test` 用約 1.5 GB，spike 時兩座都起得來 | 停下來請你處理，不會動 `k3d-spil` |
| 架構圖寫「已實測」但測的範圍有限 | 讀的人以為兩廠都跑過 | 圖上註明：本機一套 vmagent、兩座模擬的中央，不是兩個廠區 |

## 不在這個 plan 裡的

| 不做 | 為什麼 |
| --- | --- |
| 灌滿暫存測「超過上限丟資料」 | spec 已列為不涵蓋 |
| 暫存快滿的告警規則 | 同上 |
| 第二套 vmagent（模擬二林的 cluster） | 同上。設定只差 `site` 的值，由第一批的靜態檢查涵蓋 |
| 套到 AP1105、Deployment 換 StatefulSet 的中斷時間 | 需求 4 |
| 修 `tests/routing`、`tests/ap1105-sim` | 總綱已定 |
| 把 `tests/tenant-routing` 改名 | 它現在不只測租戶，但改名會動到文件與 commit 歷史的對照，另開 |
