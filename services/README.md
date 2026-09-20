# services：應用程式碼

每個應用一個資料夾，裡面放程式碼和 Dockerfile。部署用的 chart 在同階層的 [../charts](../charts)。

```
services/<app>/      程式碼、Dockerfile      ← CI 的 build 階段看這裡
charts/<app>/        Chart.yaml、values      ← CI 的 deploy 階段看這裡
```

## 一條硬規定：兩邊資料夾必須同名

`services/yms-sim` 對應 `charts/yms-sim`，不能一邊叫 `yms-sim` 一邊叫 `yms-simulator`。

CI 和 ArgoCD 都靠這個名字把兩邊接起來：改到 `services/yms-sim/` 就 build 那個映像、推到 `.../yms-sim:<tag>`，再用 `charts/yms-sim` 部署。名字對不上，pipeline 會安靜地做出錯的事（build 了沒人部署，或部署了舊映像）。

這個名字同時也是：

- `values.yaml` 裡的 `app.name`
- pod 的 `app.kubernetes.io/name` label
- metrics 和 log 上的 `app` 欄位
- namespace 名稱裡的 app 段（`zk-yms-sim-prod`）
- [歸屬對照表](../platform/cluster-addons/ns-labeler/registry/apps.tsv) 的 key

**所以上線後不要改名。** 改了等於歷史資料對不起來。

## 新增一個服務要做的事

1. `services/<app>/` 放程式碼和 Dockerfile
2. `charts/<app>/` 複製一份現成的 chart，改 `values.yaml`
3. 在 [apps.tsv](../platform/cluster-addons/ns-labeler/registry/apps.tsv) 加一行，**這步要先合進去**（否則資料會進 unassigned，事後救不回來，見 [attribution.md](../docs/attribution.md)）

ArgoCD 的 ApplicationSet 用 git generator 掃 `charts/*`，**不用改 ApplicationSet**，新資料夾自己會長出一個 Application。

## 程式要吐出什麼

平台只能幫你把資料送到對的地方，**吐不吐得出來是應用自己的事**。

| 要吐的 | 怎麼吐 |
|---|---|
| metrics | 開一個 `:9464/metrics`，chart 會自動產生 VMServiceScrape 去抓 |
| log | 寫 JSON 到 stdout，至少要有 `level`、`message`，有 trace 就加 `trace_id` |
| 業務指標 | 見 [roadmap 階段三](../docs/roadmap.md)。CPU 和記憶體正常但產線卡住，靠的就是這些 |

`app.kubernetes.io/name`、`version`、`dept`、`group`、`cluster` 這些欄位**不用自己填**，平台會補。應用只要負責 `level`、`message`、`trace_id` 和自己的業務指標。

[yms-sim](yms-sim) 是完整的參考實作：Prometheus 指標、JSON log、業務指標長什麼樣子都在裡面，四十行看得完。

## 基數警告

label 的值只能是「種類有限」的東西：機台編號（幾十個）、站點（幾十個）、結果（ok / fail）可以。

**批號、工單號、序號、trace_id 絕對不能當 label。** 一個高基數 label 就能把儲存撐爆，而且是慢慢爆，等發現時已經塞了幾個月。要記這些就寫進 log，不要寫進 metrics。

## `_standards/`

跨服務共用的設定範本，例如 Java 的 [logback-spring.xml](_standards/java/logback-spring.xml)（輸出符合上面格式的 JSON log）。底線開頭是為了跟真正的服務資料夾區分開，ApplicationSet 掃 `charts/*` 時也不會掃到這裡。
