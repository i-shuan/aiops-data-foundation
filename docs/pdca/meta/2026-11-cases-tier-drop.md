# 歷史 case 補根因分析、P0 服務升嚴重度、baseline 往下掉（2026-11）

**狀態**：已完成　　**日期**：2026-11-07　　**相關**：分支 `claude/rca-cases-severity-shift`（從 `claude/mapping-trace` 開，那條還沒合回 master）；問題記在 [PDCA 現況與待辦](../README.md)
**模組**：⑥（嚴重度）、⑦（根因分析）、④（動態基準線學習 (Dynamic Baseline Learning) 的重新學）

## Plan：這一次要達到什麼

使用者的三個問題：

1. **metrics、trace、log 已經有根因分析，歷史 case 能補哪一塊？** 當下的資料回答「哪裡壞了」（哪一段慢、錯從哪開始）；回答不了「為什麼、上次是什麼、先查哪個」。要做的四件：
   - (1) **線索排順序**：當下資料**已經證實有變化**的段（變慢、錯變多），過去相似的 case 也出現過的，在報告裡排前面、寫出是哪幾件；當下資料沒變化的不列（舊答案不能取代證據）。
   - (2) **先查什麼清單**：每一類根因（db、network、config_change…）一份「先查什麼」，放在情境資料檔；報告的分類那一段列出這一類的清單。
   - (3) **學權重**：`similar-eval --learn-weights` 已經有（case ≥ 50 件才學、只印不寫）；這次只確認、寫進文件，不另做。
   - (4) **考卷**：改了根因分析的程式後，拿已確認的事故重跑根因分析，看「慢在哪一段、錯從哪開始」跟當時確認的指紋還對不對得上；結果存成檔，下次比較有沒有變差。資料過了保留期的那件略過、列出來。
2. **緊急的 P0 跟門檻有什麼差別？** 門檻決定「算不算異常」，嚴重度決定「要不要叫人」。現在嚴重度不看服務多重要。要做：`rca.yaml` 的 `alerts.tiers` 列 P0 服務（名字或 regex），P0 的件嚴重度升一級（warning → high、high → critical），換版後的上限也跟著鬆一級（P0 換版壞了要叫人）。**門檻不動**（P0 要更早報，用靜態臨界值規則 (Static Threshold Rules) 或 `confirm_points: 1`，文件說明）。
3. **baseline 突然往下掉，通知放寬？移動區間？依特徵？** 依特徵：流量（兩個方向都判）往下掉照常報；延遲、錯誤（只判往上）往下掉是好事，不通知，但要提「從這裡重新學」（`reset_point`，人核准），不然中位數要約 2 週才跟上，這段期間退回舊水準不會報。現在的漏洞：只判往上的序列，往下的點不會記成異常，`shift` 看不到。要做：`shift` 對只判往上的序列另外用「往下」打一次分（只在記憶體，不寫異常、不通知），一樣用一階跳變的條件判斷，提 `reset_point`。

**不做**：自動寫回學出來的權重；P0 的門檻自動變嚴；往下掉自動重新學（都要人核准）。

## Do

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `aiops-engine/src/llm/similar.py` | `segment_hints`（這次已證實有變化、過去也出現過的段，件數多的排前面）、`checklist`（同一類事故勾過的排前面、不夠用預設補）、`format_hints`、`confirmed_cases` | 1-(1)(2)；段名兩邊都去數字再比（匯入的、舊的指紋可能沒正規化） |
| `aiops-engine/src/incident/alert_service.py` | `_case_hints`：分類之後接線索與清單，存進 `findings.case_hints`；讀不到事故不擋 | 接進告警流程 |
| `aiops-engine/src/common/scenario/base.py`、`it_otel.yaml` | `Scenario.checklists`；`domain.checklists` 每一類 3～5 項 | 先查清單的預設值是情境的事（工廠的類別不一樣） |
| `aiops-engine/src/incident/rca_eval.py`（新）、`interface/cli/rca.py` | `investigator rca-eval [--save] [--compare]` | 1-(4) 考卷 |
| `aiops-engine/src/incident/alert_severity.py`、`incident/config/rca.py`、`alert_service.py` | `alerts.p0_services`；`is_p0`、`escalate`（算完再升一級） | 2：只動嚴重度，門檻不動 |
| `aiops-engine/src/detect/series_ops/level_shift.py` | `quiet_drops`：只判往上的序列用 `detect_series` 換成 `down` 打分（不寫 ES）；併進 `find_shifts`，理由多一句；`cmd_shift` 收 `repo=` | 3：往下的點本來不在 `aiops-anomalies`，`shift` 看不到 |
| 1-(3) 學權重 | 沒改：`similar-eval --learn-weights` 已經有（≥ 50 件才學、只印不寫） | |

