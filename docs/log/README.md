# Log 進 Elasticsearch

> **類型：原理 Explanation**

節點 Collector 只負責收 log 並依部門分流，欄位整理全部在中央 ES 的 pipeline 做；改解析規則只動中央，不動各 cluster。

## 資料怎麼走

節點 Collector 與 metrics 共用，讀 `/var/log/pods`，補上 pod label、dept、cluster，再把 dept 對應成 group。elasticsearch exporter 把資料寫進 `logs-generic.otel-<group>`。中央 pipeline `k8s-logs-otel` 整理形狀、算 `_id`、轉到 `k8s-<group>-<cluster>`，接著 `k8s-logs` 做共通身分，依 `log-format` 分派解析，再統一欄位。

- 應用自己填的 K8s 與部門欄位會被移除，由 Collector 依 Pod 補上（邊緣身份鑑別與標籤注入），避免應用冒充別的服務或部門。
- `_id` 由 Pod、容器、stream、時間與內容算出，重送的資料 ES 回 409 擋掉，所以不會重複。
- 轉進 `k8s-<group>-*` 時，ES 再驗一次 key 權限，IT 的 key 轉到 IMC 得到 403。
- 任何一步出錯，原文照樣入庫，錯誤另記在欄位裡。
- 中央 pipeline 也接受 Fluent Bit 的資料，邊緣 VM 可以用 Fluent Bit，欄位 `agent.type` 記錄是誰送的。

## 關鍵決定與原因

1. 解析規則放中央。收集器每個 cluster 一套，改規則要每廠重新部署；放中央只要跑一次 `elasticsearch-setup.sh`，全廠同時生效。
2. 依服務宣告的格式分派。Java 的 JSON、Go 的鍵值對、設備通訊的 SECS、各家 REST API 各寫各的，無法統一，所以服務在 pod 加 label `log-format`，沒宣告就走 `default`。宣告了但中央還沒有規則時，退回 `default` 並保留原文，此時實際解析規則與宣告的格式兩個欄位不同，可以據此找出缺規則的格式。
3. 應用自帶的欄位收進 `app`。數百個服務共用同一個 index，攤平在最上層會撞到欄位數上限，整個 index 拒收；改用 flattened 型別只佔一個欄位，仍然能查。
4. 以 OTel Collector 取代 Fluent Bit。比較環境是單節點 k3s、ES 9.5.5，兩者讀同一批檔案；同一行 log 兩邊各一筆，比對 444 對，欄位全部一致；每秒 2,000、5,000 行都沒有遺失與重複；ES 停機或當機後補送，都沒有遺失與重複；收集器重啟兩次，Fluent Bit 遺失 342 筆，OTel 遺失 0；每節點記憶體 Fluent Bit 約 40MB，OTel 單獨跑約 250MB，併入 metrics 的 Collector 後只多約 75MB。

選 OTel 的原因有二：log、metrics、trace 共用一種收集器和同一份分部門規則，部門清單少一處要同步；重啟不掉 log。Fluent Bit 的重啟遺失出在它自己的關機流程，加長等待時間之後仍會掉 5～8 筆，設定改不掉。

key 失效時資料留在送出佇列：`retry_on_status` 含 401，重試到 key 換好；佇列滿（2,000 萬筆）才丟。換 key 仍先發新的，確認每個 Collector 換上之後才作廢舊的。409 是去重造成的，屬正常，丟棄數要看錯誤類型，不能直接當遺失告警。

## 規格

