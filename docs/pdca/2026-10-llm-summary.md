# LLM 摘要：RAG 的「生成」（2026-10）

**狀態**：完成骨架（template 實測；真的模型沒驗過）　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-plans`；前置 [相似事故檢索](2026-10-similar-incidents.md)；[feedback/llm-mlops.md](../feedback/llm-mlops.md)（LLMOps 的定案）；主線 [product-loop](2026-10-product-loop.md) 第 7 步
**模組**：⑦ 根因分析、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：根因報告是給工程師看的：表格、段落、瀑布圖、log 樣式，加上相似事故。值班半夜收到時要先讀懂「到底怎麼了、要不要叫人、先做什麼」，現在要自己從報告裡拼。
- **目的**：報告最前面多一段**三到五句話**：原因（最可能是什麼）、影響（哪些服務、哪些 API、多久了）、建議先做什麼（引用過去事故的處置）。每一句都要標出**依據**（哪一筆異常、哪一段 trace、哪一件過去事故），沒有依據就說「不確定」。
- **範圍**：
  - 做：給 LLM 的輸入包（結構化，不丟原始資料）；prompt；輸出格式；評測集與評測方法；prompt 和模型的版本紀錄；token 用量。
  - 不做：讓 LLM 決定要不要告警（**只加註，不擋告警、不刪告警**，統計層照常報）；讓 LLM 自己去查資料（工具呼叫，下一輪）。

### 輸入包（約 4,000 token）

| 內容 | 從哪來 |
|---|---|
| 這件告警：服務、時間、持續多久、異常的指標（值、平常值） | 告警出口的 `aiops-alerts` |
| 根因分析的前幾名：慢在哪一段（自己的時間 vs 平常）、錯從哪開始、新出現的 log 樣式 | `investigator` 的結構化輸出 |
| 最像的 3 件過去事故：根因、處置、怎麼看出來的、為什麼像 | [similar-incidents](2026-10-similar-incidents.md) |

### 防呆

- 每一句要附依據的 id；**引用了不存在的 id 就整段不顯示**（程式檢查，不靠 LLM 自律）。
- 只用輸入包裡的東西，不准自己編服務名、數字。
- LLM 掛了、逾時：報告照出，只是沒有這一段。

### 要你決定

| 項目 | 選項 | 影響 |
|---|---|---|
| 用哪個模型、在哪跑 | 雲端 API（要能連外、資料出公司）／內網自架（要 GPU） | 決定資料能不能出去、成本、品質 |
| 誰能看 | 跟報告一樣（各 group 自己的） | — |

### 驗收（評測集）

1. 評測集：20～30 件已確認的事故（[similar-incidents](2026-10-similar-incidents.md) 收的同一批）。每件跑一次，看：
   - **根因命中**：LLM 說的最可能原因，跟老師傅確認的根因分類一樣（目標先訂 70%，估的）。
   - **捏造率**：引用不存在的依據、或數字跟輸入不符的比例（目標 0；程式檢查）。
   - 值班看得懂：找兩三位值班，盲測「有這段 vs 沒這段」誰比較快講出原因。
2. 改 prompt、換模型之前一定重跑評測集，分數掉了不上（版本記在 `aiops-tuning` 或檔案）。
3. token：每件約 4,000 輸入；一天 50 件約 20 萬 token（[system-stages 第 8 節](../system-stages.md)）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/core/llm.py`（新） | `build_pack(alert, findings, similar)`：輸入包，每個項目一個 id（`A` 異常、`S` 慢的那段、`E` 錯誤源頭、`L` 新 log 樣式、`I` 過去事故），只放數字和一句話，不放原始資料。`validate(text, pack)`：每一句要有 `[id]`、id 要存在、最多 6 句，否則整段不顯示。`template(pack)`：不叫模型的固定句型（原因、錯從哪、新 log、跟哪件像＋當時處置、建議先怎麼確認）。`_call_anthropic`、`_call_openai`。`summarize(pack, provider)`：模型掛了退回 template，再驗一次 | Plan 的防呆是程式做，不靠模型自律；沒有模型也要能跑 |
| `engine/investigator/cmd_alert.py` | 新的一件：`build_pack` → `summarize` → 通過檢查才放報告最前面（引言 `>`），存 `llm_summary`、`llm_provider`；沒通過只印原因 | 只加註，不擋告警 |
| `engine/configs/rca/rca.yaml` | `alerts.llm: true`、`llm_provider`（註解） | provider 預設看環境變數 `LLM_PROVIDER` |
| `engine/feedback_web/app.py` | 表單顯示「AI 摘要（只是參考）」 | 老師傅看得到，但標明參考 |
| `engine/tests/test_llm.py`（新） | 11 個：id、沒依據的句子、不存在的 id、太長、template 通過檢查且引用相似事故、沒料時不顯示、模型 401 退回 template、模型捏造 id 整段不顯示、模型正常輸出保留 | |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 每句附依據、引用相似事故 | `loop_e2e` 第 2 件（provider `template`） | 三句：「mes-api 的 synth.latency 異常：現在 521.2，平常 119.4 [A1]」「跟 2026-10-05T02:05 的事故相似（…），當時的根因是 [db] …，當時的處置：重跑 analyze [I1]」「建議先照上次的方式確認：只有 lots 那張表的查詢慢… [I1]」；都通過 `validate` |
| 捏造的依據整段不顯示 | 單元測試：假的模型回「是 Redis 的問題 [R9]」 | `ok: false`、`text` 空、原因列出 R9 |
| 模型掛了報告照出 | 單元測試：`_call_openai` 丟 401 | 退回 template，`provider: template（openai 失敗：401…）` |
| 真的模型 | 這台的 `LLM_API_KEY`（gpt-6-luna 閘道）實打 | **401，沒驗到**；所以 e2e 用 template |
| 沒料時不硬寫 | 沒有 findings、沒有相似事故 | 空、不顯示 |

## Act：Check 後沒改善的、還沒解決的

- **真的模型一次都沒跑過**（在什麼情況下發現：這台的 LLM 閘道回 401）。prompt 寫好了、兩種 API 的呼叫寫好了，但模型會不會乖乖每句附 id、會不會太常被 `validate` 整段擋掉，不知道；要有 key 的環境跑 20 件看通過率。
- **評測集沒有**（Plan 的驗收 1）：根因命中率、捏造率要 20～30 件真實事故；現在只能說「捏造的一定被擋」。
- **prompt、模型沒有版本紀錄**（Plan 範圍有）：`llm_provider` 只記 provider，沒記模型名、prompt 的 hash；要加到 `aiops-alerts` 每一件。
- **token 用量沒算**：輸入包很小（幾百 token），比 Plan 估的 4,000 少很多，因為沒放原始資料；但也表示模型看到的資訊很少，摘要的價值大半來自相似事故那幾句。
- template 的句子只用第一名（第一段、第一個錯誤源頭、第一件相似事故），**第二名以後都沒講**；模型版會講得更完整，但要先驗。
- 時間顯示 UTC（同 [similar-incidents](2026-10-similar-incidents.md) 的 Act）。
