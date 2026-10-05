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

實作時補上逐條的指令。要涵蓋：

| 要驗的 | 預期 |
| --- | --- |
| `./tests/validate.sh` | `PASS`；允許的 `tenant` 是從 `platform/otel/` 讀出來的 |
| 重試改成 0 | 自我檢查擋下 |
| 拿掉「先丟掉程式自己帶的」 | 自我檢查擋下 |
| 拿掉對 pod 的那一段 | 自我檢查擋下 |
| server 沒有標 `icsd` | 自我檢查擋下 |
| 密碼寫在檔案裡 | 自我檢查擋下 |

## 第三批：k3d 實測與文件

實作時補上逐條的指令。要涵蓋 `2-spec.md`「行為」表的每一列，加上三種改壞的設定都會讓測試 FAIL。

## 跟 spec、plan 不一樣的地方

| 不一樣 | 為什麼 |
| --- | --- |
| 範例程式只有一個資料夾 `app/demo-svc/`，不是 `app/demo-web/` 與 `app/demo-api/` 兩個 | 兩支的程式碼幾乎一樣，只差「要不要呼叫下一支、要不要自己產生流量」。寫成同一支程式用環境變數切換，免得兩份程式碼各自改、越改越不一樣 |
| 一次部署會同時起 `demo-web` 與 `demo-api`，在同一個 namespace | 兩支分開部署的話，第二支會因為 namespace 已經存在而失敗。一起部署也比較符合「一組互相呼叫的服務」 |

## 已知未涵蓋

| 沒證明 | 為什麼 |
| --- | --- |
| 範例程式的 log 與 trace 有被收走 | 這一步只確認它有吐。總綱第 2、4 步才收 |
| trace 不是真的送到某處 | 目前只有編號在 header 與 log 裡傳。真的送出去要等第 4 步 |
| 正式環境 | 只在本機 k3d 驗 |

## 寫回文件

| 改了什麼 | 動到哪份文件 | 狀態 |
| --- | --- | --- |
| 新的範例程式 | `README.md`、`.agents/project/commands.md` | 第三批一起改 |
