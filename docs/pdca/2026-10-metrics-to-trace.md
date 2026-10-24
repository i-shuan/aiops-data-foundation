# metrics 異常怎麼找到對的 trace（2026-10）

**狀態**：決定採方案 B；根因分析的程式改動還沒做　　**日期**：2026-10-23　　**相關**：[rca/README.md](../rca/README.md#metrics-異常怎麼串到-tracelog)、`aiops/aiops_rca/`

## Plan：這一次要達到什麼

- **需求**：metrics 只有統計數字，沒有 trace_id。metrics 發現「/checkout 變慢」之後，根因分析要自己去找對應的 trace，目前用「服務＋路由＋時間」去撈，不保證撈到的是造成異常的那些請求。
- **目的**：判斷能不能讓 metrics 的資料點直接帶 trace_id（OpenTelemetry 的 exemplar），如果不行，決定用什麼替代。
- **範圍**：只驗證可行性和做決定；不改收值、不改 ES、不改應用端。

## Do：改動哪些 code、改了什麼、為何要改

**無 code 改動。** 做了一個實驗和兩份文件：

| 項目 | 做了什麼 | 為何 |
|---|---|---|
| 實驗腳本（不放 repo） | 自己組 OTLP metrics，四種型別（exponential histogram、一般 histogram、counter、gauge）各夾一個 exemplar，分別送到 ES 的 `/_otlp/v1/metrics`（正式環境用的）和 Collector 的 `elasticsearch` 寫入元件（otel 模式） | 要知道 ES 收不收 exemplar，不能只靠文件猜 |
| `docs/rca/README.md`、`docs/metrics/README.md`、`docs/metrics/catalog.md`、`docs/system-stages.md` | 把「exemplar 是待辦」改成「實測不行，用服務＋時間代替」，並寫明 RCA 實際怎麼挑 trace | 避免之後有人把它當成還沒做的待辦 |
| 本文件 | 記錄決策與比較 | 之後 ES 升級要重測時有依據 |

環境：ES 9.5.5（docker 單節點）、Collector contrib 0.162.0（專案鎖定的版本）。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| ES 內建 OTLP 入口收不收 exemplar | 送四種型別各夾 exemplar（含 trace_id、span_id），再從 `_source`、mapping、全文搜尋三處找那個 trace_id | 請求成功、數值都存了；三處都找不到 exemplar，也沒有 trace_id |
| 換成 Collector 的 `elasticsearch` 寫入元件會不會不同 | 同樣的資料經 Collector 送進去 | 結果一樣，找不到。Collector 的 debug 輸出看得到 exemplar 和 trace_id，所以是寫進 ES 的過程中沒存下來 |
| 就算存得下來，exemplar 能不能解決問題（推論，非實測） | 對照尾端取樣設定和 RCA 程式 | exemplar 只是一個樣本，不是「造成異常的那個請求」；正常的 trace 只留 5%，點過去約 95% 是空的 |
| RCA 現在實際怎麼挑 trace（讀程式確認） | 讀 `aiops_rca/es.py`、`__main__.py` | 已經撈最慢 200 個加最多 100 個出錯的跟一週前同時段比；但瀑布圖和用 trace_id 撈 log 只用最慢的 1 筆 |

**方案比較**

| | A：exemplar 轉成 log 文件 | B：用時間區間撈一批 trace |
|---|---|---|
| 做法 | 進 ES 之前把 exemplar 抽出來，另外寫成一筆筆 log 文件，RCA 查這個 index 拿 trace_id | metrics 異常時，用「服務＋路由＋時間」撈那段時間的一批慢的、錯的 trace，統計共同卡在哪一段 |
| 要不要動基礎架構 | 要。依我的了解 Collector 內建轉換不能把 metrics 變 log，要自己寫程式（未驗證）；部署、監控、重送都要顧 | 不用 |
| 要不要應用端配合 | 要開 exemplar 設定 | 不用 |
| Java 那條路 | exemplar 能不能經過「Prometheus 格式來抓 → 換成差值」沒測過 | 不相關 |
| 優點 | 從圖上的點能拿到一個確定存在的請求 | 看多數請求的共同點，特例不會主導；慢、錯的 trace 全留，撈得到 |
| 缺點 | 只有一個樣本，可能被 GC 停頓這類特例帶偏；常常查不到 trace；多一個元件 | 是「同時間、同路由」的關聯，不是綁定同一個請求；同時有兩種問題要再分群 |

## Act：Check 後沒改善的、還沒解決的

**決定**：採 B，不做 A。A 花一個新元件和跨團隊配合，換到一個不保證是原案、而且常常查不到的樣本。

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 每一段用平均，一兩筆特別慢的會拉高平均 | 讀 `analyze.py` 的 `compare_segments` | 少數極慢的請求可能讓排名失真 | 加「這批慢請求裡有幾成最卡的是這一段」和中位數 |
| 瀑布圖和撈 log 只用最慢的 1 筆 | 讀 `__main__.py` | 最慢的那筆可能是特例 | 改成幾筆有代表性的（卡在主要瓶頸段的） |
| 出事時挑最慢的、平常隨機挑，兩邊樣本性質不同 | 讀 `sample_trace_ids` | 差距被放大；排名可信、絕對值要保守看 | 文件註明；之後可改成平常也挑同等級的 |
| 比 1 秒快的變慢（例如 200ms → 800ms）只剩隨機 5% 的 trace 可用 | 對照尾端取樣的 1 秒門檻 | 這類變慢的結論弱 | 評估降低慢的門檻或提高隨機比例 |
| 慢、錯的 trace 放在 `traces-problem`（保留 30 天），RCA 讀 `traces-*.otel-*` 兩種都讀得到；沒有實際用它跑過 RCA | 對照 `aiops/rca/rca.yaml` 與 retention 的 reroute | B 依賴慢、錯的 trace 保留夠久且讀得到 | 見 [retention 紀錄](2026-10-retention.md)；用故障案例驗證 |
| Java 路線能否帶 exemplar 沒測 | 實驗只用 OTLP 直送 | 若之後 ES 支援，Java 這條還要另外驗證 | ES 升級時用同一個腳本重測 |

**下一輪**：改 `aiops/aiops_rca`（`analyze.py`、`__main__.py`），用 k3s 的故障案例（`test/local-k3s/run-rca-e2e.sh`）確認「幾成卡在這段」比只看平均更準。
