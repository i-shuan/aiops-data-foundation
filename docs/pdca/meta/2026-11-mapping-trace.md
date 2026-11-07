# 欄位對照的覆寫擴到 trace：歸屬欄位、raw 索引（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/mapping-trace`（從 `claude/mapping-ui` 開，那條還沒合回 master）；第一版 [2026-11-mapping-ui.md](2026-11-mapping-ui.md)
**模組**：②（讀進來的欄位對照）

## Plan：這一次要達到什麼

- **需求**：使用者問「trace 上面也有支援 mapper 嗎」。第一版只開放歸屬與 log；trace 的兩件事還是要改程式或設定檔：
  1. APM 的 span 讀部門、群組是寫死的 `labels.dept`、`labels.group`（`data/source/apm.py`），在 `/mapping` 核准把部門改到別的欄位，metrics 與 log 的異常跟著改，**trace 合成時還是讀舊的**（`traces-merged-<group>` 的分法、文件的 `dept` 會不一致）。
  2. raw trace 讀哪個索引（`trace.yaml` 的 `raw_index`）要改設定檔再部署。
- **目的**：①trace 合成時，部門、群組用跟歸屬同一份（含覆寫）的欄位名；②raw 索引也能網頁提、命令列核准，核准前先對真的資料試（索引有沒有資料、格式是不是宣告的來源）。
- **範圍**：只做這兩件。**不做**：trace id、耗時、上一層 span 這些結構欄位（決定 trace 接不接得起來，寫錯整段壞；新格式寫新的來源適配器）；序列（`series.yaml`）。
- **假設**：同一個來源的 agent 在 metrics 與 trace 用同一個 label 路徑（APM 都是 `labels.dept`；OTel 都是 `resource.attributes.dept`），所以 trace 直接用歸屬的 `dept`、`group` 欄位名。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/data/source/adapter.py` | `apply_owner`（span 的 `resource.dept`、`resource.group` 改用歸屬的欄位名從 raw 文件讀；文件沒有就拿掉）、`with_paths`（查 raw 時多要這些欄位） | 第一件：trace 跟 metrics、log 讀同一個欄位；覆寫的欄位不一定在 APM 預設的 `_source` 清單裡 |
| `aiops-engine/src/data/store/trace_repo.py` | `spans` 讀 raw 時用 `_owner(來源)`（`effective` 之後的歸屬）套上 | 只有讀 raw 的這一處；merged 不動（合成時已經寫好） |
| `aiops-engine/src/data/store/models.py` | `traces-merged-<group>` 的 group 轉小寫 | 群組改讀別的欄位時值可能有大寫（D3120），ES 的索引名不收大寫，整批寫不進去 |
| `aiops-engine/src/common/scenario/base.py`、`mapping.py` | `Scenario.trace_raw`；`EDITABLE` 加 `trace.raw_index`；`apply` 把核准過的寫進 `trace_raw` | 第二件：覆寫跟歸屬、log 同一套（同一個索引、同樣的狀態與核准） |
| `aiops-engine/src/data/mapping_service.py` | `raw_indexes`（trace.yaml 的蓋上覆寫：只換那個來源的；沒列的來源加上去）、`yaml_raw`；試 raw 索引用階段 2 的 `check_trace_index`（要有資料、要是這個來源的格式） | 換錯索引會讓 trace 整段停，核准前一定要對得上格式 |
| `aiops-engine/src/interface/config/schedule.py` | `trace_raw_pairs`：從排程表找 trace-merge 用的 trace.yaml | 網頁與 `mapping list` 要顯示 trace 的 yaml 值、提案要記「原本是什麼」；情境裡沒有 trace.yaml 的路徑 |
| `aiops-engine/src/interface/cli/detect.py` | `trace-merge`、來源自我檢查讀 raw 索引前先 `MAP.raw_indexes(effective(...))` | 生效點 |
| `aiops-engine/src/interface/cli/mapping.py`、`web/`（`views.py`、`routes.py`、`templates/mapping.html`） | 帶 trace.yaml 的值；頁面說明 trace.raw_index | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 部門、群組的欄位名：照歸屬讀、文件沒有就拿掉、歸屬沒寫不動；多要的欄位加進 `_source` | `tests/test_mapping_trace.py::OwnerFields` | 都如預期；OTel 的 yaml 路徑跟原本 adapter 讀的一樣（沒覆寫時結果不變） |
| 合成時用覆寫的群組、部門，索引名跟著 | 同上 `test_merge_uses_the_overridden_group_and_dept` | `traces-merged-mes-…`、dept 讀 `custom.dept` |
| raw 索引：沒覆寫原樣、只換那個來源、沒列的來源加上去、沒明講來源的用索引名判斷 | `RawIndex` | 都如預期 |
| 試 raw 索引：格式對才過、別的來源的格式擋、沒資料是 nodata；格式不對核准不了 | `RawIndex` | 都如預期 |
| 清單與提案知道 trace.yaml 的值；工廠的排程表沒有 trace-merge 回 None | `RawIndex` | 都如預期 |
| **真的 ES** | `tests/mapping_trace_e2e.py`（APM 合成資料）：每一步清掉 merged 與進度、重跑 `detector trace-merge` | ①沒覆寫：`traces-merged-mes`、D3120／mes ②群組改讀 `labels.dept`：`traces-merged-d3120`、group D3120 ③提 APM 的 metrics 索引當 raw：核准被擋（不是這個格式） ④沒資料的索引：不 `--force` 不給，`--force` 後合 0 個 ⑤停用後合得回來 ⑥`mapping list` 有 trace.raw_index、值 `traces-apm-*`。全部通過 |
| 第一版沒壞、其他沒壞 | `tests/mapping_e2e.py`、全測試（含 helm） | 6 項都過；881 個過 |

## Act

- **假設 metrics 與 trace 的 label 路徑一樣**（同一個來源的 agent）。不一樣時（例如 metrics 經過 Collector 改名、trace 沒有），trace 的部門、群組會變成沒有（落到 `unassigned`）；要分開就得在 `EDITABLE` 另開 `trace.dept`、`trace.group`。
- **改群組會換 merged 的索引**（`traces-merged-<group>`）：改之前合成的留在舊索引，讀的人用 `traces-merged-*` 不受影響，但保留天數、權限若依索引名分要注意。索引名只處理大寫；群組值有空白、`/` 這種字元還是會寫不進去。
- **raw 索引的覆寫一個來源一個值**：trace.yaml 同一個來源列了兩個索引時，覆寫會把兩個換成一個（可以寫逗號分隔的多個索引）。
- trace id、耗時、上一層 span 這些結構欄位照計畫不開放。
