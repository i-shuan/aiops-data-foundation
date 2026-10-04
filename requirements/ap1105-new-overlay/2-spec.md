# Spec — 在 AP1105 的模擬環境套新的 overlay

> 已確認。
> 「行為」表的每一列都會變成 `4-acceptance.md` 的一條驗收項。

## 做法

改寫既有的 `tests/ap1105-sim`，不另外開一支。它前半段「把 k3d 弄成 AP1105 的樣子」
跟分流無關，照用；後半段跟分流有關的檢查全部換成新設計。

| 舊測試的步驟 | 這次怎麼處理 |
| --- | --- |
| `cluster`、`ap1105`、`diagnose` | 留著不動：建 cluster、重現 rev1 成功與 rev2 hook 失敗、看 CRD |
| `apps` | 改：中央換成兩座，測試 app 的情境換成四個租戶、沒貼、打錯、冒充 |
| `routed` | 改：建兩個 key 的 Secret，套新 overlay 加廠區的檔，檢查換成新設計 |
| `rollback` | 改：回去之後是單一寫入，而且 vmagent 還在抓 |
| `hookfix`、`final` | 留著，`final` 的檢查換成新設計 |
| `oldcrd`、`crdfix` | 改：舊 CRD 裁掉的欄位換成這次新加的那幾個 |
| 新增 `nostorage` | 沒有 StorageClass 時套 overlay 會怎樣 |

兩座中央直接用 `tests/tenant-routing/backends.yaml`，不另外寫一份。

## 行為

| 情境 | 預期行為 |
| --- | --- |
| 套之前 | vmagent 一條 `remoteWrite`，資料在 AP1105 自己的租戶 0，樣本沒有 `tenant` |
| dry-run 比對 | 只有 VMAgent 這一個資源有變；`externalLabels` 原有的 `cluster` 還在，多一個 `site` |
| 在 failed 的 release 上套 | 套得上去，分流生效，release 仍然是 failed |
| 送進去的欄位 | 沒有被 CRD 裁掉 |
| vmagent 換成 StatefulSet | 換的過程中沒抓資料的時間有量出來、寫進文件 |
| 套完的分流 | 七個情境在兩站都只出現在該去的 AccountID，帶 `site=ctsp` 與原有的 `cluster` |
| 原本寫租戶 0 的系統元件 | 新資料改進兩站的 1000；AP1105 自己的租戶 0 不再有新資料，舊資料還查得到 |
| `helm rollback` | 回到一條 `remoteWrite`、寫租戶 0、vmagent 在抓。PVC 留著不影響 |
| rollback 之後再套一次 | 跟第一次一樣成功 |
| CRD 是不認得新欄位的舊版 | 模擬會實際量出後果並寫進文件。⚠️ 預期是分流規則被裁掉、資料全部進同一個租戶，不報錯 |
| 沒有可用的 StorageClass | 模擬會實際量出後果：vmagent 是不是整個停掉、舊的那個還在不在 |
| 沒疊廠區的檔 | 樣本沒有 `site`，檢查會 FAIL |

## 邊界與限制

* 輸入的合法範圍：chart 0.91.0、operator v0.74.0，跟 AP1105 現在的版本一樣。
* 超出範圍時怎麼處理：其他版本不在這次的範圍，文件寫明。
* 資料量上的限制：本機記憶體有限，跑這支時 `tenant-test` 要先停掉。

## 影響到哪些既有行為

| 既有行為 | 會變成怎樣 | 需要改既有測試嗎 |
| --- | --- | --- |
| `tests/ap1105-sim/run.sh` 現在跑不過 | 可以跑 | 改 `run.sh`、`check.py`、`central-vmauth.yaml` |
| `tests/ap1105-sim/README.md` 記的是舊分流的結果 | 分流的部分改成新設計的結果；發現 1、2 的結論留著，數字重測 | 不適用 |
| 「`it.imc` 抓一次送兩份」的檢查 | 拿掉，那個功能已經不存在 | 改 `check.py` |
| 根目錄 `README.md`、`platform/README.md` 寫「新 overlay 沒有在 AP1105 的模擬環境走過」 | 改成已走過，附量到的數字 | 不適用 |
| `.agents/project/commands.md` 把 `ap1105-sim` 標成過期 | 拿掉過期的標示 | 不適用 |

## 已定的做法

| 項目 | 決定 |
| --- | --- |
| 測試怎麼做 | 改寫既有的 `tests/ap1105-sim`，不另外開一支 |
| 沒有 StorageClass 的情況 | 加一步 `nostorage` 實測 |
| 本機資源 | 跑這支前先 `k3d cluster stop tenant-test`，跑完再開 |
| AP1105 真正的 StorageClass | 待確認。不影響模擬，會影響寫給人照做的步驟 |

## 這份 spec 不涵蓋的

| 不涵蓋 | 為什麼 |
| --- | --- |
| 真的 AP1105 | 見 `1-intent.md` |
| 其他版本的 chart 與 operator | 只測 AP1105 現在的版本 |
| 暫存灌滿、兩個廠區各一套 | 需求 3 已列為不涵蓋 |
| 套 overlay 的當下 AP1105 既有的 Grafana 與告警會看到什麼 | 模擬環境的 Grafana 是關的。只確認資料從租戶 0 換到 1000 這件事 |
