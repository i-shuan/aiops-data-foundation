# Acceptance — 健康度數值經 OpenTelemetry Collector 進 Elasticsearch

> 每一項都要是**可執行的指令加預期輸出**。分批驗收，前一批全過才做下一批。
> 都從 repo 根目錄、用 Git Bash 跑，cluster 是本機的 `obs-test`。

## 第零批：先確認地基（已做）

結果列在 `2-spec.md` 的「先實測過的」。Elasticsearch 8.19.22 與 9.5.3 各測一輪。

## 第一批：範例程式

先跑這段：

```bash
export MSYS_NO_PATHCONV=1
K="kubectl --context k3d-obs-test"
pod() { $K -n demo-tsid get pod -l app.kubernetes.io/name=$1 -o jsonpath='{.items[0].metadata.name}'; }
raw() { $K get --raw "/api/v1/namespaces/demo-tsid/pods/$1:$2/proxy/$3"; }
```

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 1-1 | `./tests/validate.sh \| grep -E "demo-svc\|^PASS"` | 第 1 段 `OK demo-svc（手動 Helm）`；第 2 段列出 `dept=D3120 system=demo tenant=tsid`；`PASS` | ☑ |
| 1-2 | `docker build -q --build-arg BASE_IMAGE=python:3.12-slim -t demo-svc:test app/demo-svc && k3d image import demo-svc:test -c obs-test` | 建得出來、匯入成功 | ☑ |
| 1-3 | `helm --kube-context k3d-obs-test upgrade --install demo-tsid app/demo-svc/k8s -n default --reset-values --wait && $K -n demo-tsid get pods` | `STATUS: deployed`；`demo-api`、`demo-web` 都是 `1/1 Running` | ☑ |
| 1-4 | `$K get ns demo-tsid --show-labels` | 有 `dept=D3120`、`system=demo`、`tenant=tsid` | ☑ |
| 1-5 | `raw $(pod demo-web) 9464 metrics \| grep -E "^demo_(requests_total\|inflight\|fault)"` | 有請求數、進行中的請求數、四種故障的開關狀態 | ☑ |
| 1-6 | 取 `demo-web` 最後一筆 log 的 `trace_id`，在 `demo-api` 的 log 裡 `grep` | 兩邊都找得到同一個編號 | ☑ |
| 1-7 | `raw $(pod demo-api) 8080 "admin/fault?latency_ms=400"`，前後各量 20 秒的平均處理時間 | 兩支都明顯變長 | ☑ |
| 1-8 | `raw $(pod demo-api) 8080 "admin/fault?error_rate=0.5"`，15 秒後看 `demo-web` 的請求數 | 出現 `code="502"` | ☑ |
| 1-9 | `raw $(pod demo-api) 8080 "admin/fault?mem_mb=64&cpu=1"`，10 秒後看記憶體與 CPU | 記憶體多約 64 MB；CPU 用掉的秒數接近經過的秒數 | ☑ |
| 1-10 | `raw $(pod demo-api) 8080 "admin/fault?clear=1"` | 四個值都回到 0；pod 沒有重啟 | ☑ |

### 第一批實際量到的

| 項目 | 數字 |
| --- | --- |
| 正常時的平均處理時間 | `demo-api` 42 ms，`demo-web` 89 ms |
| `demo-api` 多睡 400 ms 之後 | `demo-api` 438 ms，`demo-web` 484 ms |
| `demo-api` 一半回錯誤，15 秒內 | `demo-web` 記到 11 個 `502` |
| 佔 64 MB、吃 CPU | 記憶體 25 MB → 89 MB；10 秒用掉 10.4 秒的 CPU |

## 第二批：正式的收集設定與靜態檢查

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 2-1 | `./tests/validate.sh \| sed -n '/=== 5/,/=== 6/p'` | 印出「允許的 tenant（從 `platform/otel/common.yaml` 讀）」與五個部門；`OK` | ☑ |
| 2-2 | `grep -c "ALLOWED = {" tests/validate.sh` | `0`。腳本裡暫時的那份清單拿掉了 | ☑ |
| 2-3 | `./tests/validate.sh \| sed -n '/=== 6/,$p'` | 22 種改壞的寫法每一條都是 `OK`、後面寫著被哪一條擋下；最後 `PASS` | ☑ |
| 2-4 | `grep -n "elasticsearch/second" platform/otel/cluster-collector.yaml` | 每一行都在註解裡 | ☑ |

第 6 段涵蓋 plan 列的五條，各有對應的改壞寫法：

