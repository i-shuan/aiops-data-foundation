# 回饋的一句話 LLM 抽取＋採納率欄位（POC 任務 2）（2026-10）

**狀態**：已完成（單元測試、26 句語料評測、自己的 ES 9.5.3 容器 e2e）　　**日期**：2026-10-29　　**相關**：[POC 驗收計畫](../meta/2026-10-poc-acceptance.md) 的任務 2；KPI「回饋標記解析率 ≥ 90%」「RCA 建議採納率 ≥ 60%」
**模組**：⑨ feedback、⑦ 根因分析

## Plan：這一次要達到什麼

- **需求**：回饋表單只有結構化欄位（真的／誤報、根因分類、處置…）。值班最自然的是寫一句話（「這是每月初的批次匯出報表，屬於預期內負載」），系統要能自己把它變成可以採取行動的資料；KPI「採納率」沒有對應的題目可算。
- **目的**：
  - 表單多一格自由文字；送出後 LLM 抽成四欄 `feedback_type`、`identified_cause`、`target_service`、`suggested_action`，存在 `aiops-feedback` 那筆上；`suggested_action` 對得到既有調整種類的，同時寫一筆 `aiops-tuning`（`status: proposed`、`source: feedback`、`reason` 帶原文），**人核准才生效**（審核機制不動）。
  - 表單多兩題「可能根因有參考價值」「建議的排查動作有參考價值」（是／否／沒看）；`detector metrics` 算採納率＝兩題正面 ÷ 有回答的。
  - ≥ 20 句值班語料、人工標準答案、一支評測腳本量「四欄全對」的比例；真的模型與 `template` 退路各量一次。
