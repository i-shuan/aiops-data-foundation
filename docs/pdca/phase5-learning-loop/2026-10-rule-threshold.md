# 規則草稿把門檻算出來、核准就生效；行事曆拿掉國定假日；第一階段只開 4 個 CronJob（2026-10）

**狀態**：完成（docker ES 實測：核准 → rules 自動載入 → 第 2 次事故的時間觸發）　　**日期**：2026-10-25　　**相關**：分支 `claude/rule-threshold`；過度設計的盤點（[architecture.md「哪裡過度設計」](../../architecture.md#11-哪裡過度設計)）；[loop-update](2026-10-loop-update.md) 的 Act「規則草稿的門檻要人填」
**模組**：⑨（建議）、④（規則）、②（行事曆）

## Plan：這一次要達到什麼

- **需求**：
  1. 同根因 3 次的「升級成規則」草稿，門檻是 `<填：那幾次事故時的 p95，秒>`：人要翻三份報告找數字、手抄進 `rules.yaml`；核准了也不生效（規則只從 `rules.yaml` 讀）。
  2. `calendar.yaml` 預設抓台灣國定假日，每個假日一筆「不學、不報」。工廠 24×7，這會把假日資料從 baseline 挖掉，`adjust list` 也被幾十行假日淹掉。真的要列的是歲修，一次一筆、不合併。
  3. `deploy/` 10 個 CronJob 一起上 K8s：第一個月的誤報很難分是 baseline、Pod 同儕橫向比對 (Peer Comparison)、trace 段還是規則報的。
- **目的**：
  1. 門檻算出來（取法見 Do）；規則的查法直接用那條 baseline 序列的定義，只多加「只看這個服務」。人只要 `adjust approve`，**核准後 `detector rules` 自動載入**，不用改 yaml。算不出就寫「算不出」，不能核准。
  2. 拿掉國定假日來源；歲修一筆一筆寫在 `calendar:`（`type: maintenance`），格式不變。
  3. 第一階段只開 rules、detect、alert、train（故事跑得通的最少集合）；其他 6 個放 `cronjobs-stage2.yaml`，kustomization 預設註解，四個跑穩再一個一個開。
- **範圍**：`feedback.suggest_rules`、`cmd_feedback`、`cmd_adjust approve`、`cmd_rules`、`calendar.yaml` 與相關文件。
- 不做規則草稿的回測：規則沒有「現行 vs 建議」可比，核准前用 `rules --at <那三件的時間>` 看會不會觸發。

### 門檻的取法

| 取法 | 實測（三件 mes-api db 事故） | 結論 |
|---|---|---|
| 各件峰值取最小 | 峰值 517、521、526 → 516.9；`rules --at 10:12` 觸發 0 個（10:10 那點 475 < 517） | 不用：只有最高那 5 分鐘過門檻，跟「一跳就報」相反 |
| **各件最低的異常值，再取最小（採用）** | 最低值 456.4、451.1、449.1 → 449.1 | 三件都抓得到 |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/detector/feedback.py` | `rule_threshold(incs, anomalies_of)`：每件延遲類異常的最低值，再取三件最小 | 門檻要算出來 |
| 同上 | `suggest_rules(..., anomalies_of, series_specs)`：查法抄那條 baseline 序列 | 同一欄位、同一聚合，量才對得上 |
| 同上 | `filter` 加「只看這個服務」；三件同一支 API 再加 route | 只針對這個服務 |
| 同上 | `threshold` 是數字；`_source` 記序列與三件的值；算不出 → `threshold: None`＋`_note` | 看得到門檻從哪來 |
| `engine/detector/cmd_feedback.py` | `_anomalies_of`：用事故的 `anomaly_ids` mget `aiops-anomalies`（series、value、keys） | 門檻的材料 |
| `engine/detector/cmd_adjust.py` | `approve`：`rule_suggestion` 的 `threshold` 不是數字就拒絕；核准訊息說「下一次 rules 就會載入」 | 算不出的不能核准 |
| `engine/detector/cmd_rules.py` | `approved_rules(es, cfg)`：讀 active、門檻是數字的 `rule_suggestion`，去掉 `_` 註記 | 核准就生效，不用改 yaml |
| 同上 | 跟 `rules.yaml` 的一起跑；讀不到只印一行、照跑 yaml 的 | tuning 出問題不影響既有規則 |
| `engine/deploy/gen-cronjobs.py`、`cronjobs.yaml`、`cronjobs-stage2.yaml`（新） | `STAGE1`（rules、detect、alert、train）進 `cronjobs.yaml`，其他 6 個進 stage2 | 第一個月偵測來源少，誤報容易歸因 |
| `kustomization.yaml` | stage2 註解掉 | 同上 |
| deploy README | 加「階段」欄和開的順序 | — |
| `engine/configs/baseline/calendar.yaml` | 拿掉 `taiwan` 來源（留註解：週休的辦公室系統才打開）；假資料來源 `enabled: false` | 工廠 24×7 |
| 同上 | `calendar:` 範例改成兩次歲修各一筆＋ UPS 維護 | 歲修一次一筆、不合併 |
| 文件 | baseline README、`docs/rca/README.md` | 跟程式一致 |
| 同上 | `architecture.md` 過度設計表 | 同上 |

- 延遲類：series 含 latency／p95／duration。
- 規則的 `index`／`filter`／`by`／`value` 直接抄那條 baseline 序列的定義。
- `approved_rules` 從 `aiops-tuning` 讀。
- 歲修範例：`type: maintenance`、`match: {namespace: "^fab1-"}`。

| 測試檔 | 加了什麼 |
|---|---|
| `tests/test_feedback.py` | 門檻＝各件最低值的最小值；同一支 API 才加 route；算不出 → None；沒給異常也提 |
| `tests/test_cmd_rules.py`（新） | 只載 active 且門檻是數字的；`_` 註記不進規則；tuning 讀不到退回 yaml |
| `tests/loop_e2e.py`＋`tests/rules_loop.yaml` | 門檻約 450、核准、`rules --at` 第 2 次事故時間觸發 1 個、平常 0 個 |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 門檻算出來、三件都抓得到 | `loop_e2e`：三件 mes-api db 事故（synth.latency 各點 451～526） | 建議「synth.latency ≥ 449.1」；`_source.floors` = 456.4、451.1、449.1 |
| 核准後自動生效 | `adjust approve` → `rules --config tests/rules_loop.yaml`（yaml 裡 0 條） | 印「從 aiops-tuning 載入 1 條核准的規則：mes-api.db.recurring」 |
| 觸發時間對 | 同上，`rules --at` | 10:12 觸發 1 個、12:00 觸發 0 個 |
| 算不出門檻不能核准 | 單元測試：事故只有錯誤率異常 | `threshold: None`，`_note`「算不出門檻」；`approve` 擋（程式路徑，e2e 沒走） |
| 查法跟 baseline 一樣、只看這個服務 | e2e 看草稿 | `index: aiops-synth`、`value: {avg: latency_ms}`、`filter: term service=mes-api` |
| 行事曆不再載假日 | `yaml.safe_load`；`adjust list` | sources 只剩停用的假資料；list 沒有幾十行假日 |
| 第一階段只有 4 個 | `test_deploy.py`：兩個檔跟產生器一致；`cronjobs.yaml` 恰好 4 個；kustomization 沒列 stage2 | 過 |
| 全部 | pytest 302、ruff、`loop_e2e` | 全過 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 門檻用「最低的異常值」偏鬆 | 改門檻取法時 | 那個值被判成異常，但離平常可能只差一點 | 真實資料看這條規則的誤報率，太吵改用各件中位數 |
| 規則核准前沒有回測 | — | 只能用 `rules --at <事故時間>` 看會不會觸發 | 待排 |
| 只處理延遲類 | — | 根因是錯誤率的事故算不出門檻，只提醒不給規則 | 待排 |
| 規則核准後一直有效，沒有過期 | — | 不再適用的規則留著 | 加 `expires_at`，或連續 N 週沒觸發就提醒拿掉 |
| 第二階段何時開沒有客觀條件 | README 寫「跑穩、看過一輪誤報」 | 靠人判斷 | 用 `metrics` 的誤報率當門檻（例如連續 2 週 < 30%） |
| 歲修範例是假的日期、namespace | — | — | 真的歲修日期要管廠務的人填 |
