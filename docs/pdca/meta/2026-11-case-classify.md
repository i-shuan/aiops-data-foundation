# case 只做告警分類：分類器、報告只給分類、表單預帶、DB 讀 case、precision 自動提建議（2026-11）

**狀態**：已完成　　**日期**：2026-11-03　　**相關**：分支 `claude/case-classify`（從 [model-modules](2026-11-model-modules.md) 的分支接出，用到它的 `Store.incidents`）
**模組**：⑦⑨

## Plan：這一次要達到什麼

- **需求**：相似事故檢索現在把過去 case 的根因、處置、怎麼看出來的整段放進報告、LLM 摘要和通知，等於拿舊藥方開給新病：程式換版後指紋還對得上、根因已經不是那個，會給錯建議。使用者定的原則：**case 只做告警分類**，根因定位從當下的 trace 算、門檻從回饋統計修，都不靠 case。另外公司過往的事故單在 DB，進不來。
- **目的**：
  1. 一件新事故 → 「像哪一類」（db、network、config_change…）＋信心，寫進 `aiops-alerts`；報告、摘要、通知只講分類，不列 case 的處置。
  2. 分類器可評測（留一法準確率）；case 夠多時可學特徵權重。
  3. 回饋表單預帶分類，人改掉就是 corrected，下次分類吃到。
  4. `import-incidents` 能直接從 DB 讀（SQL 一句），排程可定期匯。
  5. 門檻修正多一條訊號：每條序列的 precision 低於 70% 提放寬、高於 95% 且有漏報提收緊（proposed，照舊回測、人核准）。
- **範圍**：不碰根因定位（trace 分段）、不建圖、不改基準線；不動 `feedback suggest` 既有的誤報／漏報規則；DB 讀取只做「SQL → 列」，欄位對應靠 SQL 別名。

### 分類怎麼算

- 指紋（既有）：`slow_segments`、`error_origins`、`services`、`routes`、`patterns`、`series`，權重 4、4、3、2、2、1。
- 候選：`aiops-incidents` 裡 status confirmed／corrected 的 case，分數 = Σ 權重 ×（有交集），≥ 4 分留前 k=3 件。
- 分類 = 前 k 件的 `root_cause_category` 以分數加權投票；信心 = 贏的那類分數 ÷ 前 k 件總分。沒有候選 → 沒有分類（不猜）。
- 評測：留一法，每件確認的 case 輪流當新的，`accuracy`（投票結果 == 它的分類）與既有的 recall@3。
- 學權重：確認的 case ≥ 50 件時才做；每個權重在 0～6 試、座標上升到留一法準確率不再變好；印出建議的權重與前後準確率，人決定要不要寫進 `rca.yaml` 的 `similar_weights`。少於 50 件只印「不夠，維持手設」。

### 怎麼驗

