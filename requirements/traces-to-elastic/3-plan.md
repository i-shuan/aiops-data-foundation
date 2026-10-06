# Plan — trace 經同一套收集程式進 Elasticsearch

> `2-spec.md` 寫完直接實作：使用者確認 intent 時說了「好就執行」。
> 檔案數超過 3，分兩批，每批各自驗證、各一個 commit。

## 第一批：範例程式、埋點設定、收集設定、靜態檢查

1. 測試 cluster 裝 cert-manager 與 OpenTelemetry Operator，版本釘死。指令寫進 `.agents/project/commands.md`。
2. 範例程式：收請求改用 Flask，拿掉手寫傳編號，log 的編號讀自動埋點的。chart 加標註，可以用 values 關掉。
3. `platform/otel/instrumentation.yaml`：自動埋點的設定。
4. 節點那一份：開入口、加 Service（只轉同節點）、加 trace 的 pipeline。
5. 標籤規則加一段給 trace 用的：先丟掉程式自己填的身分與 pod 資訊，改看連線來源。兩份收集設定都加。
6. `tests/validate.sh`：查 trace 那一段的擋冒充、Service 只轉同節點、pipeline 的順序。每一條加改壞的對照。

| 檔案 | 內容 |
| --- | --- |
| `app/demo-svc/main.py`、`requirements.txt`、`k8s/values.yaml`、`k8s/templates/deployment.yaml` | 步驟 2 |
| `platform/otel/instrumentation.yaml` | 步驟 3 |
| `platform/otel/node-collector.yaml`、`cluster-collector.yaml` | 步驟 4、5 |
| `tests/validate.sh`、`.agents/project/commands.md` | 步驟 1、6 |

## 第二批：k3d 實測與文件

1. `tests/obs/backends.yaml`：冒充的假程式多一個容器，自己送 trace，自稱是 `tsid`、自稱是 `demo-tsid` 的 pod。沒掛門牌的假程式也送一份。
2. `tests/obs/check.py`：分流多查 trace；弄壞多查「慢在哪一段」「錯誤那一段」「同一個編號串到 log」；多查「誰呼叫誰」；停機補送多比 trace 的筆數。
3. `tests/obs/run.sh`：部署前檢查 Operator 有沒有裝；套用埋點設定；`nospoof` 連 trace 的擋冒充一起拿掉。
4. 全部重跑：分流、弄壞、停機補送、容器重啟、三種改壞的設定。數值與 log 的檢查照樣全過。
5. 文件：`README.md`、`platform/README.md`、`requirements/README.md`、`architecture.html`。

| 檔案 | 內容 |
| --- | --- |
| `tests/obs/backends.yaml`、`check.py`、`run.sh` | 步驟 1 到 3 |
| `README.md`、`platform/README.md`、`requirements/README.md`、`requirements/architecture.html` | 步驟 5 |

## 風險與退路

| 風險 | 怎麼發現 | 退路 |
| --- | --- | --- |
| 看連線來源對不到 pod（位址被轉換過） | 第二批：trace 全在 `platform` | 已實測在本機對得到。多節點時要靠 Service 只轉同節點，由靜態檢查擋 |
| 假程式用很陽春的工具送 trace，格式不對被拒絕 | 第二批：冒充的那一份沒有資料，檢查等於沒測 | 檢查裡先確認「冒充的 trace 有進來」，沒有就 FAIL |
| 收集程式重啟時 trace 掉一些，停機補送比不過 | 第二批 | 比的時段不含重啟的那幾十秒，跟數值一樣 |
| Flask 內建的伺服器撐不住自己打自己的流量 | 範例程式的處理時間變長、健康檢查失敗 | 每秒 2 個請求，量很小。不行再換正式的伺服器 |
| 自動埋點讓範例程式的記憶體超過上限 | pod 被重啟 | 調高 chart 的上限並寫進文件 |
| 上一步的 log 檢查因為 `trace_id` 的來源換了而壞掉 | 第二批：log 的檢查 FAIL | 欄位名稱沒變，只是值的來源換了。壞了就照實修 |

## 不在這個 plan 裡的

照 `2-spec.md` 的「不涵蓋」。
