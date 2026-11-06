# 半導體（FDC）：沒有 trace id 時怎麼做 AIOps

> **類型：原理 Explanation**　目前的設計見 [architecture.md](architecture.md)・五個模型的輸入規格見 [models/README.md](pdca/meta/2026-11-target-architecture.md)・資料怎麼餵進來見 [sources/README.md](../aiops-engine/src/core/repository/README.md)
> 🟡 部分實作：工廠版 engine（`configs/factory/`）已用假資料整條跑通，怎麼跑、要給什麼資料見 [configs/factory/README.md](../aiops-engine/configs/factory/README.md)；沒有 MES 歷程、FDC 的欄位樣本，欄位名是假設，拿到樣本要重對。現況見文末。

目前的設計是一般服務（Web API、worker、Kafka、DB）：靠 Elastic APM 的 trace id 把「一個請求經過哪幾段」串起來。半導體的 FDC（Fault Detection and Classification）沒有 trace id，但有同樣功能的鍵：**lot（或 wafer）的加工路徑**。這份說明路徑怎麼取代 trace、五個模型各要改什麼、跟目前設計差在哪。

## 核心想法

| IT（目前） | 半導體 | 說明 |
|---|---|---|
| 一個請求 | 一個 lot（細到 wafer） | 被追蹤的單位 |
| trace id | lot id（wafer id） | 把一批東西串在一起的鍵 |
| span（一段） | 一個 step：lot 在某機台、某 chamber、跑某 recipe step 的那段 | 路徑的一格 |
| service | 機台、chamber | 出事的地方 |
| span name | recipe ＋ step | 做什麼 |
| 耗時 | 加工時間 | 每段自己花多久 |
| 錯誤 | OOC／OOS、FDC fault | 這一段「壞了」 |
| 慢在哪一段 | 壞 lot 跟好 lot 的路徑比，哪一步差最多 | 定位 |

**最低門檻是 MES 歷程表**（lot、機台、chamber、recipe、step、開始、結束）。FDC 的 run summary 要帶 lot id 與機台 id，才對得回路徑。

| 手上有什麼 | 能做到 | 做不到 |
|---|---|---|
| 只有 FDC 感測值、量測值 | 發現異常（基準線、規則、機台互比） | 不知道壞的 lot 壞在哪一步、哪台機台牽連哪些 lot |
| ＋ lot 路徑（MES 歷程） | 定位：壞 lot 與好 lot 的路徑比；共同點（壞的都過 chamber B） | — |
| ＋ 圖（多個 lot、多件事故） | 共用根因（同一條管路、同一版 recipe）、跨事故關聯 | — |

## 跟目前設計的對照

### ①～⑨ 模組

| 模組 | 目前（一般服務） | 半導體 | 要改什麼 |
|---|---|---|---|
| ① 收值 | Elastic APM agent；公司現有 | FDC 系統、MES、量測站、機台 alarm | 不是 agent 埋點，是匯出或介接；用 `sources/` 的 file、http、DB 來源 |
| ② 清資料 | 不收測試流量（沒有自動辨識） | 排除 PM（預防保養）後、qual run、dummy wafer、工程 lot | 要有「哪些 run 不算」的標記；對應現有的排除時段、重置點 |
| ③ 存 | `traces-merged-*`、`aiops-*` | 加一個 `lot-paths-*`（一個 lot 一筆，step 巢狀），其餘 `aiops-*` 不變 | 新 template；結果索引沿用 |
| ④ 判斷 | 每分鐘 metrics；「同星期幾 × 同小時」的基準線 | 事件驅動：每個 run 一個值；基準線依（機台、chamber、recipe、step）分 | 見下「M1」 |
| ⑤ 異常總表 | `aiops-anomalies`，key 是 service、route | key 是 equipment、chamber、recipe、step | `keys` 欄位換；索引不變 |
| ⑥ 收斂 | 同服務、時間相連合成一件；跨服務依呼叫關係併一層 | 同機台、同一段時間；跨機台依「同一批 lot 的前後站」併一層 | 見下「事件整合」 |
| ⑦ 根因分析 | metrics → trace → log | FDC 異常 → lot 路徑 → 機台 alarm／事件 log | 路徑合成器取代 trace-merge |
| ⑧ 出口 | Grafana 依 dept 分送 | 同；dept 換成廠區、製程段（區域）、設備工程師群 | `labels.dept` 的來源換成機台的歸屬表 |
| ⑨ feedback | 老師傅確認、case 只做分類 | 同；分類表換成半導體的 | 見下「case 分類」 |

