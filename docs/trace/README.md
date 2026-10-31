# Trace 進 Elasticsearch

> **類型：原理 Explanation**

應用把全部 trace 送給同節點的 Collector，Collector 依連線來源 Pod 讀 dept 分 group，再依 trace_id 交給 trace-sampler，等整個 trace 到齊才決定留不留：有問題的全留，正常的隨機留 5%，留下的用集中式資料寫入憑證寫進 ES 8.19。

## 資料怎麼走

1. 應用的 Java agent 或 .NET 自動埋點，以 OTLP 送到節點 Collector 的 Service，該 Service 限定只連同節點的 Collector。
2. 節點 Collector 做邊緣身份鑑別與標籤注入 (Edge Identity Sanitization & Injection)：移除應用自帶的 k8s、container、dept、group、data_stream 欄位，依連線來源 IP 對到 Pod，補上 namespace、Pod、Deployment、dept、版本，再把 dept 轉成 group，規則與 log、metrics 共用。
3. 例外事件的種類與訊息抄到 span 屬性。
4. 依 trace_id 分配到第二層 trace-sampler，共 2 台。
5. trace-sampler 等 30 秒再決定，並記下是哪條規則留的。
6. 用集中式資料寫入憑證 (Shared Ingest Token) 寫入，每個 group 一個資料流。AIOps 用唯讀 key 讀，人用各 group 的唯讀角色。

規則、台數、等待時間、隨機比例都在 `collector/code/`。log、metrics 走同一個 Collector，部門與 key 的規則共用。

## 關鍵決定與原因

- 應用只連同節點的 Collector：Collector 只記得本節點的 Pod，要靠連線來源 IP 對到 Pod。
- 移除應用自帶的資源欄位：應用可以自稱別的 namespace 或部門，移除後歸屬只看連線來源，無法冒充。
- 應用全送，`otel.traces.sampleRatio` 保持 `1.0`：在應用端一開始就抽樣，出事的請求也只剩抽樣比例，而場景一、三最需要的正是這些請求。
- 兩層，依 trace_id 分配：同一個 trace 的每一段要到同一台，才能判斷整個 trace。
- 等待 30 秒：必須比最長的請求長。等 10 秒時，下游很快、自己之後又做 12 秒的請求只留下 5%，因為決定時還看不到最上面那段；等 30 秒則全留。
- 例外抄到 span 屬性：ES 8.19 的 span 文件不存事件，事件另存成一筆 log。
- 以 trace ID 加 span ID 當文件 ID：重送不重複。

保留條件：錯誤、5xx、例外、超過 1 秒的全留，其餘隨機 5%。k3s 壓測每秒 500 個請求，其中 11% 有問題，有問題的 100% 留下且完整，總量剩約 15%，每台 trace-sampler 記憶體約 150MB。有問題的轉到另一個 problem 資料流，留 30 天，隨機的留 7 天（[保留天數](../retention.md)）。兩種分開存，baseline 只用隨機那份，所以不偏。測試環境隨機比例預設 100%。

## 欄位

存成 OTel 原生格式，用 ES 內建的 trace 模板。span 欄位是 ID、名稱、種類、耗時、狀態碼與屬性；資源欄位在 `resource.attributes` 底下，有服務名稱、版本、group、dept 與 Kubernetes 的 namespace、Pod、節點、Deployment、cluster。

服務名稱的決定順序：Pod 的 `app.kubernetes.io/name` label，其次應用自己設的 `OTEL_SERVICE_NAME`，最後 Deployment 名稱。Pod label 與 container 欄位只給 log 用，不帶進 trace。

## 跟 log、metrics 串

- log 與 trace：自動埋點把 trace 與 span 的 ID 放進 log 的 context，pipeline 存成 log 的 trace 欄位，兩邊互查。Java 照 SDK 的 logback 設定、.NET 輸出 JSON 的，都綁得起來。
- 純文字 log 沒印 MDC 的綁不起來。解法是 chart 設 `otel.logs.exporter: otlp`，log record 直接送 Collector，以來源欄位區分。代價是 stdout 與 OTLP 各存一份，所以只在需要時開。.NET Framework 不支援這條。
- 背景排程、Java 自建 Thread、.NET Kafka 消費端的 log 沒有 trace_id。
- trace 與 metrics 共用服務名稱、namespace、Pod 名稱。業務 ID 只放 log 與 trace，不放 metrics，因為每個值都會多一條序列。

