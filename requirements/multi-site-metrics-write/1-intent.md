# Intent — 多廠區 K8s metrics 寫入 VictoriaMetrics

> ⚠️ branch `explore/elastic-all-in` 上這個方向已被
> [`elastic-unified-observability`](../elastic-unified-observability/1-intent.md) 取代，這份留作紀錄。
>
> 已確認。這份是總綱，實作拆成下面「拆法」的四個需求，各自有自己的資料夾。
> 給主管看的動態架構圖：[`architecture.html`](../architecture.html)，用瀏覽器直接開。

## 解決誰的什麼問題

中科、二林兩個廠區的 K8s cluster 裡，多個部門的 namespace 混住在同一個
cluster。各部門的 metrics 要進到各自的租戶，而且任一座 VictoriaMetrics
停機時資料不能斷。

## 不做會怎樣

| 現況 | 代價 |
| --- | --- |
| vmagent 只寫一個 vmauth URL | 那一端停機超過 buffer 上限，那段時間的 metrics 就沒了 |
| metrics 上沒有廠區資訊 | 同一個部門在兩廠的資料進到同一個租戶後分不出來 |
| 租戶只有 `it` / `imc` / `platform` | `tsid`、`icsd`、`bsid` 沒有自己的租戶 |

## 成功長什麼樣

本機 k3d 上，一個貼了 `tenant` label 的 namespace 的 metrics 同時出現在兩座
VictoriaMetrics 的同一個 AccountID，帶著 `tenant` 和 `site` label；關掉其中
一座再開回來，停機期間的樣本會補回去。

## 已談定

| 項目 | 決定 |
| --- | --- |
| namespace label | `tenant`，取代現在的 `group` |
| 租戶與 AccountID | `platform` 1000、`tsid` 1001、`bsid` 1002、`icsd` 1003、`imc` 1004 |
| `tenant` 怎麼變 AccountID | vmagent 依 `tenant` 補 `vm_account_id`，寫 multitenant 端點 |
| 沒貼、打錯 `tenant` | 一律 `tenant="platform"`、AccountID 1000 |
| 一個 namespace 幾個租戶 | 一個。現在的 `it.imc`（抓一次送兩份）不再支援 |
| 廠區 | vmagent 補 `site`：`ctsp` / `erlin` |
| 寫入 | 每個 vmagent 兩條 `remoteWrite`，Site 1、Site 2 各一條 |
| 認證 | 帶 bearer token，兩座前面各一個 vmauth，不直連 vminsert |
| 停機時 | vmagent 掛 PVC 排隊，目標撐 8 小時，每個目的地上限 20 GiB（估計值，要實測） |
| AP1105 舊資料 | 留在 AccountID 0 不搬，等保留期到期 |
| 舊分流 | `platform/victoriametrics/vmagent-routing-overlay.yaml` 的三條 token 分流由新設計取代 |
| 中央那一側 | 兩座 VictoriaMetrics 與 vmauth 不在這個 repo。這裡只交付 vmagent 與 app 這一側，本機用 k3d 模擬兩座 |
| 現有 app 的租戶 | `mes-dispatch` → `icsd`、`yms-sim` → `imc`、`eqp-collector` → `imc`、`metrics-sim` → `platform` |
| AP1105 | 寫一份新的 overlay 疊在現有 values 上。`tests/ap1105-sim` 等新驗證在 k3d 跑通後才刪 |
| token | 每個 cluster 一把，由 vmauth 驗。⚠️ 租戶是 vmagent 自己填的 `vm_account_id`，vmauth 不檢查，貼錯對照會直接寫進別人的租戶 |
| 部署方式 | 先做不走 ArgoCD 的路：人下 `helm`，namespace 與 label 由 chart 的 `namespace.yaml` 建。`eqp-collector` 是範本。ArgoCD 那條路的檔案留著不刪，這次不驗 |

## 範圍

```
Namespace label → vmagent（scrape、relabel、remoteWrite）→ VictoriaMetrics 寫入端
```

## 明確不做什麼

| 不做 | 為什麼 |
| --- | --- |
| React Flow 架構圖 | 那是需求介紹用的，不是交付物 |
| 查詢端：vmauth `first_available`、Grafana 儀表板 | 範圍只到寫入。⚠️ 查詢端切換有「Site 1 恢復後顯示資料缺口」的問題，要另開需求 |
| log（`platform/logging/fluent.conf`） | 這次只做 metrics |
| ArgoCD 那條路（`platform/argocd/applicationset.yaml`、`tests/routing`） | 先做不走 ArgoCD 的。⚠️ 需求 2 改名後，這條路在補做之前是壞的 |
| 正式 cluster 的套用 | 只產出設定與本機驗證，正式環境由人執行 |

## 拆法

照順序做，前一個驗收過才開下一個。

| 順序 | 資料夾 | 內容 |
| --- | --- | --- |
| 1 | `requirements/validate-manual-apps/` | 讓 `tests/validate.sh` 回到綠燈：認得不走 ArgoCD 的 app，並檢查它的 namespace label |
| 2 | `requirements/tenant-label-routing/` | `group` 改 `tenant`、AccountID 對照、`platform` 兜底 |
| 3 | `requirements/dual-site-write/` | `site`、兩條 `remoteWrite`、PVC 與 buffer、本機模擬兩座 |
| 4 | `requirements/ap1105-new-overlay/` | AP1105 的新 overlay，新驗證跑通後淘汰舊分流與 `tests/ap1105-sim` |

⚠️ 第 2 個光改名就超過「單次 ≤ 3 個檔」，已同意破例。
