# trace 段異常與資料品質檢查：邊緣情境（2026-10）

**狀態**：已完成；兩個行為變更**待使用者決定**　　**日期**：2026-10-28　　**相關**：分支 `worktree-agent-a5ab9d5cee4d2a7de`（從 `14a12ed`）；[baseline README「trace 每一段」](../../../engine/configs/baseline/README.md#trace-每一段的-baseline)、[「資料品質檢查」](../../../engine/configs/baseline/README.md#資料品質檢查資料本身怪不怪)、[data-quality-check](2026-10-data-quality-check.md)
**模組**：② 清資料、④ 判斷

## Plan：這一次要達到什麼

- **需求**：trace 段異常（`trace-collect`／`trace-train`／`trace-detect`）和資料品質檢查（`quality`）只驗過幾個主要案例。尾端取樣、新舊埋點欄位、第一天、缺欄位這些邊緣情境沒有預期，也沒有實測。
- **目的**：每個情境先寫預期，再用自己的 ES 灌合成資料實跑；錯的修掉並補測試，門檻問題記在 Act。
- **範圍**：
  - trace：每段延遲的動態基準線學習、N+1、例外吞噬檢測、新出現／消失的段、部分請求變慢、尾端取樣的偏差。
  - 資料品質：路由沒參數化、`service.name`／`dept` 缺、資料停了、unassigned 突增、failure store、第一天。
  - 不上共用 cluster，不改 Collector、pipeline。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/trace.py` | 例外吞噬：入口沒標錯誤時，標了錯誤的例外也算吞掉（**待決定**） | Java agent 的 JDBC 丟例外一定標錯誤，catch 掉、回 200 原本抓不到 |
| `engine/core/traces.py` | 呼叫對象多看 `db.system.name` | 新版 DB 語意只有這個欄位時段名沒有「→」，N+1 規則看不到 |
| `engine/detector/quality.py` | 第一天（昨天整段沒資料）不看倍數（**待決定**） | 第一天每個 ≥50 條樣板路由的正常服務都被報 |
| 同上 | 「像 ID」的比例改抽最多 500 個值（原 50），限制每頁組數 | 只抽筆數最多的 50 個，量少的 ID 路由抽不到，比例被低估 |
| 同上 | 查詢結果用 `dtype=object` 建 DataFrame（`_frame`） | pandas 3 把缺欄位那組的 None 變 NaN，寫 ES 整批 400、`quality` 中斷 |
| 同上 | 訊息裡缺欄位的那組寫「沒有 namespace」 | 原本寫「全部」或「nan」，看不出來是哪一組 |
| `engine/detector/cmd_quality.py` | `sample_values`（預設 500）；寫入前去掉 NaN 的 key | 同上 |
| `engine/configs/baseline/quality.yaml` | 新增 `trace_name_cardinality`、`metrics_missing_service`、`trace_stopped` | trace 名稱沒參數化、metrics 缺服務名、trace 停了，原本沒有檢查 |
| `engine/tests/test_trace.py`、`test_quality.py` | 兩種吞噬樣子、`db.system.name`、第一天、NaN key、設定檔欄位 | 改前這些新測試失敗（4 個），改後全過 |
| `engine/tests/trace_quality_edge_e2e.py`、`trace_edge.yaml`（新） | 灌合成 trace／log／metrics，每項印 PASS／FAIL | 可重跑；只刪 `tq-` 開頭的資料；`a`／`b` 只跑一段 |
| `engine/configs/baseline/README.md` | 吞噬定義、對象欄位、三個新檢查、第一天 | — |

**兩個待使用者決定的行為變更：**

| 變更 | 改前 | 改後 | 副作用 |
|---|---|---|---|
| 例外吞噬的定義 | 只算「有例外、這一段沒標錯誤」 | 另加「這一段標了錯誤、入口沒標錯誤」 | 重試後成功也算吞掉；同一段同時報 error_rate 和吞噬 |
| 第一天的路由數檢查 | 昨天整段沒資料也算倍數（當成從 0 開始） | 昨天整段沒資料時只看上限 500、像 ID ≥ 50% | 第一天就有 50～499 條非 ID 樣子的爆炸抓不到 |

- 例外吞噬：既有測試 `test_error_is_not_swallowed` 改成「錯誤傳到入口（500）」才不算吞掉，跟現在的程式一致。
- 新服務（昨天有別的服務、沒有它）的倍數照樣從 0 算，沒變。

## Check：測試的目的與結果

`ES_URL=http://127.0.0.1:31504 python -X utf8 -m tests.trace_quality_edge_e2e`（`engine/` 底下）。ES 9.5.3 單節點、heap 512m，跑過 `elasticsearch-setup.sh`；2026-10-28 13:58Z。

- 整份：**41 PASS、2 FAIL**（trace 段 21／1，資料品質 20／1）。之後只修了測試的清理與顯示，B 段重跑一次，結果一樣是 20／1。
- 合成 trace：每分鐘 400 個請求（4% 錯、3% 慢 1.5～3 秒），照 `sampler.yaml` 的順序做尾端取樣。
  - random 5% 先留；錯、例外、超過 1 秒的全留，寫進 `traces-generic.otel-*`。
  - 正常 40 分鐘學 baseline（1 分鐘一點），每個情境 3 分鐘。共 50,119 個 span。

### trace 每一段

| 情境 | 預期 | 實際 |
|---|---|---|
| 尾端取樣後的去向 | 不是 random 的都轉到 traces-problem | generic 裡 0 個；problem 35,865 個 span |
| 基準線用哪一份：只讀 random | 錯誤率、簡訊時間跟真實差 ≤ 3 標準誤 | 錯誤率 4.3%（真實 4.0%）；簡訊 98 ms（真實 89 ms） |
| 對照：讀全部留下的（不分 policy） | 明顯偏高 | 錯誤率 34.6%；簡訊 561 ms |
| 低流量 API（每分鐘 40 個） | 每點不到 5 個請求，不學 | 每點中位數 2 個（主 API 20 個）；baseline 0 筆 |
| 正常 3 分鐘（含 4% 錯、3% 慢） | 不報 | 0 筆 |
| wip-worker 查 DB 2 → 80 ms | 那一段的 time_ms 報 | 報 anomaly；mes-api 等 wip-worker 那段也報 |
| N+1（每個請求查 60 次） | calls、repeated 報在 wip-worker | 都報；mes-api 沒有 repeated |
| N+1，DB span 只有 `db.system.name` | repeated 報在「→ oracle」那段 | 報（改前：段名沒有「→」，只報成新段） |
| 吞噬：例外、status 沒設、回 200 | swallowed、swallowed_rate 報 | 都報 |
| 吞噬：DB 標錯誤＋例外、wip-worker 回 200 | swallowed 報 | 報，同時報 error_rate（改前只有 error_rate） |
| 錯誤一路傳到入口 500 | 報 error_rate、不報吞噬 | error_rate 四段都報；吞噬 0 筆 |
| 新出現的段（每個請求多查 redis） | new 報 | 報 `HGET → redis` |
| 消失的段（不再送簡訊） | 簡訊那段 calls 報 | 報（1 → 0） |
| 只有 10% 請求的簡訊多 2 秒 | 簡訊那段 time_ms 報 | **FAIL：沒報**（見 Act） |
| 長期 2% 吞例外（低於門檻 5%） | 不報 | 3 個點 0 筆 |

- 部分變慢沒報的原因（同一份產生器在記憶體實跑）：
  - 3% 長尾讓每點平均是雙峰（抽到 0 個或 1 個慢的），基準線中位數 68 ms、MAD 49 ms，門檻約 430 ms。
  - 10% 變慢的點落在 220～612 ms，只有偶爾超過。
- 改前的 trace 結果是用同一份產生器在記憶體跑的（呼叫同一套 `summarize`、`train`、`score`、規則），不是在 ES 上。

### 資料品質

| 情境 | 預期 | 實際 |
|---|---|---|
| 120 條 `/lots/<數字>`（昨天 3 條） | 報 | 報 |
| 3 條樣板、60 條樣板（兩天一樣） | 不報 | 不報 |
| 40 條樣板（量大）＋45 條 ID（各 1 筆） | 報（53% 像 ID） | 報（改前沒報：只抽 50 個，看到 20%） |
| trace 入口名稱 120 個 `GET /orders/<數字>` | 報 | 報（改前沒有這個檢查） |
| Pod 沒有 `app.kubernetes.io/name`（三成） | 報：服務身分對不上 | **FAIL：沒報**。pipeline 改用 container 名稱（120 行變 `app`） |
| log 沒補到 K8s 中繼資料（200 行） | 報 | 報「沒有 namespace，100% 缺」（改前 `quality` 中斷，見下） |
| metrics 缺 `service.name` | 報 | 報（改前沒有這個檢查） |
| 沒標 dept：unassigned 的 log 2% → 17% | 報 | 報 |
| 沒標 dept：unassigned 的 metrics 1% → 25% | 報 | 報 |
| log 停 20 分、metrics 停 10 分、trace 停 20 分 | 各報一次；有資料的不報 | 都報；有資料的沒報（trace 改前沒有檢查） |
| log、span 的時間壞掉 | 進 failure store、兩個資料流都報 | 都報 `document_parsing_exception` |
| span 的 duration 是字串 | — | 不進 failure store：logsdb 存下、丟掉那個欄位 |
| 帶 trace.id 的 log 90% → 20% | 報 | 報 |
| 第一天：像 ID 的路由 | 報 | 報 |
| 第一天：60 條樣板路由 | 不報 | 不報（改前報「從 0 變 60」） |
| 第一天：unassigned、trace.id 變少 | 不報（只跟昨天比） | 不報；unassigned 設 `max_share: 0.1` 會報 |
| 第一天：缺欄位、資料停了 | 照報 | 照報 |
| `quality` 指令整個跑 | 寫進 aiops-anomalies | 15 個檢查觸發 12 個，寫入 |

- **發現的 bug**：log 沒有 namespace 時，那一組的 key 被 pandas 3 變成 NaN。
  - 寫進 `aiops-anomalies` 時 ES 回 400（`Non-standard token 'NaN'`），整個 `quality` 指令中斷，這一輪所有檢查的結果都沒寫。
  - 已修（`_frame`），補了單元測試。
- **沒測到的**：
  - 真的 Java agent、.NET 的 JDBC span（用合成的）。
  - Kafka 消費者、時鐘不同步（只有單元測試）。
  - 正式流量下的查詢時間。
  - trace 落到 unassigned、`unknown_service:*`。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 只有一部分請求變慢抓不到 | 10% 請求 +2 秒，3 點都沒報 | 單一參數、單一 Pod 慢會漏 | 段的時間加 p90 或慢請求比例 |
| 低流量 API 學不到 | 每分鐘 40 個，每點 2 個 | 每 5 分鐘不到 100 個請求的 API 沒有 trace 基準線 | 量少的改用較粗的粒度 |
| 吞噬規則門檻＝1 個請求 | 每點約 20 個樣本，5% 就是 1 個 | 長期 2% 時推論每點約 33% 會報 | 改成跨多點累計或至少 2 個 |
| 重試後成功也算吞噬（待決定） | 新定義的副作用（推論） | 有重試的服務可能常報 | 試點時看觸發數 |
| [②] Pod 沒有服務 label 的 log 不算缺 | pipeline 退回 container 名稱 | 這些 log 對不上 metrics、trace | pipeline 記名稱來源，或檢查 container 名 |
| duration 型別錯的 span 不會被拒 | logsdb ignore_malformed | 欄位默默丟掉，failure store 看不到 | 查 `_ignored` 欄位 |
| Client span 沒有任何對象欄位 | 段名沒有「→」 | N+1 規則看不到 | 實際埋點遇到再補欄位 |
| 三個新檢查的門檻是估的 | 沿用同類檢查 | 可能誤報或漏報 | 跟其他 quality 一起試點 2～4 週再校 |

- **決定**：例外吞噬的新定義、第一天不看倍數，兩項都待使用者決定；測試跟著現在的程式。
- **下一輪**：部分變慢的統計值、低流量 API 的粒度、吞噬規則的最少請求數，列入 [README.md](../README.md) 的「④ baseline 補強」。