## 部署與驗證

用 app-common chart 並開 Java agent 或 .NET 自動埋點的開關，送出位址與取樣設定由 chart 設好；沒用 chart 的服務自己設這些環境變數。手動埋點的時機與寫法見 [埋點指引](../../sdk/standards/tracing.md)。中央 ES 重跑 `elasticsearch-setup.sh`，新 key 才有 trace 資料流的權限。

驗證指令：`e2e/k8s/run-trace-e2e.sh`。23 項檢查涵蓋各 namespace 分流、應用自稱別的部門仍歸真實 namespace、欄位、與 log 互查、同一個 span 送 3 次只存 1 筆、各角色的讀寫權限。問題排除見 [疑難排解](../troubleshooting.md)。

## 六個場景

- ✅ 一、慢在哪：⑦ 每一段只算自己的時間，出事時與平常比，多最多的排第一；baseline 記每段的耗時。
- ✅ 二、程式壞味道：baseline 記每段每個請求呼叫幾次，k3s 抓到 N+1、重試風暴、快取穿透；單一呼叫平均 20 次以上的規則，連上線就有的 N+1 也抓得到。
- ✅ 三、錯誤源頭：標了錯誤、子呼叫都沒錯的那段是源頭。例外吞噬檢測 (Exception Swallowing Detection) 針對回 200 卻丟過例外的請求，5% 以上就報，baseline 也記這個比例。
- ⚠️ 四、非同步：排隊時間、消費端寫 DB 變慢、lag 抓得到，串不回是哪一筆訊息。
- ⚠️ 五、架構治理：有呼叫關係圖，出現沒看過的呼叫會報；缺循環呼叫與越權直連 DB。
- ❌ 六、工廠：不在目前範圍。SECS／GEM 需 EAP 手動開 span；MES、MCS、AMHS 之間傳不了 traceparent，改用工單號、載具 ID 串。

場景四用真 Kafka 加兩個 .NET 8 服務、不改程式測過。生產端的 HTTP 與 publish 在同一個 trace；消費端的 receive 另起 trace，只以 link 指回 publish；處理時的 DB 自成 trace（實測 961 筆全部沒有上層）。補法是排隊時間等於 receive 結束減 publish 結束，積壓看消費落後。限制：

1. 正常 trace 只留 5%，生產、消費兩邊都留下的很少，正式環境多半算不出排隊時間。
2. 查哪一筆訊息要靠 log 裡的訂單編號。
3. 要串起來，消費端得改約 5 行，用 header 的 traceparent 開處理 span；決定先不改。
4. Java agent、其他佇列、排程批次沒驗證。

場景一還沒做循序呼叫偵測，因為有些呼叫本來就有順序，誤報要先確認。

## 現況與沒做的

- ✅ 呼叫方與被呼叫方的時間差超過 500ms，記進 trace 的時鐘差欄位。
- ✅ 兩段佇列都寫磁碟。trace-sampler 被強制終止時，掉終止前 31 秒（等待時間加 1 秒）內該台收到的 trace；PVC 滿也掉。不用 Kafka 當緩衝，因為兩段磁碟佇列已讓 trace-sampler 換 IP、ES 停 30 分鐘、Collector 換版都不掉資料。
- ✅ ES 剛起來約 1 秒多，API key 被拒回 401。健康檢查要用 API key 打根路徑等 200，健康檢查不用 API key 時，流量會被導進還在拒絕的 ES，Collector 直接丟資料；中央 ES 前的檢查需管 ES 的人改。
- ✅ 工廠的批號、機台、工單、配方四個欄位名稱已定案，寫在埋點指引。
- ❌ 每天列出時鐘差最大的前 10 對服務，尚未做。
- ❌ 服務層的交易 ID（一次操作從頭到尾的編號，暫名 txn.id）還沒定名，許多 ID 只寫在 log 訊息裡，沒有獨立欄位；Kafka 不改程式、批次、排程斷掉的地方只能靠它接起來。
- ❌ .NET 不改程式時，消費端的 DB 呼叫接不回訊息。
- ❓ Java agent 的 Kafka header 自動串接未驗證。
- ❓ 資料是 OTel 原生格式，Kibana 的 Observability 畫面理論上能直接看，尚未確認。