| plan 列的 | 第 6 段的哪幾條 |
| --- | --- |
| 重試不能是 0 | 重試改成 0、重試關掉、佇列不寫磁碟、佇列的 PVC 換成 emptyDir |
| 先丟掉程式自己帶的再填 | 拿掉「丟掉樣本自己帶的」、沒先丟掉程式帶的 `ns.tenant`、資料流在 `tenant` 之前決定、門牌不比對清單就照用 |
| 對 pod 的那一段還在 | 拿掉對 pod 的那一行、pipeline 拿掉 `k8s_attributes`、pipeline 拿掉 `transform/pre` |
| server 有標 `icsd` | server 沒有標 `SERVER_TENANT`、server 的資料沒補節點名稱、`SERVER_TENANT` 不在清單裡 |
| 密碼沒有寫在檔案裡 | 密碼寫在設定裡、密碼寫在環境變數的值、檔案裡有明文、位址裡夾帳密 |

## 第三批：k3d 實測與文件

cluster `obs-test` 要先建好，建法見 `.agents/project/commands.md`。

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 3-1 | `./tests/obs/run.sh` | 「分流」37 項都是 `OK`、`PASS`；「弄壞看得到」兩支都是 `OK`、`PASS` | ☑ |
| 3-2 | `./tests/obs/run.sh outage` | 重啟收集程式之前、之後排隊的，程式與 server 四項都補回 95% 以上；`PASS` | ☑ |
| 3-3 | `./tests/obs/run.sh broken nospoof` | 「分流」有 `FAIL`；最後一行 `✔ 改壞的設定（nospoof）被測試抓到了` | ☑ |
| 3-4 | `./tests/obs/run.sh broken retry0` | 「停機補送」有 `FAIL`；最後一行 `✔ 改壞的設定（retry0）被測試抓到了` | ☑ |
| 3-5 | `./tests/obs/run.sh broken nopvc` | 「停機補送」有 `FAIL`；最後一行 `✔ 改壞的設定（nopvc）被測試抓到了` | ☑ |
| 3-6 | 跑完 3-3 到 3-5 之後再跑 `python tests/obs/check.py routing` | `PASS`。改壞的設定有換回來 | ☑ |
| 3-7 | `ls requirements/health-metrics-to-elastic/spike` | 不存在。正式的設定與測試蓋過它了 | ☑ |

### 對照 spec 的「行為」表

| spec 的情境 | 由哪一項驗 | 實際結果 |
| --- | --- | --- |
| namespace 貼 `tenant: tsid` | 3-1 | `demo-tsid` 的資料流、`tenant` 都只有 `tsid` |
| `tsid`、`bsid` 各一個 namespace | 3-1 | `demo-bsid` 只在 `bsid` |
| 沒貼、或貼成不在清單裡的值 | 3-1 | `fake-none`、`fake-typo`（貼 `tsidd`）都在 `platform` |
| 節點的 CPU、記憶體、磁碟 | 3-1 | 都在 `icsd`，另外有負載、網路、節點是不是 Ready |
| 程式自己吐的 `tenant` 跟門牌不一樣 | 3-1、3-3 | `fake-spoof` 門牌是 `bsid`、自稱 `tsid`：資料在 `bsid`，自稱的值沒有留在樣本上 |
| 沒掛門牌的 namespace 裡，程式自稱 `tsid` | 3-1 | `fake-none`：資料在 `platform`，自稱的值沒有留在樣本上 |
| 每筆帶 `site` 與 `cluster` | 3-1 | 每個 namespace 的 `site` 都只有 `ctsp`、`cluster` 都只有 `obs-test` |
| 把 `demo-api` 弄慢 | 3-1 | `demo-api` 45 ms → 375 ms，`demo-web` 90 ms → 400 ms |
| Elasticsearch 停機再恢復 | 3-2 | 停 312 秒，補回 100% |
| 重試被改成不重試 | 2-3、3-4 | 靜態檢查擋下；實跑補回 0 / 16、0 / 4 |
| 停機中收集程式重啟 | 3-2、3-5 | 重啟之前排隊的補回 16 / 16、4 / 4 |
| 容器的資源用量照 pod 的部門 | 3-1 | `demo-tsid` 容器的 CPU、記憶體、重啟次數都在 `tsid`，`demo-bsid` 的在 `bsid` |

### 第三批實際量到的

| 項目 | 數字 |
| --- | --- |
| 平常 120 秒的抓取次數 | 程式 16 次（兩支，15 秒一次），server 4 次（30 秒一次） |
| 正確的設定，停 312 秒、中間重啟兩份收集程式 | 重啟前排隊的 16 / 16、4 / 4；重啟後排隊的 16 / 16、4 / 4 |
| 重試改成 0，停 293 秒 | 四項都是 0 |
| 佇列不放 PVC | cluster 那一份重啟前排隊的補回 0 / 16；重啟後的 16 / 16。節點那一份的佇列在節點磁碟上，沒有動，4 / 4 |
| 拿掉「丟掉樣本自己帶的」 | `fake-spoof` 的樣本上留著自稱的 `tenant`、`site`、`cluster`，各 6 筆 |