- 時間：`@timestamp` 存 UTC。沒帶時區的依環境變數 `LOG_DEFAULT_TIMEZONE`（預設 Asia/Taipei）解讀，並把用過的時區記下來，方便找出沒帶時區的服務。
- level 統一成 trace 到 fatal 六級，不認得的值轉小寫保留，不硬塞進六級。
- level 推斷：沒有 level 時，取行首前綴，或前 80 字元內獨立的全大寫關鍵字，並標 `log.level_inferred: true`。推斷可能誤判，所以 AIOps 對這類資料降低權重。
- `error.type` 取自 stack trace 第一行，讓例外可以分群，不必比對全文。
- alias 讓 baseline、quality、RCA 與 metrics、trace 用同一條路徑查；alias 不存在 `_source` 裡，程式要用 `fields` 取值。
- `log.source` 區分來源是容器 log 檔或應用直送的 OTLP；服務兩種都開時，同一行有兩份，靠這個欄位分開。
- trace_id 轉小寫、去空白，短的 16 位 id 前面補零。空字串與全 0 視為沒有；長度不對或非 hex，不設 `trace.id`，原值另存，方便找出印錯 trace_id 的服務。只取欄位，不從訊息內文猜，因為內文裡的 hex 不一定是 trace。
- Index 名稱帶 group，因為權限依 group 切；帶 cluster，因為某一廠要整批清掉或調保留天數時不影響別廠。cluster 名稱要小寫。原文保留 30 天；每 5 分鐘各服務與 level 的筆數另存 400 天，見 [retention.md](../retention.md)。

## 操作

- 部署：先在中央跑 `elasticsearch-setup.sh`，再到每個應用 cluster 建 Secret `es-writer-keys` 並部署 Collector，最後替 namespace 標上 `dept`。標之前的資料不歸任何部門。
- Secret：encoded key 是 60 個字元，多 1 個就是結尾多了換行，之後一直 401。編輯時用 key/value 模式，用 YAML 編輯會整份取代，沒貼到的 key 會消失。存檔後約 1 分鐘，Collector 自動換用新 key，不必重啟。
- 換 key：用 telemetry 的換 key 腳本，把該 key 用到的所有 cluster 全部列上；任何一個 5 分鐘內沒換到，腳本就停下來，不作廢舊 key。直接作廢再換 Secret，401 期間的資料留在佇列，換好後補送。
- 從 Fluent Bit 換過來：先確認新收集器的資料已進來，再移除 Fluent Bit。並行期間每行會有兩筆。
- 宣告格式：用 app-common chart 的服務填 `logFormat`，其他服務自己加 pod label。可以先宣告，之後再補規則，這段期間 log 照收、原文完整，只是沒拆欄位。
- 新增一種格式：
  1. 在 `telemetry/storage/formats/` 加規則檔，第一步設 `log.parser`，解析失敗要保留原文。
  2. 在 `e2e/local-k3s/` 加輸出該格式的測試服務，並跑 端到端腳本驗證。
  3. 重跑中央的安裝腳本，各 cluster 的 Collector 不用動。
  4. 服務填 `logFormat`。

  一則訊息跨多行的格式（SECS）要另在 Collector 加多行合併，因為送到中央就拼不回來。
- 驗證：`run-e2e.sh` 在 ES 8.19.23 上 54 項通過。每秒 10,000 行沒有遺失；20,000 行沒有遺失，但晚到約 2 分鐘。寫入 index 大過 ES 記憶體時，查重會停住，所以 ILM 要依大小 rollover。
- 被 pipeline 拒收的文件進 failure store，只要出現一筆就告警。排錯見 [troubleshooting.md](../troubleshooting.md)。

## 現況與沒做的

- ✅ 依 dept 分流與換 key：實測換 key 期間 59,714 筆沒有遺失，也沒有重複。
- ✅ 格式解析與 level 推斷：24 個案例通過。
- ✅ 收資料延遲（p95 逾 1 分鐘）與時鐘偏差（逾 5 分鐘）告警：延遲 3、20 分鐘與時區錯誤的案例都報。
- ⚠️ 時區規範（寫 UTC 或帶時區）還沒寫進應用標準。
- ✅ ES 剛起來約 1 秒回 401：`retry_on_status` 含 401，資料留在佇列；入口健康檢查建議用 API key，需 ES 管理者處理。
- ⚠️ 部門清單在 `groups.yaml` 與 `common.yaml` 手動同步兩處，group 變多之前要改成由 `groups.yaml` 產生。
- ❌ SECS 的解析規則與多行合併：缺一般事件、告警、逾時、多行 body 的真實樣本。
- ❌ REST API 的解析規則：缺兩三個服務的真實 log 樣本。
- ❌ 從 log 抽業務 ID：規劃見 [trace/README.md](../trace/README.md)。
- ❌ K8s events：event 不帶 dept，歸屬未定。

整體架構與各模組現況見 [architecture.md](../architecture.md)。
