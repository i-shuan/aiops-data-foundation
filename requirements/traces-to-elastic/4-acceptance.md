# Acceptance — trace 經同一套收集程式進 Elasticsearch

> 每一項都是**可執行的指令加預期輸出**。都從 repo 根目錄、用 Git Bash 跑。
> cluster 是本機的 `obs-test`，要先裝 Operator，做法見 `.agents/project/commands.md`。2026-10-06 全部跑過。

## 第一批：範例程式、埋點設定、收集設定、靜態檢查

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 1-1 | `./tests/validate.sh \| tail -1` | `PASS` | ☑ |
| 1-2 | `./tests/validate.sh \| grep "trace："` | 9 種改壞的寫法每一條都是 `OK` | ☑ |
| 1-3 | `./tests/validate.sh \| grep -E "同時送很多批\|猜的容器"` | 兩條都是 `OK` | ☑ |
| 1-4 | `grep -c "opentelemetry" app/demo-svc/requirements.txt` | `0`。範例程式沒有裝任何送 trace 的套件 | ☑ |
| 1-5 | `kubectl --context k3d-obs-test -n demo-tsid get pod -l app.kubernetes.io/name=demo-web -o jsonpath='{.items[0].spec.initContainers[*].name}'` | `opentelemetry-auto-instrumentation-python`。埋點是 Operator 塞進去的 | ☑ |

## 第二批：k3d 實測

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 2-1 | `./tests/obs/run.sh` | 「分流」70 項都是 `OK`（數值 37、log 18、trace 15）；「弄壞看得到」9 項都是 `OK`；兩個 `PASS` | ☑ |
| 2-2 | `./tests/obs/run.sh outage` | 程式、server、log、trace 重啟前後八項都補回 95% 以上；log 沒漏沒重複；`PASS` | ☑ |
| 2-3 | `./tests/obs/run.sh restart` | `PASS` | ☑ |
| 2-4 | `./tests/obs/run.sh broken nospoof` | trace「每一段自己帶的沒有留下」`FAIL`；最後一行 `✔` | ☑ |
| 2-5 | `./tests/obs/run.sh broken retry0` | 四種資料都補回 0；最後一行 `✔` | ☑ |
| 2-6 | `./tests/obs/run.sh broken nopvc` | 只有 cluster 那一份的數值掉；最後一行 `✔` | ☑ |
| 2-7 | 跑完 2-4 到 2-6 之後 `python tests/obs/check.py routing` | `PASS` | ☑ |

### 對照 spec 的「行為」表

| spec 的情境 | 由哪一項驗 | 實際結果 |
| --- | --- | --- |
| 貼 `tenant: tsid` 的程式送 trace | 2-1 | `demo-tsid` 的 trace 只在 `tsid` |
| `tsid`、`bsid` 各一組 | 2-1 | `demo-bsid` 只在 `bsid` |
| 沒掛門牌的 namespace 送 trace | 2-1 | `fake-none` 的在 `platform` |
| 每一段帶標籤，值跟數值、log 相同 | 2-1 | `site`、`cluster` 一致；`dept` 是 `D3120` |
| 一次請求三段接得起來 | 2-1 | `demo-web` 收到 → `demo-web` 呼叫 → `demo-api` 收到，同一個編號 |
| 誰呼叫誰 | 2-1 | 查得出 `demo-web` 呼叫 `demo-api` |
| 把 `demo-api` 弄慢 400 毫秒 | 2-1 | `demo-api` 那一段 41 → 439 ms，`demo-web` 那一段 88 → 486 ms；變慢的那一筆 494 ms 裡 446 ms 花在呼叫 `demo-api` |
| 把 `demo-api` 弄成回錯誤 | 2-1 | 那一段標成錯誤；同一個編號在 trace 有 4 段，兩支程式的 log 都查得到 |
| 程式自己填 `tenant`、`site`、`cluster` | 2-1、2-4 | 不留在資料上 |
| 程式自稱是別的部門的 pod、別的 namespace | 2-1 | 自稱在 `demo-tsid` 的假程式：namespace 還是它實際所在的，pod 名稱是收集程式查到的 |
| 健康檢查 | 2-1 | 0 段 |
| pod 沒有加標註 | 2-1 | 假程式的 `fake`、`logger` 容器照常跑，沒有 trace |
| 停機再恢復，中間重啟收集程式 | 2-2 | 203 段：重啟前補回 196，重啟後補回 202 |
| 數值與 log 不受影響 | 2-1 到 2-3 | 前兩步的檢查照樣全過 |

