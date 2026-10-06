# Acceptance — log 經同一套收集程式進 Elasticsearch

> 每一項都是**可執行的指令加預期輸出**。都從 repo 根目錄、用 Git Bash 跑。
> cluster 是本機的 `obs-test`，建法見 `.agents/project/commands.md`。2026-10-06 全部跑過。

## 第一批：收集設定與靜態檢查

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 1-1 | `./tests/validate.sh \| tail -1` | `PASS` | ☑ |
| 1-2 | `./tests/validate.sh \| grep "log："` | 9 種改壞的寫法每一條都是 `OK`，後面寫著被哪一條擋下 | ☑ |
| 1-3 | `./tests/validate.sh \| grep "兩份的規則不一樣"` | `OK`。兩份收集設定給 log 用的規則不一樣時擋得下 | ☑ |

## 第二批：k3d 實測

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 2-1 | `./tests/obs/run.sh` | 「分流」55 項都是 `OK`（數值 37、log 18）；「弄壞看得到」四項都是 `OK`；兩個 `PASS` | ☑ |
| 2-2 | `./tests/obs/run.sh outage` | 程式、server、log 重啟前後六項都補回 95% 以上；log 沒有漏、沒有重複；`PASS` | ☑ |
| 2-3 | `./tests/obs/run.sh broken nospoof` | 數值與 log 的「自稱的值沒有留下」都 `FAIL`；最後一行 `✔` | ☑ |
| 2-4 | `./tests/obs/run.sh broken retry0` | log 補回 0；最後一行 `✔` | ☑ |
| 2-5 | `./tests/obs/run.sh broken nopvc` | 只有 cluster 那一份的數值掉；log 不受影響；最後一行 `✔` | ☑ |
| 2-6 | 跑完 2-3 到 2-5 之後 `python tests/obs/check.py routing` | `PASS`。改壞的設定有換回來 | ☑ |
| 2-7 | `ls platform/logging` | 不存在 | ☑ |

### 對照 spec 的「行為」表

| spec 的情境 | 由哪一項驗 | 實際結果 |
| --- | --- | --- |
| namespace 貼 `tenant: tsid` | 2-1 | `demo-tsid` 的 log 資料流、`tenant` 都只有 `tsid` |
| `tsid`、`bsid` 各一個 namespace | 2-1 | `demo-bsid` 只在 `bsid` |
| 沒貼、或貼成不在清單裡的值 | 2-1 | `fake-none`、`fake-typo` 的 log 都在 `platform` |
| 每筆帶 `tenant`、`site`、`cluster`，值跟數值相同 | 2-1 | `site` 都是 `ctsp`、`cluster` 都是 `obs-test`；`dept` 跟數值一樣是 `D3120`、`D9999` |
| 每筆帶 namespace、pod、容器的名稱 | 2-1 | 沒有 pod 名稱或容器名稱的 0 筆 |
| 一行 JSON 的 log | 2-1 | 等級、訊息、`trace_id` 都查得到 |
| 一行不是 JSON 的 log | 2-1 | `fake-none` 的純文字 log 查得到 |
| log 的 JSON 裡自己寫 `tenant`、`site`、`cluster` | 2-1、2-3 | `fake-spoof` 的 log 在 `bsid`，自稱的三個欄位都沒有留下 |
| 把 `demo-api` 弄成一半回錯誤 | 2-1 | 查得到 `demo-api` 的錯誤 log；同一個 `trace_id` 在 `demo-web` 也查得到 |
| 收集程式自己的 log | 2-1 | 0 筆 |
| 停機再恢復，中間重啟收集程式 | 2-2 | 停 318 秒：146 行，序號連續，沒有重複 |
| 收集程式平常重啟 | 2-2 | 同上那一輪裡重啟了一次，沒有漏也沒有重複 |
| 同名欄位型別不同 | 2-1 | `fake-spoof` 的 `code` 是文字，範例程式的是數字，兩邊的 log 都進得來 |
| 數值那一條不受影響 | 2-1、2-2 | 上一步的 37 項與停機補送照樣全過 |

### 實際量到的

| 項目 | 數字 |
| --- | --- |
| 平常 120 秒 | 程式 16 次抓取、server 4 次、log 60 行 |
| 正確的設定，停 318 秒、中間重啟兩份收集程式 | 數值 16 / 16、4 / 4；log 重啟前 60 / 60、重啟後 60 / 60；整段 146 行沒漏、沒重複 |
| 重試改成 0，停 297 秒 | 數值與 log 都是 0 |
| 佇列不放 PVC | cluster 那一份重啟前的數值 0 / 16；log 與 server 的數值在節點磁碟上，60 / 60、4 / 4 |
| 拿掉擋冒充 | `fake-spoof` 的 log 留著自稱的 `tenant`、`site`、`cluster`，各 42 筆 |

### 實作時才發現的

| 發現 | 怎麼處理 |
| --- | --- |
| 同名欄位型別不同時，Elasticsearch 兩筆都收，但後到那一筆的那個欄位查不到 | log 不會掉，所以不另外處理。寫進 `platform/README.md` |
| log 是讀檔案的，收集程式重啟的那幾十秒也補得回來。數值做不到：那段時間沒有去抓 | 停機補送對 log 多查「整段序號連續、沒有重複」 |
| 佇列不放 PVC 的對照只影響 cluster 那一份。log 在節點那一份，佇列在節點的磁碟上 | log 沒有對應的「佇列不見」對照。讀到哪有沒有記在磁碟，由 `tests/validate.sh` 擋 |
| `broken nospoof` 那一輪多出一項跟冒充無關的 `FAIL`：容器的 CPU「在 `tsid` 的 4 筆，全部 6 筆」 | 查過最近三小時，進錯部門的是 0 筆。是檢查分兩次查詢、中間進來新資料造成的。改成直接數「不在這個部門的」 |
| 測試 cluster 上有十幾個小時的舊 log，第一次啟動一分鐘進了 18 萬筆 | 檢查只看最近 90 秒，不受影響。正式上線的第一次要挑時間，寫進 `platform/README.md` |

## 跟 spec、plan 不一樣的地方

| 不一樣 | 為什麼 |
| --- | --- |
| 「容器重啟後新舊兩個容器的 log 都收得到」沒有驗 | 這一輪沒有製造容器重啟。列進下面的「已知未涵蓋」 |
| plan 寫完沒有另外等確認就實作 | 使用者確認 spec 時同時說了開始做 |
| 多動了 `requirements/logs-to-elastic/2-spec.md` 第 3 題 | 把實測結果寫回去 |

## 已知未涵蓋

| 沒證明 | 為什麼 |
| --- | --- |
| 容器重啟、log 檔輪替時不漏不重複 | 沒有製造。輪替在本機不容易做 |
| log 量很大時收集程式夠不夠力 | 只有每秒十行左右 |
| 讀到哪沒有記在磁碟時真的會重複 | 只有靜態檢查擋，沒有實跑對照 |
| 跨好幾行的 log | 已定不合併 |
| 節點本身的系統 log、trace | 不在這一步 |
| 多個節點、真的 server、正式環境 | 只在本機 k3d 的一個節點驗 |
| 佇列滿了丟哪些 | 沒測 |

## 寫回文件

| 改了什麼 | 動到哪份文件 | 狀態 |
| --- | --- | --- |
| 收集程式多收 log | `README.md`、`platform/README.md`、`requirements/README.md` | ☑ |
| 架構圖的進度 | `requirements/architecture.html` | ☑ |
| 舊的 log 設定 | `platform/logging/fluent.conf` 刪掉 | ☑ |
