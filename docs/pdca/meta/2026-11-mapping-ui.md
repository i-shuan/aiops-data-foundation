# 欄位對照（mapper）可以在網頁上調：存哪裡、進版會不會被洗掉（2026-11）

> ⚪ 構想，尚未實作（這份只有 Plan：使用者問「能不能 UI 設定、調完寫進 yaml、要不要資料庫、Dockerfile 內建資料庫進版會不會被清」）

**狀態**：Plan　　**日期**：2026-11-07　　**相關**：分支 `claude/mapping-ui`（從 `claude/source-adapter` 開：要用到那邊的來源適配器與自我檢查，那條還沒合回 master）
**模組**：②（讀進來的欄位對照）、⑧⑨（engine 的網頁）

## Plan：這一次要達到什麼

- **需求**：接一個新環境時，欄位名（服務名在哪個欄位、log 的 trace id 叫什麼、序列讀哪個索引）跟預設不一樣，現在要改 yaml、重 build image 或改 helm 的 `configOverrides` 再部署。希望人在網頁上調、馬上生效、留紀錄。
- **目的**：①能在網頁上看與改「可以用設定描述的對照」；②改完下一輪工作就用新的，不用重新部署；③改之前先對真的資料試（抽樣看欄位有沒有）；④誰、何時、從什麼改成什麼都留著，可以退回；⑤進版（換 image）不會洗掉。
- **範圍**：只做「設定描述得出來的對照」：歸屬與 log 的欄位（`common/scenario/it_*.yaml` 的 `owner`、`log`）、trace 的 `raw_index` 與來源、序列的 `index`／`by`／`scale`／`filter`。**不做**：來源適配器裡用程式寫的轉換（APM 的 `kind` 怎麼推、耗時單位）——那要改程式、寫測試，不適合網頁上改。

## 現況（查過的）

| 事實 | 在哪 |
|---|---|
| 設定檔跟著 image 走；要改用 helm 的 `configOverrides` 掛 ConfigMap 蓋掉，ConfigMap 在 pod 裡**唯讀** | `aiops-engine/helmchart/values.yaml`、`templates/env.yaml` |
| pod 的可寫空間是 `emptyDir`：pod 重啟就清空 | `templates/deployment.yaml` |
| 排程器每個工作都開一個新的程序跑，**每輪都重新讀設定** | `src/interface/jobs/runner.py` |
| 已經有「人在網頁核准、存在 ES、下一輪偵測直接吃」的先例：調整（`aiops-tuning`），yaml 是基準、ES 裡的調整蓋在上面 | `src/learn/store/tuning_repo.py`、`detect/series_ops/manual_adjust.py` |
| engine 已經有網頁（FastAPI＋HTML 模板） | `src/interface/web/` |

## 選項比較

| 存哪裡 | 進版會不會被洗掉 | 多個 pod 看得到同一份嗎 | 問題 |
|---|---|---|---|
| 網頁直接改容器裡的 yaml | **會**（pod 重啟就沒了，不用等進版） | 不會（各 pod 各一份） | ConfigMap 唯讀，寫不進去；寫到別處又沒人讀 |
| Dockerfile 內建 SQLite | **會**：新 image 帶一顆新的空資料庫；而且沒進版、只是 pod 重啟也會清 | 不會 | 要保住就得掛持久磁碟（PVC）；distributed 模式好幾個 pod 共用一顆 SQLite 不行（只能一個 pod 掛） |
| SQLite＋持久磁碟（PVC） | 不會 | 只有 single 模式行 | 多一個要備份、要遷移 schema 的東西 |
| **ES 一個新索引（建議）** | **不會**：ES 是另一個服務，換 engine 的 image 不碰它 | 會 | 沒有新的東西要養：engine 本來就讀寫 ES（`aiops-tuning` 就是這樣） |
| 網頁產生 yaml、開 PR 進 git、再部署 | 不會 | 會 | 最乾淨、有審核，但**不是即時**：要等合併與部署 |

**資料庫不一定大顆**：SQLite 只是一個檔案。問題不在大小，在**存活**（容器的檔案會被清）與**共用**（好幾個 pod）。這兩件事 ES 都已經解決了，所以不必另外加資料庫。

## 建議的做法：yaml 當基準、ES 存「蓋在上面的那一層」

```
image 裡的 yaml（git 管、PR 審）   ← 預設值、基準
        ＋
helm 的 configOverrides（ConfigMap）← 部署時就決定的
        ＋
ES 的 aiops-config（網頁改的）      ← 執行時調的，即時生效、有紀錄、能退回
        ↓  每輪工作開始時合併、用同一套 Pydantic 模型驗證
     這一輪用的設定
```

1. **新索引 `aiops-config`**：一筆一個覆寫（例如「`it_apm` 的 `owner.service` 改成 `labels.app`」），欄位：哪份設定、哪個路徑、新值、舊值、誰、何時、原因、狀態（生效／停用）。只存**差異**，不存整份 yaml。
2. **讀的時候合併**：`load_config`、`load_scenario` 讀完 yaml 再套 ES 裡生效的覆寫，**用同一套模型驗證**；覆寫不合法就略過那一筆、印出來（跟自我檢查一樣，不讓一筆壞覆寫擋住偵測）。ES 連不上就只用 yaml。
3. **網頁**：列出目前生效的值與**來源**（yaml／configOverrides／網頁）；改之前按「試一下」：用 `data/source/check.py` 對真的資料抽樣，顯示這個欄位在幾筆文件裡出現；存檔；可以停用（退回 yaml 的值）。
4. **折回 git**：網頁上有「匯出成 yaml」：把生效的覆寫合進對應的 yaml 下載（或之後開 PR）。穩定的對照應該進 git，不要永遠住在 ES。
5. **進版**：engine 換 image 不碰 ES，覆寫留著。風險是**新版的 yaml 改了同一個欄位**：合併時覆寫仍會蓋過新預設——網頁要標出「這筆覆寫的舊值跟現在 yaml 的值不一樣了」，讓人決定要不要停用。

## 要先問使用者的事

1. **誰可以改？** engine 的網頁現在沒有登入（回饋表單是誰都能填）。對照改錯會讓偵測靜默停擺，要不要只有特定的人能改（例如走 Grafana 的登入、或 API key）？
2. **改了要不要人核准**（像 `aiops-tuning` 的 proposed → active）？還是改了就生效？
3. **第一版要做多大**：只做歸屬與 log 的欄位（最常因環境不同要改的），還是序列也一起？

## Do／Check／Act

還沒開始。
