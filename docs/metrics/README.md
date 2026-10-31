# Metrics 進 Elasticsearch

> **類型：原理 Explanation**

每個 cluster 一套 OpenTelemetry Collector 收 metrics，依每筆資料自己的 namespace 讀 dept 分 group，用集中式資料寫入憑證 (Shared Ingest Token) 寫進 Elasticsearch 8.19 的時序資料流。分組規則與 log 相同，同一個 Pod 的資料落在同一個 group。

## 資料怎麼走

- 節點 Collector 每 30 秒收 kubelet 的節點、Pod、容器指標，cAdvisor 的 CPU 限流兩個數字，Java 的 Prometheus 端點，.NET 推來的指標，以及時鐘偏移。
- cluster Collector 收 Pod 階段、重啟次數、副本數、節點狀態。
- 兩種 Collector 共用同一份設定：先做邊緣身份鑑別與標籤注入，拿掉應用自帶的部門與 group 欄位；再依資料所屬的 Pod 或物件讀 namespace 的 dept label，對應到 group；沒有部門的資料（節點本身）進 unassigned。
- 出口是 Collector 的 elasticsearch exporter，寫入 `metrics-<來源>.otel-<group>`，來源有四種，每個 group 四條資料流。AIOps 用唯讀 key 讀取。

## 關鍵決定與原因

**依資料自己的 namespace 分 group，不依抓取對象。** 原設計依抓取對象所在的 namespace 分。Pod CPU 從節點 kubelet 抓、Pod 狀態從 kube-system 抓，結果 IT 的 Pod CPU 與重啟次數全進 platform，IT 看不到。該做法另要求 operator 與 vmagent 升到指定版本以上，舊版 vmagent 起不來。

**用 exporter，不用 OTLP。** ES 8.19 沒有 ES 9 的 OTLP 入口，exporter 寫出的資料流名稱與欄位和 OTLP 入口相同。exporter 預設會丟資料，所以改三處：重試從 2 次改成一直重試；401 加入可重試的狀態，因為 ES 剛起來約 1 秒內 key 會被拒，整個請求沒有寫入，重送不會重複；逾時期限設為不限。ES 無回應 90 秒掉 0 筆。ES 的 IP 消失時 exporter 不重試，180 秒停機掉約 3%，所以 ES 前要有 LB 加健康檢查，檢查要帶 API key，需管 ES 的人修改。

**直方圖換增量，sum 另存。** Prometheus 直方圖從啟動累加，ES 無法頭尾相減。Collector 換成每 30 秒的差值，可直接加總任一區間。ES 8.19 的直方圖欄位只存各區間的值與次數，沒有 temporality，也不存總和，所以把總和另存為 `<名稱>_sum`。累計值不換。代價是 Collector 重啟後，每條序列首次抓取沒有前值，該 30 秒遺失；應用重啟歸零視為重新開始。

**Java 指標名稱在進 ES 前統一。** node Collector 把 HTTP 請求耗時改成 OTel 名稱，狀態碼改數字，只改舊名，所以 Java agent 之後自己改用新名稱也不會改錯。baseline 因此不需要兩套寫法。

## 欄位與資料結構

一筆文件是「時間點加一條序列身分」，含身分與單位相同的所有指標，一個 Pod 每 30 秒約十餘筆。`_id` 由序列身分加時間決定，重送不重複。可寫入時間往後放寬 2 小時，ES 停機後補送的資料才收得進。

- 數值放 `metrics.<名稱>`，維度放 `attributes.*`。
- 服務身分在 `resource.attributes` 的服務名稱欄位，分流結果在 group 與 dept 欄位。
- 維度每組不同的值就是一條序列，所以交易 ID、批號不可放進 metrics，序列數會爆。
- 量測值（gauge）可直接平均；累計值（CPU 累計時間、網路流量、重啟次數）要先算差值。
- 查指標要先過濾 `exists`，因為同一資料流混有多種文件。

## 收了什麼、哪些有 baseline

kubelet 提供 CPU、記憶體、磁碟、網路用量。k8s_cluster 提供重啟、Pod 階段、副本、節點狀態。Java agent 提供 HTTP 延遲直方圖、JVM 記憶體與 GC、HikariCP 連線池。.NET 提供 HTTP、runtime、Npgsql 連線池。Kafka 落後筆數由 kafka_exporter 提供。

