# 欄位對照（mapper）可以在網頁上調：存哪裡、進版會不會被洗掉（2026-11）

**狀態**：已完成第一版（歸屬與 log；要核准）　　**日期**：2026-11-07　　**相關**：分支 `claude/mapping-ui`（從 `claude/source-adapter` 開：要用到那邊的來源適配器與自我檢查，那條還沒合回 master）
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

## 使用者的決定

要核准；第一版先做歸屬與 log。「誰可以改」沒有另外指定，所以照調整（`aiops-tuning`）的做法：**誰都能提**（網頁、命令列），**核准只在命令列**（有權限進 pod、跑排程的人）。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `src/common/scenario/mapping.py`（新） | 索引 `aiops-mapping` 與 mapping、開放的欄位（`EDITABLE`：owner 5 個、log 6 個）、`check_value`、`apply`（把生效的覆寫蓋到情境上，回新的，不動原本的）、`MappingRepository`、`effective`（讀生效的覆寫並套上；讀不到照 yaml；常駐程序 60 秒內重用） | 覆寫是「情境資料檔」的一層，跟情境放在一起；`common` 不依賴功能資料夾 |
| `src/data/mapping_service.py`（新） | `current`（每個欄位：yaml 的值、現在用的、被誰覆寫）、`try_value`（對真的資料抽樣）、`propose`、`approve`（核准前再試一次，對不上不給，`force` 才行；同一個欄位舊的生效那筆改 disabled）、`set_status`（reject、disable） | 「讀進來時欄位叫什麼」是 ② 清資料的事；試的時候用階段 2 的 `data/source/check.py` |
| `src/detect/store/anomaly_repo.py`、`src/incident/store/log_repo.py` | 讀歸屬、讀 log 時用 `effective(self.scenario, self.es)` | 只有這兩處用 owner、log 的欄位名；影響範圍就鎖在這兩處 |
| `src/interface/cli/mapping.py`（新） | `detector mapping list／try／propose／approve／reject／disable` | 核准在命令列 |
| `src/interface/web/`（`routes.py`、`views.py`、`templates/mapping.html`） | `GET /mapping`（現在生效的、提過的）、`POST /mapping`（先試不存、提出待核准） | 網頁只能看與提 |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 只開放歸屬與 log、值寫錯就擋 | `tests/test_mapping.py::Validate` | 未開放的欄位、`series.*`、空值、空清單都擋 |
| 只有核准的才生效、yaml 不被動、ES 掛了照 yaml、壞的一筆不擋其他筆、並存時改得到次要來源 | `Apply` | 都如預期 |
| 流程：提了不生效、核准後生效、資料裡沒有的欄位不給核准、新核准停用舊的、停用回到 yaml、狀態不能亂跳 | `Flow` | 都如預期 |
| 網頁：看得到、先試不存、提出、錯的回 400 說原因 | `Page`（FastAPI TestClient） | 都如預期 |
| **真的 ES** | `tests/mapping_e2e.py`：APM 合成資料，把部門從 `labels.dept` 改成 `labels.group`，每一步之後跑一次 `detector rules`、看新寫的異常的 `dept` | 沒覆寫 D3120 → 提了沒核准 D3120 → 核准後 **mes** → 資料裡沒有的欄位核准被擋 → 停用後回到 D3120；`mapping list` 看得到三筆的狀態。全部通過 |
| 其他沒壞 | 全測試（含 helm） | 870 個過 |

## Act

- **網頁沒有登入**：誰都能「提」，但提了不生效；核准只能在命令列，所以「能改的人」＝能進 pod 的人。要網頁上核准，得先有登入或 API key。
- **只做了歸屬與 log**；序列（`series.yaml`）、trace 的 `raw_index` 還要改設定檔（第二版）。
- **`log.problem_levels` 是值不是欄位名**，抽樣看不出對錯，核准時會列成「試不了」，要人自己確認（`--force`）。
- **log 的主要來源**：試的時候用情境資料檔裡的 `log.index`；但實際讀 log 的索引是 `rca.yaml` 的 `indices.logs`，兩者不一樣時，試的結果不代表實際。
- **換版後**，新的 yaml 若改了同一個欄位，覆寫仍會蓋過去；網頁會並列「現在用的」與「yaml 的值」，但沒有特別警告「yaml 在你核准之後改過」。
- **沒有「匯出成 yaml」**（Plan 寫了，第一版沒做）。
