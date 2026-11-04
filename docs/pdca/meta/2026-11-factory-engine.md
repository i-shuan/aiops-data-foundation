# 工廠版 engine：復用程式、另一套設定，ML 的異常接進 ⑤、生產履歷取代 trace（2026-11）

**狀態**：已完成（第一版，假資料）　　**日期**：2026-11-04　　**相關**：分支 `claude/factory-engine`（接在 `claude/semiconductor-design` 後）；設計 [semiconductor-design.md](../../semiconductor-design.md)；操作 [configs/factory/README.md](../../../aiops-engine/code/configs/factory/README.md)
**模組**：④⑤⑥⑦⑨

## Plan：這一次要達到什麼

- **需求**：工廠（半導體 FDC）要用 AIOps，但沒有 trace id；異常由 ML 團隊的模型（異常跳點）加 FDC 鳴叫系統判，engine 不必再判。廠方目前沒有明確需求，先跑出一版給他們看。
- **目的**：同一份 engine 程式、另一套設定（`configs/factory/`）能從 ⑤ 開始跑完整條：ML／FDC 的異常 → 收斂成一件 → 用生產履歷定位到站點、機台、chamber、recipe → 通知 → 老師傅回饋。用假資料就能整條走一遍，不等廠方資料。
- **範圔**：
  - 做：(1) ④ 接收器：ML 輸出、FDC 鳴叫轉成 `aiops-anomalies`；(2) ⑥ 收斂：併件的鍵換成機台，跨站依同一批 lot 的前後站併；(3) ⑦ 履歷合成器：MES 進出站為主幹、S6F11 機台事件為子段，轉成現有 span 格式，M4 不改；(4) ⑦ log 換成機台 alarm、PM、recipe 變更（recipe 變更套用「換版後算 warning」）；(5) ⑨ 誤報匯出給 ML 團隊，分類表換成半導體的；(6) 假資料＋整條跑通。
  - 不做：engine 自己對 FDC 感測值跑 M1～M3；chamber matching（M2）；RCA graph；真實 EAP／MES 介接；LLM 摘要（走 `template`）。