### 實際量到的

| 項目 | 數字 |
| --- | --- |
| 平常 120 秒 | 程式 16 次抓取、server 4 次、log 60 行、trace 203 段 |
| 正確的設定，停 318 秒、中間重啟兩份收集程式 | 數值 16 / 16、4 / 4；log 60 / 60，146 行沒漏沒重複；trace 196 / 203、202 / 203 |
| 重試改成 0，停 302 秒 | 四種都是 0 |
| 佇列不放 PVC | cluster 那一份重啟前的數值 0 / 16；其餘在節點磁碟上，都補回 |
| 拿掉擋冒充 | 假程式的 trace 留著自稱的 `tenant`、`cluster`，各 54 段 |
| 範例程式的記憶體 | 掛埋點之前約 25 MiB，之後約 50 MiB |
| 多裝的東西佔的記憶體 | cert-manager 三個 pod 共約 150 MiB，Operator 約 35 MiB |

### 實作時才發現的

| 發現 | 怎麼處理 |
| --- | --- |
| Elasticsearch 的寫入程式只重試「連線被拒絕」，不重試「連線逾時」，逾時的那一批直接丟掉。看過它的原始碼確認。加了 trace 之後同時重試的連線變多，停機補送第一次跑掉了三到四成 | 改成每種資料一次只送一批（`num_consumers: 1`）。改完全過，節點那一份 0 次丟資料。`tests/validate.sh` 會擋下改回去 |
| 上面那一項只是緩解。真的網路中斷（機器沒回應、封包被吃掉）就是逾時，照樣會掉 | 這一步解決不了。寫進 `README.md`、`platform/README.md` 與架構圖，列在下面的「已知未涵蓋」，上線前要另開需求 |
| 試過加一層通用的重試（`retry_on_failure`）：設定收得下，但這個寫入程式沒有接上，沒有作用 | 拿掉，免得看起來有保護 |
| 掛了自動埋點的 pod 多一個初始化用的容器。程式吐的數值被標成那個容器的名字 | 抓來的數值不再帶容器名稱（收集程式本來就不知道是哪個容器），改用 `k8s.deployment.name`。加了靜態檢查 |
| trace 從程式送出到查得到要十幾秒 | 檢查多等一下再查。寫進 `platform/README.md` |
| 前兩步的停機補送當時是真的全過，但那是資料量小、沒碰上逾時 | 前兩步驗收文件的數字沒有錯，但「補得回來」的前提比當時以為的窄。這一份寫清楚 |

## 跟 spec、plan 不一樣的地方

| 不一樣 | 為什麼 |
| --- | --- |
| 多動了 `platform/otel/` 兩份收集設定的送出設定（`num_consumers`）與 `resource/scraped` | 實測發現的兩個問題，見上表 |
| `nospoof` 的對照只拿掉「每一段自己帶的」，沒有拿掉「程式自己說的 pod」 | 假程式不知道別人的 pod 的真實編號，冒充不出會成功的情況。那幾行由 `tests/validate.sh` 擋 |
| plan 寫完沒有另外等確認就實作 | 使用者確認 intent 時說了「好就執行」 |

## 已知未涵蓋

| 沒證明 | 為什麼 |
| --- | --- |
| Elasticsearch 連線逾時時不掉資料 | 目前會掉。寫入程式的限制，要另開需求（例如改走 Elasticsearch 自己的 OTLP 入口） |
| 用別人真實的 pod 編號冒充 | 見上表。只有靜態檢查 |
| 多個節點時 trace 只送給同節點 | 本機只有一個節點。只有靜態檢查 |
| Python 以外的語言、自動埋點不認得的框架 | 範例程式只有 Flask |
| trace 量很大時 | 每秒約 2 個請求 |
| 一次只送一批會不會在量大時跟不上 | 量小驗不出來 |
| 抽樣、保留多久、誰能看 | 不在這一步 |

## 寫回文件

| 改了什麼 | 動到哪份文件 | 狀態 |
| --- | --- | --- |
| 收集程式多收 trace、連線逾時的限制 | `README.md`、`platform/README.md`、`requirements/README.md` | ☑ |
| 裝 Operator 的指令 | `.agents/project/commands.md` | ☑ |
| 架構圖的進度與限制 | `requirements/architecture.html` | ☑ |