有 baseline 的 metrics 共 5 條：請求數、錯誤率、p95（依服務、方法、路由切），Pod CPU、Pod 記憶體（依 Deployment 切）。重啟、副本、磁碟、JVM、連線池、p99 沒有 baseline，由靜態臨界值規則判斷（[規格](../../engine/configs/baseline/reference.md)）。

## metrics 能做到與做不到

五種做法中，四種已做：動態基準線學習處理有週期的請求數、延遲、CPU（5 條）；靜態臨界值規則處理有上限的記憶體、磁碟、連線池；Pod 同儕橫向比對處理一個 Pod 壞而被平均稀釋的錯誤率、p95、CPU、記憶體；狀態變化處理重啟、副本不足、節點 NotReady，k3s 的 5 種壞法都抓到、正常時不誤報。第五種是趨勢，預測緩慢上升的指標幾天後到上限，需要數天資料，尚未做。

請求、錯誤、延遲只說明何時出事；把連線池、GC、磁碟、重啟、版本更換對時間，才指得出原因種類。做不到的有四項：

- 慢在請求哪一段、呼叫幾次：metrics 只有整體數字，靠 trace。
- 錯誤被吞、狀態碼 200：靠 log 與例外吞噬檢測。
- 哪批貨、哪台機台：批號不能當維度，靠 trace 屬性。
- 從資料點跳到 trace：exemplar 寫入 ES 時被丟（ES 9.5.5、8.19.23 實測），改以服務加時間範圍查。

## .NET 與 Java

.NET 服務多為舊專案、分屬不同部門，無法要求改程式，所以 chart 開 `otel.dotnet`，掛自動埋點。自動埋點的 Prometheus 端點只聽 localhost，Collector 抓不到，只能推。.NET Framework 4.x 只跑 Windows，需先移植到 .NET 6 以上。

- 收法：Java agent 在 9464 開端點由 Collector 拉；.NET 每 30 秒推到節點 Collector。
- 部門判斷：Java 看 Pod 的 namespace；.NET 看連線來源 IP 對應的 Pod。
- HTTP 指標：Java 進 ES 前改名，與 .NET 同名；.NET 預設沒開，chart 要加環境變數。
- runtime 指標：名稱依版本不同，.NET 8 與 9 各有一組規則，一個服務只命中其一。heap 上限與連線池排隊數沒有指標。

## 部署與驗證

1. 中央 ES 跑 `elasticsearch-setup.sh`，與 log 共用，重跑才讓新 key 有 metrics 權限。
2. 應用 cluster 要有 `es-writer-keys` secret，改 `telemetry/collectors` 底下 kustomization 的 cluster 名稱、ES 位址、image，再套用。
3. Java 用 app-common chart 自動加註記；.NET 開啟 chart 的 `otel.dotnet`。
4. 驗證跑 `run-metrics-e2e.sh`，46 項通過，涵蓋分流、欄位、直方圖、權限。改 namespace 的部門 label 不用重啟 Collector，2 至 17 秒生效。

案例結論（k3s 實測）：CPU 平均只有 limit 的 35%，卻有 50% 的週期被限流；Java 洩漏看 GC 之後的老年代用量，比 OOM 早約 4 分鐘發現，因為只看用量會被尚未回收的垃圾誤導。JVM 要加 `-XX:+ExitOnOutOfMemoryError`，否則 OOM 後程式不結束，K8s 不重開。排錯見 [troubleshooting](../troubleshooting.md)，保留見 [retention](../retention.md)。

## 現況與未做

- ✅ 收值、分 group、直方圖增量：錯誤率與 p95 算得出來。
- ✅ 保留：`metrics-generic`（應用直方圖）原始資料 90 天，不降解析度，因為 ES 8.19 的 `histogram` 降解析度時不合併；其他 metrics 原始 14 天，之後 5 分鐘一點，第 90 天起 1 小時一點，第 400 天刪（[retention](../retention-reference.md)）。
- ✅ 節點時鐘：偏移超過 100ms 警告、超過 1 秒嚴重。
- ⚠️ 往外 HTTP 呼叫耗時：測試程式沒有往外呼叫，未驗證。
- ❌ 趨勢、p99 baseline、節點系統指標。
- ❌ 真實 Kafka、機台指標、OEE、DB 慢查詢。
- ❌ .NET 的 SQL Server 連線池、.NET 6、Alpine。
