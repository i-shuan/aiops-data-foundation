# 規則草稿把門檻算出來、核准就生效；行事曆拿掉國定假日（2026-10）

**狀態**：完成（docker ES 實測：核准 → rules 自動載入 → 第 2 次事故的時間觸發）　　**日期**：2026-10-25　　**相關**：分支 `claude/rule-threshold`；[end-to-end-flow.md「過度設計」](../end-to-end-flow.md) 的前兩列；[loop-update](2026-10-loop-update.md) 的 Act「規則草稿的門檻要人填」
**模組**：⑨（建議）、④（規則）、②（行事曆）

## Plan：這一次要達到什麼

- **需求**：
  1. 同根因 3 次提的「升級成規則」草稿，門檻那格是 `<填：那幾次事故時的 p95，秒>`。人要自己翻三份報告找數字、再手動抄進 `rules.yaml`：看起來自動，其實沒省到事。核准了也不會生效（規則只從 `rules.yaml` 讀）。
  2. `calendar.yaml` 預設抓台灣國定假日，每個假日變一筆「不學、不報」。工廠 24×7，假日跟平日一樣，這會把假日的資料從 baseline 挖掉，`adjust list` 也被幾十行假日淹掉。歲修才是真的要列的，而且一次歲修一筆，不合併。
- **目的**：
  1. 門檻算出來：那三件事故各自的延遲峰值取**最小值**當門檻（三件都抓得到），規則的查法直接用那條 baseline 序列的定義（同一個 index、欄位、切法），只多加「只看這個服務」。人只要 `adjust approve`；**核准後 `detector rules` 自動載入這條規則**，不用改 yaml。算不出來（那幾件沒有延遲類的異常）就老實寫「算不出」，不能核准。
  2. 拿掉國定假日來源；歲修一筆一筆寫在 `calendar:`（`type: maintenance`），格式不變。
- **範圍**：`feedback.suggest_rules`、`cmd_feedback`、`cmd_adjust approve`、`cmd_rules`、`calendar.yaml` 與相關文件。不做：規則草稿的回測（規則沒有「現行 vs 建議」可比，核准前用 `rules --at <那三件的時間>` 看會不會觸發）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/detector/feedback.py` | `rule_threshold(incs, anomalies_of)`：每件事故裡延遲類（series 含 latency／p95／duration）異常**最低的那個值**，再取三件的最小值；`suggest_rules(..., anomalies_of, series_specs)`：規則的 `index`／`filter`／`by`／`value` 直接抄那條 baseline 序列的定義，`filter` 多加「只看這個服務」（三件同一支 API 再多加 route）；`threshold` 是數字，`_source` 記用哪條序列、三件的值；算不出就 `threshold: None`、`_note` 說清楚 | 門檻要算出來；查法跟 baseline 一樣，量才對得上（同一個欄位、同一種聚合） |
| `engine/detector/cmd_feedback.py` | `_anomalies_of`：用事故的 `anomaly_ids` 去 `aiops-anomalies` mget（series、value、keys）給 `suggest_rules` | 門檻的材料 |
| `engine/detector/cmd_adjust.py` | `approve`：`rule_suggestion` 的 `threshold` 不是數字就拒絕；核准訊息說「下一次 rules 就會載入」 | 算不出的不能核准 |
| `engine/detector/cmd_rules.py` | `approved_rules(es, cfg)`：從 `aiops-tuning` 讀 `rule_suggestion`、`status: active`、門檻是數字的，去掉 `_` 開頭的註記，跟 `rules.yaml` 的一起跑；讀不到只印一行、照跑 yaml 的 | 核准就生效，不用改 yaml |
| `engine/configs/baseline/calendar.yaml` | 拿掉 `taiwan` 來源（留註解：週休的辦公室系統才打開）；假資料來源 `enabled: false`；`calendar:` 的範例改成兩次歲修各一筆（`type: maintenance`、`match: {namespace: "^fab1-"}`）＋ UPS 維護 | 工廠 24×7；歲修一次一筆、不合併 |
| 文件 | baseline README（行事曆段、`feedback suggest` 列、還沒做）、`docs/rca/README.md`、`factory-later.md`、`end-to-end-flow.md` 的過度設計表（兩列標已改） | 跟程式一致 |
| 測試 | `tests/test_feedback.py`（門檻＝各件最低值的最小值、同一支 API 才加 route、算不出 → None、沒給異常也提）、`tests/test_cmd_rules.py`（新：只載 active 且門檻是數字的、`_` 註記不進規則、tuning 讀不到退回 yaml）、`tests/loop_e2e.py`＋`tests/rules_loop.yaml`（門檻約 450、核准、`rules --at` 第 2 次事故時間觸發 1 個、平常時間 0 個） | |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 門檻算出來、三件都抓得到 | `loop_e2e`：三件 mes-api db 事故（synth.latency 各點 451～526） | 建議「synth.latency ≥ 449.1」；`_source.floors` = 456.4、451.1、449.1 |
| **第一次用峰值取最小值，實測不觸發** | 第一版門檻 = 各件峰值（517、521、526）取最小 516.9；`rules --at 10:12`（第 2 件進行中） | 觸發 0 個：10:10 那點 475 < 517。峰值只有在最高的那 5 分鐘才過門檻，跟「一跳就報」的目的相反。改成各件**最低**的異常值再取最小 |
| 核准後自動生效 | `adjust approve` → `rules --config tests/rules_loop.yaml`（yaml 裡 0 條） | 印「從 aiops-tuning 載入 1 條核准的規則：mes-api.db.recurring」；10:12 觸發 1 個、12:00 觸發 0 個 |
| 算不出門檻不能核准 | 單元測試：事故只有錯誤率異常 | `threshold: None`，`_note`「算不出門檻」；`approve` 擋（程式路徑，沒在 e2e 走） |
| 查法跟 baseline 一樣、只看這個服務 | e2e 看草稿 | `index: aiops-synth`、`value: {avg: latency_ms}`、`filter: term service=mes-api` |
| 行事曆不再載假日 | `yaml.safe_load`；`adjust list` | sources 只剩停用的假資料；list 不再有幾十行假日 |
| 全部 | pytest 302、ruff、`loop_e2e` | 全過 |

## Act：Check 後沒改善的、還沒解決的

- **門檻用「最低的異常值」會偏鬆**（在什麼情況下發現：改法的時候）：那個值已經被 baseline 判成異常，但離平常可能只差一點；真實資料跑過要看這條規則的誤報率，太吵就改成各件的中位數。核准前只能用 `rules --at <事故時間>` 看會不會觸發，沒有像 override 那樣的回測。
- **只處理延遲類**：根因是「錯誤率」的事故（series 不含 latency／p95／duration）算不出門檻，只提醒不給規則。
- 規則一旦核准就一直有效，**沒有過期**；之後要加 `expires_at` 或「連續 N 週沒觸發就提醒拿掉」。
- 歲修的範例是假的日期、假的 namespace，真的歲修日期要管廠務的人填。