⚠️ 處理時間是從 Elasticsearch 裡的分布圖算的，取的是每個區間的中間值，所以是 375 這種
整齊的數字，不是精確的平均。拿來看「有沒有變」夠用，不能拿來當精確的延遲。

### 實作時才發現的

| 發現 | 怎麼處理 |
| --- | --- |
| 處理時間這種「累計的分布圖」會被 Elasticsearch 的寫入程式整個丟掉，log 只有一行警告。spike 用的假程式沒有分布圖，所以沒遇到 | 加 `cumulative_to_delta`。`tests/validate.sh` 會擋下拿掉它的修改 |
| k3d 節點的根目錄是 overlay，磁碟的數值預設一筆都沒有，不報錯 | `filesystem` 改成連虛擬的也看，再用類型與掛載點濾掉每個容器自己的 |
| 拿掉擋冒充之後，資料**還是**進對的那一份（`bsid`），只是自稱的值留在樣本上 | 資料流是照 namespace 的門牌定的，程式用一般的標籤改不了。測試查的是「自稱的值有沒有留下」 |
| `broken nospoof` 第一次跑誤判成通過：剛換上改壞的設定就查，查到的還是之前收的資料 | `run.sh broken` 一律先等 150 秒再查 |
| `broken nopvc` 第一次沒跑成：Windows 的 python 看不懂 Git Bash 的 `/tmp` 路徑 | 腳本裡改用轉過的路徑 |

## 跟 spec、plan 不一樣的地方

| 不一樣 | 為什麼 |
| --- | --- |
| 範例程式只有一個資料夾 `app/demo-svc/`，不是 `app/demo-web/` 與 `app/demo-api/` 兩個 | 兩支的程式碼幾乎一樣，只差「要不要呼叫下一支、要不要自己產生流量」。寫成同一支程式用環境變數切換，免得兩份程式碼各自改、越改越不一樣 |
| 一次部署會同時起 `demo-web` 與 `demo-api`，在同一個 namespace | 兩支分開部署的話，第二支會因為 namespace 已經存在而失敗。一起部署也比較符合「一組互相呼叫的服務」 |
| `platform/otel/` 是四個檔，比 plan 多一個 `common.yaml` | 允許的部門清單要有一個固定的地方放，兩份收集程式都讀它，`tests/validate.sh` 也讀它。放進任何一份收集設定裡，另一份就得抄一次 |
| 「讓節點的記憶體吃緊」沒有做成自動的檢查 | 本機只有一個節點，把它的記憶體吃緊會連 Elasticsearch 一起拖垮。只確認了 `icsd` 裡有節點的記憶體數值 |
| 停機補送比的是「抓取次數」，分成重啟前、重啟後兩段，不含重啟的那幾十秒 | 收集程式重啟的那段時間本來就沒有在抓，那不是掉資料。分兩段才分得出是佇列不見還是沒有重試 |

## 已知未涵蓋

| 沒證明 | 為什麼 |
| --- | --- |
| 範例程式的 log 與 trace 有被收走 | 這一步只確認它有吐。總綱第 2、4 步才收 |
| trace 不是真的送到某處 | 目前只有編號在 header 與 log 裡傳。真的送出去要等第 4 步 |
| 正式環境 | 只在本機 k3d 驗 |
| 多個節點 | 本機只有一個節點。每個節點一份的那個收集程式只跑過一個 |
| 真的 server 的磁碟 | k3d 的節點是一個 docker 容器。磁碟的過濾規則在真的節點上要再看一次 |
| Elasticsearch 開 TLS | 本機的沒開。正式的位址是 `https`，憑證怎麼帶沒有驗 |
| 佇列滿了會怎樣 | 只測了停 5 分鐘。停很久、佇列超過上限時丟哪些資料沒有測 |
| 第二座 Elasticsearch | 已定先一座。設定裡是註解 |
| 誰能看哪個部門的資料 | 資料流分開了，權限沒有設 |
| 用 Grafana 或 Kibana 看 | 這一步只用查詢確認 |

## 寫回文件

| 改了什麼 | 動到哪份文件 | 狀態 |
| --- | --- | --- |
| 新的範例程式、收集設定、測試腳本 | `README.md`、`platform/README.md`、`.agents/project/commands.md`、`requirements/README.md` | ☑ |
| 測試的架構與實測結果 | `requirements/architecture.html` 多一張「已實測」的圖與一節結果 | ☑ |
| 試驗用的設定 | `requirements/health-metrics-to-elastic/spike/` 刪掉，`2-spec.md`、`3-plan.md` 裡提到它的地方保留當紀錄 | ☑ |