## Check

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 線索只列這次有變化的段、排序、段名去數字 | `tests/test_case_hints.py::Hints` | 這次沒變慢的 redis 段過去常出事也不列；件數多的在前；`id=123` 對得上 `id=#` |
| 先查清單：勾過的排前、預設補、沒分類不列 | 同上 | 如預期 |
| 告警流程接上、存進 findings、讀不到不擋、`similar: false` 不列 | `AlertHook` | 如預期 |
| 考卷：對／錯／略過（沒 trace、分析失敗）／不考；時間窗；存檔、比較、變差回 1 | `Exam` | 如預期；一開始抓到段名沒正規化對不上，改成兩邊都去數字 |
| P0：名字或 regex 整個對上、壞的 regex 不掛；升一級；換版上限也升 | `tests/test_severity_p0.py`、`test_severity.py::GradeAlert` | 如預期；告警流程裡 high → critical |
| 往下掉：不寫異常、`quiet_drops` 抓得到、只看只判往上的、`shift` 提一筆 | `tests/test_shift.py::QuietDrops`（記憶體版 repo：錯誤率 30% → 2%） | 如預期 |
| **真的 ES** | `tests/cases_tier_drop_e2e.py` | ①4 週歷史、wip-worker 延遲最後一天 120 → 40ms：學到 baseline、detect 不判成異常 ②`shift` 只提 wip-worker 的 `reset_point`（proposed、寫「只判往上」）③APM 合成資料那一件 warning，列 P0 後 high、why 寫 P0 ④放一件確認過的事故（同一段、db 類、勾過 DB 連線池）：報告有線索（`wip-worker › SELECT LOT_HISTORY → oracle`，這次每個多 1127 ms）與「像 db 類：先查什麼」（DB 連線池排第一，其他是預設）、`findings.case_hints` 有那件 ⑤`rca-eval` 1/1、`--compare` 變差 0 件回 0。全部通過 |
| 其他沒壞 | 全測試（含 helm）；設定快照只多了 `p0_services: []` | 895 個過 |

e2e 寫的過程發現的：規則的異常記在評估的時間點（不是出事那幾分鐘），根因分析的窗要 `rca_lead` 往前多看才看得到出事的 trace——正式環境 IT 是 0，規則多的話要考慮設。

## Act

- **線索與清單要有已確認的事故才出得來**：現在沒有真實事故單，報告不會多這兩段。匯入（`import-incidents`）後才有用；`checked`（查過什麼）是回饋表單勾的，沒人勾就只剩預設清單。
- **考卷的答案是事故的指紋**：自動開的事故，指紋是當時根因分析算的，老師傅「確認」不一定有看段對不對；`corrected` 的才是人改過的。要更可靠，回饋表單要讓人改「根因在哪一段」。
- **trace 保留期**（raw 2 天、merged 依 ILM）過了的事故考不了，考卷只能考最近的；長期要把答案的 trace 另存（評測集）。
- **P0 只升嚴重度**：P0 要更早報還是要逐條寫規則或 `confirm_points: 1`；P0 清單在 `rca.yaml`，沒有跟服務目錄連。
- **往下掉的判斷每天一次**（`shift` 04:00），往下掉後最多一天才提；核准前要人確認「是真的變好，不是量變少或收資料出問題」——量變少的話流量那條會報。
- 學權重照舊只印不寫；case ≥ 50 件後要不要排程跑、寫成建議，等有真實事故再決定。