| 要確認的 | 怎麼測 |
|---|---|
| 分類與信心 | 3 件 db、1 件 network 的候選 → db、信心 = db 分數佔比；沒有候選 → None |
| 留一法準確率、學權重 | 合成 case：權重學出來後準確率不低於手設；< 50 件拒絕 |
| 報告、摘要、通知只講分類 | `format_similar`、`llm.template`、`alerts.summary` 的輸出沒有「處置」「當時的根因」；有「像 db 類」 |
| 表單預帶 | `form_data`／`form_json` 有 `root_cause_category`；HTML 的 select 預選 |
| DB 匯入 | sqlite 檔一張表 → `import-incidents --db … --sql …` → 跟 CSV 同樣的 normalize、略過規則 |
| precision 建議 | 序列 reviewed 12 筆、precision 50% → 放寬；98% 且 missed 1 → 收緊；reviewed < 10 不提；已有建議不重提 |
| 沒弄壞既有的 | 593 個測試 |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `investigator/similar.py` | `classify`（前 k 件以分數投票 → 分類、信心、票數、依據哪幾件）、`describe_class`；`format_similar` 改成「分類＋日期／分類／標題／為什麼像」，不列根因、處置；`evaluate_cases`（留一法，不碰 ES，多 `accuracy`）、`evaluate` 改呼叫它；`learn_weights`（座標上升，< 50 件拒絕）；`explain`／`rank`／`search` 多 `weights` 參數 | case 只做分類；評測與學權重是純函式，測試不用假 ES |
| `investigator/cmd_alert.py` | 檢索帶 `alerts.similar_weights`；算 `cls` 寫進 `suggested_category`、`category_confidence`（mapping 加兩欄）；報告段用 `format_similar(…, cls=cls)` | 分類進告警文件，表單、Grafana 都讀得到 |
| `investigator/alerts.py` | 通知摘要的「過去相似：根因；處置」改成「分類：像 db 類（…）；最像 日期（為什麼像）」 | 通知不給舊處置 |
| `core/llm.py` | 輸入包的 `similar` 只留 id、日期、分類、為什麼像，多 `classification`；`PROMPT` 改成「case 只用來說像哪一類，不要照抄處置」；`template` 的相似句改成分類句＋「根因以定位為準，先查那一段的下游」 | 模型抄不到處置；固定句型同樣原則 |
| `feedback_web/app.py` | `form_data` 多 `suggested_category`（舊告警沒有就現場算）、`form_json` 多 `root_cause_category`（Grafana 表單那格的 id）、相似清單不給處置；HTML 的根因分類 select 預選並提示「系統預帶」 | 表單預帶，人改掉就是 corrected |
| `investigator/importer.py`、`__main__.py` | `DbRows(url, sql)`：sqlite 用標準庫、其他用 SQLAlchemy；URL 的 `${VAR}` 換環境變數、印出時遮密碼；`import-incidents --db --sql`，或 `rca.yaml` 的 `incidents_db` | 公司的事故單在 DB，不用先匯 CSV；可排程 |
| `investigator/__main__.py` | `similar-eval` 印分類準確率與 recall@3；`--learn-weights` | 評測分類器 |
| `detector/feedback.py`、`cmd_feedback.py` | `suggest_by_precision`：每條序列 reviewed ≥ 10 時，precision < 70% 提整條放寬 z × 1.4、> 95% 且有漏報提收緊 × 0.8；`feedback suggest` 多跑這條（id `suggest-precision`，有了不重提） | 門檻多一條「整條序列比例」的訊號，跟單一對象誤報 3 次互補 |
| `models/similar.py` | re-export `classify`、`evaluate_cases`、`learn_weights` | M5 的門面 |
| `tests/test_case_classify.py`（新，13 個）；`test_alerts`、`test_llm`、`test_similar`、`test_models` 改 5 處斷言 | 見 Check；改的 5 處是釘著舊措辭（「處置：重跑 analyze」）的，照新原則改 | |
| `configs/rca/README.md`、`docs/aiops-data-reference.md`、`docs/feedback/README.md` | 「過去相似的事故」改寫成「告警分類」；DB 匯入的指令；兩個新欄位 | |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 分類與信心 | 候選 db 9 分、db 7 分、network 4 分 | db、信心 0.8（16/20）、`based_on` 三件；沒候選或候選沒分類 → None、「不分類」 |
| 權重能改變結果 | 同一批候選，`slow_segments` 0、`routes` 6 | 分類從 db 變 network |
| 留一法準確率 | 6 件 db（同一段慢）、6 件 network（另一段）互不重疊 | 12/12、accuracy 1.0；`rows[i]["predicted"]` 有值 |
| 學權重的門檻 | 12 件 | 拒絕：「少於 50 件不學」 |
| 學權重真的會動 | 60 件，兩類只差 `series`（手設權重 1，比同服務的 3 小） | `after ≥ before`；用學出的權重重算 = `after` |
| 報告、摘要、通知只講分類 | `format_similar` 第一行、`template`、`alerts.summary` | 有「像 db 類」「分類：」；沒有「處置」「統計資訊過期」「重跑 analyze」 |
| 表單預帶 | `form_data`／`form_json`／`render_form`；舊告警沒 `suggested_category` | `root_cause_category: db`、select 預選、提示「預帶 db」；相似清單沒有處置；舊告警現場算出 db |
| DB 匯入 | sqlite 兩列（一列缺分類） | 匯 1 筆、略過第 2 筆「缺 root_cause_category」，services 拆成陣列、指紋有 services；`${CASE_PW}` 換進 URL、`str()` 遮掉 |
| precision 建議 | 12 筆 50% → 放寬 z 7、match `{}`；11 真＋1 根因錯＋漏報 1 → 收緊 z 4；12 真沒漏報 → 不提；9 筆 → 不提；83% → 不提 | 全部如預期 |
| 沒弄壞既有的 | 全部測試 | 606 個過（593 ＋ 13 新） |
| 文件 | `tools/check-docs.py` | 0 個問題 |

- **沒測到的**：`similar-eval --learn-weights`、`import-incidents --db` 沒在真 ES／真 DB 上跑（只有 sqlite 與純函式）；SQLAlchemy 那條路沒裝套件、沒跑；Grafana 表單預帶靠的是 Business Forms 外掛「initial 回的 key 對到元素 id」這個既有機制，沒實際開表單看。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 分類的特徵是指紋六組，同根因在不同服務的對不上（之前合成 20 筆 recall 50% 的原因） | 設計時 | 分類只在「同一個地方又出事」時準；跨服務的同類事故要靠 `error_origins`／`patterns` 這種不綁服務的特徵 | 真實事故單匯入後看準確率，不夠再加「錯誤樣式」的正規化特徵 |
| case 沒有版本、沒有時間衰減 | 討論換版後 case 會過時 | 很舊的 case 跟上週的一樣重 | 指紋加 `service.version`，排序時近的優先；跨大版降權 |
| `learn_weights` 是座標上升、整數格 | 設計時 | 可能停在局部最佳；60 件的測試只保證「不變差」 | case 真的到 50 件再看，不夠就維持手設 |
| 排程表沒加 `import-incidents` 的 job | 範圍 | 要定期匯得自己加 | 有 DB 連線資訊時加進 `configs/apm/schedule.yaml`（stage 2） |

- **決定**：case 只做分類（投票＋信心），根因、處置一律不從 case 給；權重手設、≥ 50 件才學且只建議不自動寫；DB 讀取只做 SQL → 列，欄位對應靠別名。
- **下一輪**：匯真實事故單，`similar-eval` 看準確率；FDC（沒有 trace id）要先做「lot 路徑合成器」再談 graph（討論紀錄：機台／chamber／recipe step 對到 service／segment，lot id 當 trace id）。