### 五個模型

| 模型 | 目前 | 半導體 | 改法 |
|---|---|---|---|
| M1 動態基準線學習 | 一條序列（服務 × API）依「同星期幾 × 同小時」取中位數與 MAD，粒度 1 分鐘到 1 小時 | 一條序列是（機台、chamber、recipe、step、感測參數）；一個 run 一個值（均值、峰值、斜率）；時間週期不重要，**製程狀態才重要**（距上次 PM 的 run 數、前一個 lot 的 recipe） | 分組鍵從「時段」換成「製程條件」：`train` 的時段欄換成條件欄；`n` 是 run 數；PM 之後重置基準線（現有的 `reset_point`） |
| M2 Pod 同儕橫向比對 | 同服務的 Pod 互比 | **chamber matching**：同型機台、同 recipe 的 chamber 互比 | `group` = 機型 + recipe，`member` = chamber；`min_peers` 要看廠裡同型機台有幾台 |
| M3 靜態臨界值規則 | 錯誤率 5%、p95 2 秒 | SPC／管制界限：超出 spec、連續 N 點同側、趨勢（Western Electric、Nelson 規則）、alarm 次數 | `slope`、`total`、`window` 型已有；連續 N 點同側要新增 type |
| M4 trace 分段 | 每個請求每一段自己花的時間，出事時跟平常比 | 每個 lot 每一步的加工時間／參數，壞 lot 跟好 lot 比 | `core/traces` 的 span 格式不變；寫路徑合成器產生 span |
| M5 相似事故分類 | 指紋：服務、API、慢的段、錯的源頭、log 樣式、指標 | 指紋：機台、chamber、recipe、step、FDC 參數組合、alarm 代碼 | 指紋欄位對應換掉；投票邏輯不變 |

### 事件整合與 RCA graph

| | 目前 | 半導體 |
|---|---|---|
| 併成一件的依據 | 同服務、時間相連（`close_after` 30 分鐘）；跨服務用 trace 裡的呼叫關係併一層 | 同機台、時間相連；跨機台用「同一批 lot 的前後站」併一層（前站出事連帶後站的良率異常） |
| 「共同點」 | `analyze.commonality`：壞請求的 Pod、版本、節點跟好的比 | 壞 lot 的 chamber、recipe 版本、PM 週期、材料批號、氣體管路跟好 lot 比 |
| 圖 | 不建圖（⚪ graph RAG 是構想）；同源用依賴對象互標 | **更值得做**：半導體的根因典型是「共用」，要靠交集找 |
| 圖的節點、邊 | — | 節點：lot、機台、chamber、recipe 版本、量測站、材料批；邊：加工歷程、使用關係 |
| 實作 | — | 先不用 graph DB：lot 路徑表自連接（pandas）算交集夠用；規模上來再評估 |

### 資料與模型的差別

| | 目前 | 半導體 |
|---|---|---|
| 資料到達方式 | 連續（每分鐘） | 事件（run 結束、lot 出站）；量測站的結果晚數小時到數天 |
| 時間軸 | 牆上時間 | **run 序號、lot 出站時間**；基準線的窗用「最近 N 個 run」不用「最近 10 分鐘」 |
| 異常的樣子 | 流量、延遲、錯誤率突變 | 參數漂移（慢慢偏）、突跳、chamber 間不一致、良率下降（有延遲） |
| 標記的難度 | 老師傅看報告就能判斷 | 要等量測、良率回來才知道真不真；回饋延遲長 |
| case 分類表 | db、network、config_change、deploy、external、resource、bug、data | equipment（硬體）、process_drift（製程漂移）、recipe、material（材料）、metrology（量測誤差）、pm_related、operator、other |