- **決定**：復用不 fork。engine 有價值的是 ⑥～⑨，跟資料從哪來無關；`sources/` 已抽好介面；兩套設定分開部署。
- **假設（未驗證）**：ML 輸出一個 run 一筆，帶 lot、機台、chamber、參數、分數；FDC 鳴叫帶 lot id；EAP 有把 S6F11 存進 DB；撈得到 MES 進出站；EPT 不帶 lot，只當背景。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `core/domain.py`（新） | 領域 profile（`AIOPS_DOMAIN` it／factory）：根因分類表、誤報原因、常查的地方、對象／單位／trace 的叫法 | 回饋表單與報告措辭跟「這是服務還是機台」有關，YAML 碰不到；放一處，`feedback.py`、`feedback_web/app.py`、報告、LLM 範本都讀它 |
| `core/source.py` | 加 `factory` profile：owner 查機台主檔 `eqp-master`（`static`，沒有時間欄位）、log 是 `eqp-logs-*`（`equipment_id`、`lot_id`、`event_type`）、trace id 是 `lot_id` | 既有的「索引、欄位依來源」機制直接多一個來源 |
| `core/traces.py` | `schema_of`：`lot-paths` 也當 merged | 履歷跟 `traces-merged` 同格式，讀回來走同一條路，RCA 不改 |
| `detector/trace_merge.py` | `build(..., prefix=)` | 履歷用同一個合成函式，只換 index 前綴 |
| `detector/ingest.py`、`cmd_ingest.py`（新） | 接收器：一個 input 一個來源，`series`／`message` 範本帶欄位、`keys` 對應、`status`／`severity` 可查表；`_id` 由來源識別碰算；來源可以是 ES 或 `sources` 的 Backend（file／http） | 工廠的異常是別人判的，engine 只轉格式；重讀不重複、拿到樣本只改右邊欄位名 |
| `detector/path_merge.py`、`cmd_paths.py`（新） | 履歷合成器：root 是 lot、站是 Server span（service 機台、name recipe、hold／scrap 算錯）、S6F11 事件掛到同機台同時段的站底下；自己放 `lot-paths` 的 index template（nested） | lot 路徱取代 trace；格式一樣所以 M4、瀑布圖、共同點、前後站全部照舊 |
| `detector/owner.py` | `SVC_KEYS` 加 `equipment`；`static` 的主檔不加時間過濾 | 異常的 dept／group 從機台主檔查 |
| `detector/__main__.py` | 指令 `ingest`、`paths-merge`；`feedback export --out` | 排程表與人都用得到 |
| `detector/cmd_feedback.py` | `export_rows`／`write_export`：回饋＋它那筆異常（值、分數、`source_id`、對象）合成一列，NDJSON 或 CSV | 工廠的誤報 engine 調不到（門檻在 ML），只能回給模型那邊重訓 |
| `detector/feedback.py`、`feedback_web/app.py` | 分類表、誤報原因、常查的地方改讀 `core/domain` | 同上一列的 domain |
| `investigator/alerts.py` | `SVC_KEYS` 加 `equipment`；`route_of` 認 `recipe`；件多 `lots` 欄位；`merge(..., links=)`：後站的異常依 lot 併進前站——優先「那件已經有這批 lot」的前站，否則最近且 lot 在那站的時間落在那件範圍（±`close_after`）的前站 | 工廠沒有呼叫關係但有 lot 路徑；第一版只用「最近的站＋時間」會併錯（e2e 併到 CVD-01），改成同一批 lot 優先 |
| `investigator/factory.py`（新） | `lot_links`（從 `lot-paths-*` 查這批 lot 在出事機台之前經過哪幾站、幾點離站）、`change_at`（變更紀錄最近一筆的時間） | 前後站、recipe 變更當換版 |
| `investigator/severity.py` | `parse_classes`、`signal_class(series, classes)`、`grade(..., classes=)` | IT 的四類黃金訊號不認得 FDC 參數；工廠用參數家族（pressure、temperature、metrology…）當廣度 |
| `investigator/cmd_alert.py` | `chain_source: lot_path`、`critical_levels`（加 fdc）、`deploy_kind: change_log`、`rca_lead`、severity `classes`；`ALERT_MAPPING` 加 `lots` | 設定決定走 IT 還是工廠那條路，程式一份 |
| `investigator/es.py` | 入口的時間放在 `entries.start_ms`（文件層多框 `lag`）；`change_logs`；異常查詢認 `keys.equipment` | 履歷一個 lot 一筆、文件時間是 lot 開始，lot 幾小時後才到這一站；只用文件時間會挑錯 lot（e2e 的共同點因此沒抓到 CH-B） |
| `investigator/__main__.py` | `entry_lag`、`commonality.min_traces`、報告多一段「事故前的變更」（`changes`）、措辭依 domain（請求／lot、Trace／履歷）、`_ms` 超過 1 分鐘用分 | PM、recipe 變更在事故**之前**，原本的 log 段只看事故窗看不到；工廠一站幾十分鐘，毫秒看不懂 |
| `core/llm.py` | `template` 的措辭依 domain | 同上 |
| `configs/factory/`（新） | `ingest.yaml`、`paths.yaml`、`rca.yaml`、`schedule.yaml`、`README.md` | 另一套設定；欄位名全是假設 |
| `helmchart` | `source` 多 `factory`（排程表對應表）、`domain` → `AIOPS_DOMAIN` | 另外部署一隻 |
| `tests/factory_e2e.py`（新） | 假資料（7 天、728 個 lot、兩個事故、雜訊）、整條跑、15 項檢查、`--dump`、`--clean` | 不等廠方資料就能看整條 |
| `tests/test_ingest.py`、`test_path_merge.py`、`test_factory_alerts.py`、`test_feedback_export.py`（新，31 個） | 見 Check | |
| 文件 | `configs/factory/README.md`（操作）、`semiconductor-design.md` 加現況、`aiops-data-reference.md`（`level` ml／fdc、`source`、`source_id`、`lots`）、`docs/README.md`、`CLAUDE.md` | |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 接收器轉得對、重送不重複、weak 不開件、OOS 是 critical、壞列略過 | `test_ingest`：欄位對應、`_id` 由 run_id、查表預設值、沒時間／對象的列、巢狀欄位、設定檢查 | 9 個過；`value` 壞掉的列留下（None）不丟，沒時間或沒對象的才略過 |
| 履歷格式跟 traces-merged 一樣、M4 算得出哪一站哪個 chamber 慢 | `test_path_merge`：root／站／事件的 span、hold 算錯、站的自己時間扣掉 chamber、還沒出站用 now、`build_docs` 的 index 與 entries、`compare_segments` 壞 lot vs 好 lot | 7 個過；慢 20 分鐘的 chamber 段排第一、差 20 分鐘 |
| 前後站併對、同一批 lot 優先、自己有件不被搶、時間不對不併；參數家族的廣度；recipe 變更上限 | `test_factory_alerts`：ETCH-01 與 CVD-01 都開著、METRO 的異常併進有這批 lot 的 ETCH-01；沒有的話併最近且時間對得上的；10 小時前的件不併；三個家族 critical、換版 warning；domain 表 | 13 個過 |
| 匯出一列有回饋＋異常的來源識別碼、對象、值；過期的異常用回饋自己帶的 | `test_feedback_export`：假 ES、NDJSON 與 CSV | 2 個過；CSV 的 `keys` 攤成 `key.<名>` |
| **整條在測試 ES 上跑通** | `tests.factory_e2e`：728 個 lot 的 MES＋S6F11 → `paths-merge`；32 筆 ML／FDC → `ingest`；`alert`；`feedback add`→`export` | **15/15**：履歷一個 lot 一筆（728）；異常 32 筆全進；ETCH-01 一件 **critical**（FDC OOS）、23 項異常；METRO-01 的 cd_bias 併進 ETCH-01（`upstream_services=[METRO-01]`）、沒另開；報告慢在 `ETCH-01 › CH-B MAIN` +31.9 分（平常 12.6 分）、共同點 chamber `ETCH-01/CH-B` 100%、新 alarm 樣式 `ALID <n> Chamber B pressure…`、事故前的 PM 在結論裡；CVD-01 一件 **warning**（「換版後 25 分鐘內…原本 high：2 類訊號」）；LITHO-01 只有 weak 沒有件；CMP-01 單點 warning 自己結束；匯出那筆帶 `source_id=R-cmp-single` |
| 沒弄壞 IT 版 | 全部測試 | 671 個過（640 ＋ 31）；chart `helm template` 預設與 `source=factory` 各渲染一次，排程表路徑與 `AIOPS_DOMAIN` 對 |