- **範圍**：做抽取、寫入、表單、API、指令 `feedback add --text`、評測、文件。不做：自動生效、從文字抽 cron 時段（缺就請人補）、Grafana 儀表板顯示抽取結果。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `aiops-engine/src/core/llm.py` | 新增 `extract_feedback(text, alert, provider)`：同一套 provider（`LLM_PROVIDER`、`_call_openai`／`_call_anthropic`）；`EXTRACT_PROMPT` 給四欄定義、原因代號清單 `CAUSE_CODES`（24 個）、告警的服務當 `target_service` 預設；`parse_json_object` 容許 ```` ```json ```` 圍欄與前後多話；`validate_extraction` 驗四欄齊全、enum 合法、代號是 snake_case（大小寫、空白、減號正規化）、服務名合法（空的補告警的）；`template_extract` 是關鍵字版的退路 | 跟摘要同一個原則：模型回的要過程式檢查，不靠模型自律。模型掛了、回空的 → 退回關鍵字版（provider 寫明）；模型回了但格式不對 → **不抽、不猜**（只存原文）。整個函式不丟例外，回饋寫入不會因抽取失敗而失敗 |
| `aiops-engine/detector/feedback.py` | `FEEDBACK_MAPPING` 加 `feedback_text`、`extract.{ok, provider, reason, 四欄}`、`tuning_id`、`rca_cause_useful`、`rca_action_useful`；`RCA_USEFUL`；`CAUSE_CATEGORY`（代號 → 表單根因分類）；`suggest_from_text(fields, alert, text)`：`suppress_during_cron` → `recurring_override`（`match.service`、`effect.mute`、**沒有 `schedule`**、`missing` 列缺什麼）、`widen_threshold` → `override`（這件的序列、z × 1.4）、`add_rule` → `rule_suggestion`（`threshold: None`）、`none` → 不提 | mapping 是 `dynamic: false`，不加就查不到。對應只用既有的 kind，審核流程（`backtest`、`adjust approve`）原樣可用。cron 時段文字通常沒寫，瞎猜會寫錯資料：留空，`adjust.problem()` 會標「cron 要 5 欄」、補齊前不可能生效 |
| `aiops-engine/detector/tuning.py` | `propose_from_text(es, fields, alert, text, by, now)`：寫 `aiops-tuning`，id ＝ `doc_id("feedback-text", alert_id, by, kind)` | 網頁與指令共用；同一人對同一件、同一種 kind 再送是覆蓋不是重複 |
| `aiops-engine/feedback_web/app.py` | `submit()` 收 `feedback_text`、兩題；有文字才抽取、才寫建議；回應多 `extract`、`tuning_id`（沒給文字時回應不變）；寫建議失敗記 `tuning_error`、回饋照寫；小網頁加一格 textarea、兩題 radio、送出後的提示 | 抽取、寫建議都是加值，不能讓回饋本身失敗；小網頁是 Grafana 表單的備案，欄位要一致（測試比對） |
| `grafana/code/dashboards/aiops-feedback.json` | 加 `rca_cause_useful`、`rca_action_useful`（radio，永遠顯示）、`feedback_text`（textarea，verdict 之後）；送出成功的提示讀回應，說有沒有提建議／沒抽成 | 元素 id ＝ API 的 JSON key（`tests/test_grafana_form.py` 擋不一致） |
| `aiops-engine/detector/cmd_metrics.py` | `compute()` 多 `rca_useful`、`rca_answered`、`rca_adoption_rate`（兩題正面 ÷ 有回答；`not_seen`、沒填不算；一件多筆只算最後一筆）、`with_text`、`extracted`、`extract_rate`；`format_metrics` 多兩列 | KPI 採納率；抽取率是「解析率」在正式環境的量法（語料只是上線前的估計） |
| `aiops-engine/detector/cmd_feedback.py`、`__main__.py` | `feedback add` 多 `--text`、`--alert`；沒給 `--verdict` 就用抽出來的 `feedback_type`，**但只信真的模型**（provider 是 `template…` 的不拿來當 verdict，擋下請人給）；對得到的寫建議並印 id | 關鍵字退路把「是供應商的問題、不是我們的程式」判成誤報（Check 第 3 列），讓它決定 verdict 會寫錯資料 |
| `aiops-engine/tests/feedback_corpus.yaml`（新） | 26 句（22 句繁體中文、4 句英文），每句附告警（服務、序列）與四欄標準答案；有爭議的在 `note` 寫理由 | 標準答案在跑模型前定好，跑完不改 |
| `aiops-engine/tests/feedback_extract_eval.py`（新） | 每句跑 `extract_feedback`，印每句對錯、全對率、每欄正確率、走退路幾句、錯在哪類（欄位、feedback_type、suggested_action）；`--env <.env>` 只載入 `LLM_PROVIDER`、`LLM_API_KEY`、`LLM_CHAT_MODEL`、`OPENAI_BASE_URL`，key 不印、不寫檔 | 走退路的句子要分開算，不然「模型的解析率」被退路灌水 |
| `aiops-engine/tests/test_feedback_extract.py`（新）、`test_feedback_web.py`、`test_grafana_form.py`、`test_metrics.py` | 見 Check | — |
| `aiops-engine/configs/baseline/reference.md`、`aiops-engine/configs/rca/README.md`、`grafana/code/README.md`、`docs/pdca/README.md` | `feedback add --text`、`metrics` 兩列、四個 `suggested_action` 對到哪種 kind 與核准前要做什麼、表單欄位對照表、抽取原則 | 規格跟程式放一起 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 解析率（KPI ≥ 90%）：真的模型 | `python3 -m tests.feedback_extract_eval --env ../e2e/local-k3d/.env`（provider `openai`、模型 `gpt-6-luna`）跑兩次 | 兩次都 **26／26（100%）**，四欄各 26／26，走退路 0 句；一次 63 秒、一次 56 秒（每句約 2 秒）。文字點名別的服務的 3 句（#4、#19、#21）`target_service` 都覆蓋對；「不是我們的程式」（#10）、「其實是」（#12）這類否定句沒被帶偏 |
| 解析率：`template` 退路 | `LLM_PROVIDER=template python3 -m tests.feedback_extract_eval` | **22／26（85%）**，沒到 90%。錯 4 句全在 `feedback_type`（其中 2 句連帶 `suggested_action` 錯）：#7「量太少不該算」、#8「換版期間的抖動」、#13「每天凌晨…都會這樣」沒有「誤報／不是問題」這類關鍵字，被判成真的；#10「不是我們的程式」命中誤報關鍵字「不是我們」，其實是真的事故。`identified_cause`、`target_service` 26／26。結論：退路靠關鍵字，分不出「用否定句講的誤報」和「用否定句講的真事故」；只當模型掛了的備援，不拿它決定 verdict（Do 最後一列） |
| 模型回的垃圾不會變成資料 | `test_feedback_extract.py`：回純文字、回 JSON 但 `suggested_action: mute_forever`、缺欄位、代號是中文、服務名有空白 | 都 `ok: false` 帶原因，`fields` 是 None；回饋只存原文（`test_feedback_web.FreeTextAndRatings.test_extraction_failure_keeps_text_only`：回饋照寫 2 筆、`extract.ok false`、沒寫 `aiops-tuning`） |
| 模型掛了的退路、永不丟例外 | mock `_call_openai` 丟 401、`_call_anthropic` 回空白、`template_extract` 丟 KeyError | 前兩個退回關鍵字版且 provider 寫「template（openai 失敗…）」「回空的」；第三個 `ok: false`、reason 帶 `KeyError`，不往外丟 |
| 四個 `suggested_action` 對到的 kind、不自動生效 | `test_feedback_extract.MapToTuning`：`suppress_during_cron`、`widen_threshold`、`add_rule`、`none` | `recurring_override` 沒 `schedule` → `adjust.problem()` 回「cron 要 5 欄」、`active()` 不含它；`override` z 7.0、序列 `http\.latency_p95_s\|http\.error_rate`、`match.service` 是抽出來的服務（點名 wip-worker 就對 wip-worker）、proposed 不在 `active()`；`rule_suggestion` threshold None、`root_cause_category` 由代號對應（`external_api` → `external`、不認得 → `other`）；`none` 回 None。`propose_from_text`：同一人同一件同一 kind 的 id 相同（覆蓋），換人不同 |
| 網頁與 API 的新欄位 | `test_feedback_web.FreeTextAndRatings`（假 ES）、`test_grafana_form.Cors.test_api_feedback_with_text_and_ratings`（真的 HTTP） | 一件 2 筆異常 → 2 筆回饋都帶原文、四欄、兩題、同一個 `tuning_id`；`aiops-tuning` 那筆 `by` 是填的人、`reason` 含原文與「缺 schedule」；`suggested_action: none` 不寫建議；寫建議丟例外時回饋仍寫 2 筆、回應帶 `tuning_error`；沒給文字時回應跟以前一模一樣（既有測試原樣過）；兩題填了不合法的值被忽略。儀表板：新元素 id 在服務認得的集合裡、選項跟 `RCA_USEFUL` 一致、`showIf` 空（永遠顯示）、送出程式碼讀 `tuning_id` |
| 採納率、抽取率的算法 | `test_metrics`：4 件，一件兩人標 | 正面 ÷ 有回答＝4／5＝80%（`not_seen`、沒填不算；一件取最後一筆）；有寫文字 2 件、抽到 1 件 → 50%；空資料兩個都是 None、印 `—` |
| 整條線（真的 ES、真的模型） | 自己的 ES 9.5.3 容器（`poc2-es`、`elasticsearch-setup.sh`）、`feedback_web` 用 `openai`：POST 兩件、`adjust list`、`feedback add --text`（退路與模型各一次）、`metrics` | 全部 PASS：月初批次那句 5.3 秒回應，`aiops-feedback` 2 筆帶 `extract`（mapping 是 keyword）、`aiops-tuning` 一筆 `recurring_override` proposed 無 `schedule`；「本來就慢」→ `override` z 7.0 序列 `http\.latency_p95_s`；`adjust list` 列出 override `[proposed]`、對缺 schedule 那筆印「⚠ 略過調整 …：cron 要 5 欄」（看得到、不會生效）；`feedback add --text` 用退路且沒給 `--verdict` → exit 2 什麼都不寫，用模型 → verdict `true_positive`、`rule_suggestion` threshold None、`adjust list` 顯示「升級成規則：mes-api / external（1 件）」；`metrics` 印「RCA 建議採納率 100%（1 題有回答）」「抽成結構化欄位的比例 100%（2 件有寫）」。測完容器已刪、名額已放 |
| 沒弄壞別的 | `cd engine && pytest -q`、`python3 tools/check-docs.py` | 既有測試原樣過：沒給文字時 `submit()` 的回應與寫入跟以前一模一樣、儀表板既有欄位檢查仍過（只多了 3 個 id）；check-docs 連結、索引、舊說法、類型 0 個問題 |

- 測的過程中發現：`feedback add --text` 一開始「沒給 `--verdict` 就用抽出來的」不分 provider，e2e 用退路跑 #10 那句時把真事故寫成 `false_positive`。改成只信真的模型（Do 最後一列），e2e 重跑確認擋下。
- **沒測到的**：`anthropic` provider（只寫了、沒 key）；Grafana 真的瀏覽器操作新欄位（上一輪 [grafana-form](2026-10-grafana-form.md) 驗過外掛的 radio、textarea 型別，這輪只驗儀表板 JSON 與 API）；模型非確定性（跑兩次都 100%，更多次沒跑）；語料是自己寫的，不是真實值班留言。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `suppress_during_cron` 的建議沒有 `schedule`，`adjust list` 只能用「⚠ 略過」那一行顯示它 | 設計時：文字通常沒寫 cron、時長，不猜 | 審核的人要自己補 `schedule`（目前沒有「補欄位」的指令，要改 ES 文件或刪掉用 `adjust add --kind recurring_override --id <同 id>` 重寫） | 加 `adjust edit --id … --cron … --duration …`；或讓 LLM 在文字有寫時段時多抽一個選填的 `schedule`（這輪的四欄不含） |
| `template` 退路 85%，分不出否定句講的誤報／真事故 | 語料評測 #7、#8、#10、#13 | 模型掛了時抽出來的 `feedback_type` 不可信；已經不拿它當 verdict，但 `suggested_action` 仍會提建議（proposed） | 不打算調關鍵字湊數（會對語料過擬合）；正式環境看 `metrics` 的抽取率，退路佔比高再說 |
| 語料 26 句是自己寫的 | Plan 時 | 100% 是對「我寫的句子」；真實值班留言會更短、更口語、夾工號和縮寫 | 試點後把真的 `feedback_text` 匿名化補進語料，重跑 |
| `add_rule` 的 `rule_suggestion` 只有名字沒有門檻 | 設計時：一句話算不出門檻 | 核准前要人看報告決定序列與門檻、寫進 `rules.yaml` | 跟 [rule-threshold](2026-10-rule-threshold.md) 的做法接起來：同服務、同根因確認 ≥ 3 次後由 `feedback suggest` 算門檻，這筆只當提醒 |
| [⑧] Grafana 儀表板沒顯示抽取結果與 `tuning_id` | 範圍外 | 審核的人要用 `adjust list` 或 Kibana 看 | `aiops-learning.json` 加一格列 `source: feedback` 的 proposed 與 `reason` |

- **決定**：採用「LLM 抽四欄 → 只寫 proposed、人核准」；退路只做備援、不決定 verdict；缺的時段不猜。
- **下一輪**：POC 任務 1、3、4（[poc-acceptance](../meta/2026-10-poc-acceptance.md)）；試點後用真實留言重量解析率、用 `metrics` 看採納率。