## 做的順序

| 步驟 | 內容 | 前提 |
|---|---|---|
| 1 | 只做 M1、M3：FDC run summary → 基準線、SPC 規則，用 `source: file` 先跑離線資料 | FDC 匯出檔（機台、chamber、recipe、step、參數、時間） |
| 2 | M2 chamber matching | 同型機台的 run summary |
| 3 | lot 路徑合成器 → M4 定位 | MES 歷程表；FDC 要能對到 lot id |
| 4 | 事件整合換成機台、前後站 | 步驟 3 |
| 5 | RCA graph（lot 路徑表自連接的共同點） | 步驟 3、材料批、PM 紀錄 |
| 6 | M5 分類（半導體分類表、指紋） | 歷史事故單 |

步驟 1 不需要路徑，拿到 FDC 檔就能做；步驟 3 是這份設計的關鍵依賴。

## 現況（2026-11）

廠方的異常由 ML 團隊的模型（異常跳點）和 FDC 鳴叫系統判，所以**沒有照上表做 M1～M3**，改成 engine 從 ⑤ 開始接；其餘照對照表做了第一版（[configs/factory/README.md](../aiops-engine/configs/factory/README.md)、[PDCA](pdca/meta/2026-11-factory-engine.md)）。工廠是 engine 的一個 plugin（`AIOPS_PLUGIN=factory`），沒有 MES 履歷時用 `metrics_only`（只有異常、log、事故前的變更，不併前後站；[plugins/README.md](../aiops-engine/src/scenario/README.md)、[PDCA](pdca/meta/2026-11-plugin-architecture.md)）：

| 對照表的項目 | 做了 | 沒做 |
|---|---|---|
| ④ 判斷 | 接收器 `detector ingest`：ML、FDC 的輸出 → `aiops-anomalies`（`level` ml／fdc、`source_id` 是 run_id／alarm_id） | engine 自己判 FDC（M1、M3）、chamber matching（M2） |
| ③ 存 | `lot-paths-*`（一個 lot 一筆，跟 `traces-merged-*` 同格式） | 同一個 ES 跟 IT 版分 `aiops-*` 前綴 |
| ⑥ 收斂 | 同機台一件；後站的異常依履歷併進前站（優先「那件已有這批 lot」的前站）；嚴重度依參數家族；recipe 變更當換版 | 前後站併件只併一層 |
| ⑦ 根因 | 履歷合成器 `paths-merge`（MES 主幹＋S6F11 子段）→ M4 照舊：哪一站哪個 chamber 多花多久、壞 lot 的共同點（chamber、recipe 版本、材料批）、機台 alarm 新樣式、事故前的 PM／recipe 變更 | RCA graph；rework、split／merge lot 的路徑 |
| ⑨ 回饋 | 半導體分類表、誤報原因；`feedback export` 匯出給 ML 團隊 | Grafana 回饋表單的選項仍是 IT 版；調不到 ML 的門檻（本來就不該在 engine 調） |

驗證只有假資料（7 天、728 個 lot、兩個事故，15 項檢查全過）；真實資料的欄位、lot 路徑的分叉、量測延遲都還沒碰到。

## 風險與還不知道的

| 項目 | 為什麼 |
|---|---|
| 欄位名是假設 | 沒有 MES、FDC 的欄位樣本；`lot.id`、`equipment.id`、`recipe.id` 是 README 預留的名字 |
| 一個 lot 的路徑可能分叉、回頭（rework、split／merge lot） | span 格式是樹（一個 parent）；回頭、合批要另外處理 |
| 基準線的分組太細會沒有樣本 | （機台 × chamber × recipe × step × 參數）組合多，新 recipe 一開始沒有基準線；借同機型的組基準線（現有的 `borrow`） |
| 良率回饋延遲 | 老師傅確認可能是幾天後；`close_after`、回饋統計的窗要拉長 |
| 資安 | 半導體資料多半不能出廠區；LLM 摘要要走內網閘道或只用 `template` |
