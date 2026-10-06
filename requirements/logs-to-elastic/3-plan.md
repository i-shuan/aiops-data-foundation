# Plan — log 經同一套收集程式進 Elasticsearch

> `2-spec.md` 已確認（2026-10-06），四題都照建議。使用者同時說了開始做，所以這份 plan 寫完直接實作。
> 檔案數超過 3，沿用上一步的做法：分兩批，每批各自驗證、各一個 commit。

## 第一批：收集設定與靜態檢查

1. 先實測 spec 第 3 題：同名欄位型別不同時會怎樣。結果寫回 `2-spec.md`，再決定 JSON 拆到哪裡。
2. `node-collector.yaml`：加讀容器 log 的來源（排除收集程式自己的、讀到哪記在磁碟）、拆 JSON、統一等級與訊息兩個欄位、一條 log 的 pipeline。
3. 標籤規則加一段給 log 用的。兩份收集設定都加，維持一字不差。
4. `tests/validate.sh`：log 那一段也查擋冒充、對 pod、順序、讀到哪有沒有記在磁碟、有沒有排除自己。每一條加改壞的對照。

| 檔案 | 內容 |
| --- | --- |
| `platform/otel/node-collector.yaml` | 步驟 2、3 |
| `platform/otel/cluster-collector.yaml` | 步驟 3（只有規則那兩段） |
| `tests/validate.sh` | 步驟 4 |

## 第二批：k3d 實測與文件

1. `tests/obs/backends.yaml`：三個假程式各加一個會印 log 的容器：純文字、冒充加型別衝突、另一種欄位命名。
2. `tests/obs/check.py`：分流多查 log；弄壞多查錯誤 log 與 `trace_id`；停機補送多比 log 有沒有漏、有沒有重複。
3. `tests/obs/run.sh`：說明文字跟上。改壞的對照沿用現有三種，`nospoof` 要連 log 的擋冒充一起拿掉。
4. 全部重跑：分流、弄壞、停機補送、三種改壞的設定。數值那一條的檢查也要照樣全過。
5. 文件：`README.md`、`platform/README.md`，`architecture.html` 的狀態。
6. 刪掉 `platform/logging/fluent.conf`：驗收全過才刪。

| 檔案 | 內容 |
| --- | --- |
| `tests/obs/backends.yaml`、`check.py`、`run.sh` | 步驟 1 到 3 |
| `README.md`、`platform/README.md`、`requirements/README.md`、`requirements/architecture.html` | 步驟 5 |
| `platform/logging/fluent.conf` | 刪除 |

## 風險與退路

| 風險 | 怎麼發現 | 退路 |
| --- | --- | --- |
| 同名欄位型別不同，後到的 log 被 Elasticsearch 拒絕 | 第一批步驟 1；第二批的假程式故意製造 | JSON 不拆成各自的欄位，整包放進不分型別的欄位，只把等級、訊息、`trace_id` 另外取出來 |
| 停機補送後 log 重複 | 第二批：同一個序號出現兩次 | 查「讀到哪」是不是跟著佇列一起留在磁碟上 |
| 容器重啟或 log 檔輪替時漏掉或重讀 | 第二批：序號不連續 | 這一步只驗容器重啟。輪替在本機不容易製造，驗不到就列進「已知未涵蓋」 |
| 測試 cluster 上已經有十幾個小時的舊 log，第一次會灌進幾十萬筆 | 部署後 Elasticsearch 變慢、檢查逾時 | 檢查只看最近一段時間；必要時先等它讀完 |
| log 的 pipeline 拖慢數值 | 上一步的檢查開始不過 | 兩條 pipeline 各自的佇列分開看；必要時調高記憶體上限並寫進文件 |
| 收集程式讀到自己的 log，越印越多 | `obs` 的 log 筆數一直漲 | 排除的寫法由 `tests/validate.sh` 檢查，實測再確認筆數是 0 |

## 不在這個 plan 裡的

照 `2-spec.md` 的「不涵蓋」。另外：測試 cluster 上舊的 log 資料不清，留著。