- **發現的問題**（都在 e2e 抓到、已修）：
  1. 前後站第一版用「最近的前站＋時間對得上」，METRO 的異常被併進 CVD-01（它也開著、時間也重疊）→ 改成「那件已經有這批 lot」優先，件上多 `lots` 欄位。
  2. RCA 挑 lot 用文件的 `@timestamp`（lot 開始），事故窗內「開始」的 lot 跟「在 ETCH-01」的 lot 不是同一批，壞 lot 只抓到 2 個、共同點算不出 CH-B → 入口時間改放 `entries.start_ms`，文件層多框 `entry_lag`；再加 `rca_lead` 讓窗從第一筆異常往前 1 小時（ML 是 run 結束才報）。
  3. PM 在事故前，原本的 log 段看不到 → 加「事故前的變更」。
  4. 報告說「每個請求」「新 Pod」→ 措辭依 domain。
- **沒測到的**：真實的 ML 輸出、FDC、MES、S6F11 欄位；lot 分叉、rework、合批；量測延遲好幾天的情況（假資料 3 小時到量測）；`source: file` 離線跑 `ingest`／`paths-merge`（只有 ES 路）；Grafana 上看工廠版的件。
- **副作用**：e2e 跟 IT 版共用測試 ES 的 `aiops-anomalies`、`aiops-alerts`，第一次跑把 IT 版 7 件過期的 open 件標成 resolved（本來就該結束的）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 共同點把「壞 lot 都經過 CVD-01 的新 recipe v13」「材料批 MAT-0」列成第一名 | e2e：壞 lot 剛好都在 recipe 變更後經過 CVD-01、假資料的材料批每 20 個 lot 換一批 | 是真的共同點但不是根因，排在 CH-B 前面 | 共同點分兩層：出事機台自己的（chamber、recipe 版本）排前面，整條路徑的排後面 |
| [⑧] Grafana 回饋表單的分類、誤報原因還是 IT 版 | 設計 | 工廠的老師傅在 Grafana 填會看到 db、network… | 表單選項從回饋服務的 `/api/missed-form` 一樣的方式動態拿，或工廠版另一份 dashboard |
| [③] 跟 IT 版共用一個 ES 時 `aiops-*` 沒分前綴 | e2e 寫進共用索引 | 兩套部署同一個 ES 會混 | `AIOPS_INDEX_PREFIX`；工廠資料不出廠區，短期不會同一個 ES |
| 前後站只併一層、只看時間 ±`close_after` | 設計 | 量測延遲好幾天的（良率）併不進去 | 量測類的異常另外用 lot 回溯，不受 `close_after` 限制 |
| `feedback suggest` 對 ml／fdc 的序列會提放寬 z、min_abs | 設計 | 建議沒有意義（門檻在 ML） | `suggest` 跳過 `level` ml／fdc 的序列，改提「匹出給 ML」 |
| `source: file` 離線跑只有 `ingest`、`paths-merge` | 設計 | `alert`（收斂、RCA）一定要 ES | 不改：工廠一定有 ES |

- **決定**：復用不 fork；engine 從 ⑤ 開始接；履歷用 trace 的格式；前後站同一批 lot 優先。
- **下一輪**：拿到廠方的 ML 輸出、FDC、MES 樣本後對欄位；共同點分兩層；Grafana 表單選項改動態。
