# AIOps 根因分析：Metrics → Trace → Log

一句話：**baseline 發現某個服務異常之後，自動照「Metrics 發現異常 → Trace 縮小到哪一段 → Log 找出原因」查一遍，產出一份給人確認的報告。**

這是 [docs/rca/README.md](../../../docs/rca/README.md) 六個步驟裡的 2～4 步（合併、定位、找原因）。不需要 AI 模型：都是查詢加統計。

原理（三種資料各回答什麼、怎麼判斷慢在哪一段、為什麼用一批慢 trace 找共同瓶頸）見 [docs/rca/README.md](../../../docs/rca/README.md)。這份只寫怎麼跑、預設值、報告長什麼樣子、限制、怎麼驗。

## 預設值

| 項目 | 預設 | 在哪改 |
|---|---|---|
| 出事時取幾個請求 | 最慢的 200 個，加最多 100 個出錯的 | `rca.yaml` 的 `sample` |
| 「平常」拿哪一段比 | 一週前同一段時間；沒有資料就用出事前、同樣長的一段 | `rca.yaml` 的 `compare_with`、`ref_gap` |
| 每一段算什麼 | 只算自己花的時間（扣掉底下的呼叫）；錯誤只算最底下那段 | 固定 |
| 出事前的趨勢看多久 | 前 6 段、每段 10 分鐘的 p95：一路往上且最後 ≥ 1.5 倍＝慢慢惡化；前面平（±30%）、出事時 ≥ 1.5 倍＝突然 | `rca.yaml` 的 `trend` |
| 慢請求的共同點看什麼 | span 的 resource：Pod、版本、節點；attributes：`lot.id`、`db.statement`。慢的請求裡 ≥ 60% 同一個值、而且是平常比例的 ≥ 2 倍才列 | `rca.yaml` 的 `commonality` |

## 怎麼跑

```bash
cd engine                                # baseline、根因分析是同一個 Python 專案
pip install -r requirements.txt
export ES_URL=https://<es>:9200
export ES_API_KEY=<baseline 的 key>      # kibana-dev-tools.txt 第 3 段發的 engine key：能讀 log、trace、aiops-*

# 從 baseline 找到的異常開始：最近 30 分鐘裡最嚴重的那個服務
python3 -m main investigator investigate --config configs/rca/rca.yaml

# 指定服務、API、時間
python3 -m main investigator investigate --config configs/rca/rca.yaml --service mes-api --route "GET /lots/{id}" \
    --start 2026-10-10T02:00:00Z --end 2026-10-10T02:15:00Z --out report.md

# 服務之間的呼叫關係（最近 1 小時、最近 2,000 個請求）
python3 -m main investigator topology --config configs/rca/rca.yaml --window 1h

# 事件整合：最近的異常依服務合成一件、新的那件跑一次根因分析＋相似事故＋LLM 摘要，寫 aiops_alerts（Grafana 讀它發通知）
# 要定期跑，建議每 1～2 分鐘（rules、detect 之後）；排程見 deploy/
python3 -m main investigator alert --config configs/rca/rca.yaml

# 相似事故檢索的評測：留一法 recall@3（每件已確認的事故輪流當新的，同一個根因分類有沒有排進前 3）
python3 -m main investigator similar-eval --config configs/rca/rca.yaml

# 歷史事故單匯入 aiops_incidents（CSV 或 JSON；--dry-run 只檢查哪幾筆會略過、不寫）；格式見下面「匯入歷史事故單」
python3 -m main investigator import-incidents configs/rca/incidents-example.csv --dry-run
python3 -m main investigator import-incidents <你的事故單.csv>

# 老師傅的回饋入口（通知裡的連結點進來填；寫 aiops_feedback、aiops_incidents）
API_PORT=8080 python3 -m main api
```

### 事件整合（`alert`）

- **合成一件看服務**：同一個服務、30 分鐘內接著來的異常是同一件（`alerts.close_after`）；同一個 dept 的兩個服務是兩件。30 分鐘沒有新的 → `resolved`。
- **跨服務只併一層**（`alerts.chain`，預設開）：下游出事連帶上游異常時只開一件、留下游那件，根因分析以下游跑。呼叫關係從這段時間（`lookback`）的 trace 算：有件開著、或這次有異常的服務，每個取最近 `chain_sample` 個請求，父 span 的服務呼叫子 span 的服務（跟 `topology` 同一套）。併件條件與欄位：

  | 情況 | 做法 | 欄位 |
  |---|---|---|
  | 上游 A 的異常、A 沒有開著的件、A 直接呼叫的 B 有開著的件、離 B 那件開始不到 `close_after` | 併進 B 那件 | B 的 `upstream_services` 加 A |
  | 之後 A 的異常 | 照同服務的規則併（離最後一筆不到 `close_after`） | — |
  | B 開新的一件時 A 已經有一件、兩件開始時間差不到 `close_after` | A 那件整件併進 B：這一輪才開的直接消失；上一輪已寫進 ES 的標 `resolved` | A 的 `merged_into` = B 的 `alert_id`；A 的事故 `status: merged` |
  | 沒有呼叫關係、時間不重疊、A→B→C 的 A（不是直接呼叫 C）、A 那件已經帶 `upstream_services` | 不併，照舊各一件 | — |

  標題、摘要加「；連帶上游 A」；`dept`、標題的「最嚴重那筆」只看下游自己的異常；`anomaly_ids`、`series`、`routes` 照舊併進去，上游的異常也寫回下游的 `alert_id`。查不到 trace 就不併跨服務，不擋這一輪。
- **同源標記（不合併）**：每件從根因結果取「卡在哪個外部依賴」（慢的那幾段、錯誤源頭那幾段 `→` 後面的對象，例如 `oracle`，存 `dependencies`）；開著的、不同服務的件卡在同一個依賴，互相記 `related_alerts`、`shared_dependency`，摘要多一行「可能同源：a-… 也卡在 oracle」。對象是另一件開著的件的服務時不算（那是上下游，上面的跨服務處理）。不合併成一件：是不同服務各自的件（部門、處置不同），只提示很可能同一個源頭。
- **哪些會開一件**：critical 規則、baseline 連續才算的 `anomaly`、資料品質；warning 規則和Pod 同儕橫向比對要同一個服務 2 筆以上；只跳一次的 baseline `warning` 只併進已經開著的那件。
- **一件的嚴重度**（[incident/alert_severity.py](../../src/incident/alert_severity.py)）：併完之後算，代表要不要吵人。`critical`：靜態規則 critical，或 ≥ 3 類黃金訊號（延遲、錯誤、往外呼叫、飽和）同時異常且不是單點；`high`：2 類，或 1 類但持續（≥ 10 分鐘、最後 15 分鐘窗 ≥ 80% 的分鐘有異常）或好幾條序列都跳；`warning`：單點、單一序列。廣度算「幾類」不是「幾條」（3 支 API 的錯誤率都跳仍是 1 類），流量不算，一類至少 2 筆。換版（`service.version` 變了）後 10 分鐘內的異常上限 `warning`，過了還有就照常評。參數在 `alerts.severity`（`window`、`min_coverage`、`min_duration`、`min_span_critical`、`min_points_per_class`、`deploy_grace`）。Grafana 只通知 critical、high（`policies.yaml` 把 warning 靜音）。**P0 服務**（`alerts.p0_services`：名字或 regex）上面算完再升一級（warning → high、high → critical；換版後的上限也一樣升），`severity_why` 會寫「P0 服務，升一級」；只改要不要叫人，**算不算異常的門檻不動**——P0 要更早報，給那條指標寫靜態臨界值規則 (Static Threshold Rules)，或設 `confirm_points: 1`（單點即判定 (Instant Anomaly)）。
- **送給誰看 dept**：每一件帶 `dept`、`group`（取它的異常的，異常寫入時已補上）；資料品質一律 `group: unassigned`（送平台）。Grafana 依 `dept` 分送，設定範例在 [configs/grafana](../../../grafana/README.md)。
- 新的那件跑一次 `investigate`：結論寫進 `summary`（Grafana 通知帶這個）、全文存 `report`；結構化結果存 `findings`（慢在哪一段、錯從哪開始、新 log 樣式），也拿來算這件的**指紋**，寫一筆 `aiops_incidents`（`status: auto`）。
- **告警分類**（`alerts.similar`，[llm/similar.py](../../src/llm/similar.py)）：用指紋（服務、API、慢的那段、錯的源頭、異常的指標組合、新 log 樣式）去 `aiops_incidents` 找老師傅**確認過的**（`confirmed`、`corrected`）最像的 3 件，以分數投票說這件**像哪一類**（db、network、config_change…）＋信心，寫進 `suggested_category`、`category_confidence`；報告最前面一段只列分類和那幾件的日期、分類、標題、**為什麼像**。**不列當時的根因、處置**：程式換版後指紋還對得上、根因可能已經不是那個，根因請看定位（慢在哪一段）。分數不到 4（至少一個強特徵：同一段變慢、同一個錯誤源頭；或同服務＋同指標）就不附、不分類。用 ES 的欄位比對、自己算分，不用向量，看得出為什麼像。通知摘要多一行「分類：像 db 類…」。權重可用 `alerts.similar_weights` 蓋過（`similar-eval --learn-weights` 在 case ≥ 50 件時會建議）。
- **向量輔助**（[llm/embedding.py](../../src/llm/embedding.py)；要設環境變數 `EMBEDDING_URL`、`EMBEDDING_MODEL`，helm 的 `embedding.*`）：欄位比對照舊是主要的，另外把確認過的事故的文字（標題、根因、處置、怎麼看出來的、慢的段、錯的源頭、log 樣式）與這一件的文字轉成向量，向量最近的 10 件也當候選；相似度 ≥ 0.8 的加「5 × 相似度」分，why 寫「描述相近（向量相似度 0.86）」——只有向量像、相似度 0.8 剛好 4 分，夠 `MIN_SCORE`。參數在 `alerts.similar_vector`（`weight`、`min_similarity`、`k`）。事故的向量存在 PostgreSQL 的 `aiops_incident_vectors`，事故內容改了（文字雜湊不一樣）才重算；`pgvector` 有裝用 SQL 算最近的，沒裝在程式裡算。embedding API 或資料庫出錯：印一行、只用欄位比對，不擋這一件。留一法的評測（`similar-eval`）只算欄位比對。
- **歷史 case 的線索與先查清單**（[llm/similar.py](../../src/llm/similar.py) 的 `segment_hints`、`checklist`）：分類之後再列兩段。①**線索**：這次根因分析**已經看到變化**的段（變慢、錯變多），過去確認過的事故也是這段的，件數多的排前面、列出是哪幾件；過去常出事但這次沒變化的段不列——排序只代表先看哪個，是不是它看這次的數字（存進 `findings.case_hints`）。②**先查什麼**：分類那一類，已確認的事故裡老師傅勾過的（`checked`）次數多的排前面，不夠 5 項用情境資料檔的 `domain.checklists` 補。
- **LLM 摘要**（`alerts.llm`，[llm/summary.py](../../src/llm/summary.py)）：報告最前面三到五句（原因、影響、建議先做什麼），每句結尾附依據的 id（`[A1]` 異常、`[S1]` 慢的那段、`[E1]` 錯誤源頭、`[L1]` log 樣式、`[I1]` 過去事故）；引用了不存在的 id、或有一句沒依據，**整段不顯示**（程式檢查）。模型掛了退回固定句型（`template`），報告照出。provider 用環境變數 `LLM_PROVIDER`（`template`／`anthropic`／`openai`）或 `alerts.llm_provider` 蓋過；`template` 不叫模型，沒有模型也能跑。`openai` 實測過（`LLM_API_KEY`、`LLM_CHAT_MODEL`，可選 `OPENAI_BASE_URL`）：一件 5～8 秒；`anthropic` 只寫了沒跑過。
- **回饋連結**：`alerts.feedback_url`、`report_url`（含 `{alert_id}`）寫進每一件，Grafana 通知帶著；指到 [api](../../src/interface/web/__init__.py)。設了 `feedback_url` 時 `report` 全文最後一行也是回饋網址（Grafana 表格點眼睛看全文，看完就是回饋入口）；回饋頁加 `&full=1` 會把全文放在表單上面。
- **回饋的一句話抽取**（`core.llm.extract_feedback`，同一個 `LLM_PROVIDER`）：值班在表單寫一句自然語言（「這是每月初的批次匯出報表，屬於預期內負載」），抽成 `feedback_type`（false_positive／true_positive／wrong_root_cause）、`identified_cause`（短代號，如 `scheduled_batch_job`）、`target_service`（預設告警的服務，文字點名別的才覆蓋）、`suggested_action`（`suppress_during_cron`／`widen_threshold`／`add_rule`／`none`）。模型回的要過格式檢查，不過就只存原文；對得到調整種類的寫一筆 `aiops_tuning`（`status: proposed`、`source: feedback`、`reason` 帶原文），**人核准才生效**。解析率用 `python3 -m tests.feedback_extract_eval` 量（26 句語料 `tests/feedback_corpus.yaml`；[pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md](../../../docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md)）。
- 重跑不會重複：已經在某一件裡的異常、最近結束的件裡的異常都不會再開一次。停了一陣子才跑時，已經過去的件只記錄（`resolved`），不算新開、不會通知。

#### 過去相似怎麼算分（例）

指紋六組的權重見 [aiops-data-reference.md](../../../docs/aiops-data-reference.md#aiops_incidents事故庫相似事故檢索的資料池)：慢的那段 4、錯誤源頭 4、服務 3、API 2、新 log 樣式 2、指標 1（每個相同的指標加 1，最多算 3 個）。有交集就加一次權重，總分 ≥ 4 才算像，取最高的 3 件。

新的一件：`mes-api` 的 `GET /lots/{id}` 變慢又出錯，慢的那段和錯誤源頭都是 `wip-worker › SELECT LOT_HISTORY → oracle`，新出現 `Connection pool timeout…` 的 log，指標是 `http.latency_p95_s`、`http.error_rate`。

| 舊事故（老師傅確認過的） | 相同的欄位 | 分數 | 結果 |
|---|---|---|---|
| A：同一段變慢、同一段出錯、同樣兩個服務、同一支 API、同樣的 log 樣式、`http.latency_p95_s` | 4 + 4 + 3 + 2 + 2 + 1 | 16 | 排第一，報告寫出六點為什麼像 |
| B：只有服務 `wip-worker` 相同，兩個指標都相同 | 3 + 1 + 1 | 5 | 排第二（過門檻） |
| C：只有 API 和 `http.error_rate` 相同 | 2 + 1 | 3 | 不附（低於 4） |

分數用 `similar-eval` 的同一個 `explain` 算（`llm/similar.py`）；權重與門檻是估的，沒有用真實事故調過。

#### LLM 怎麼設定

| 設定 | 放哪 | 預設 |
|---|---|---|
| provider：`template`／`anthropic`／`openai` | 環境變數 `LLM_PROVIDER`；Helm 是 `values.yaml` 的 `llmProvider`（轉進 ConfigMap `aiops-env`）；`rca.yaml` 的 `alerts.llm_provider` 可蓋過（只能指定 provider） | `template`：不叫模型 |
| API key | 環境變數 `LLM_API_KEY`，放 Secret `aiops-secrets` | — |
| 模型 | 環境變數 `LLM_CHAT_MODEL` | `openai`：`gpt-4o-mini`；`anthropic`：`claude-sonnet-4-5` |
| 服務網址 | `OPENAI_BASE_URL`（相容 `/v1/chat/completions`）、`ANTHROPIC_BASE_URL` | `https://api.openai.com`、`https://api.anthropic.com` |
| 開關 | `rca.yaml` 的 `alerts.llm: true` | 開 |

Helm chart 只把 `ES_URL`、`LLM_PROVIDER`、`TZ` 放進 ConfigMap，Secret 整個以 `envFrom` 掛進去。所以 `LLM_CHAT_MODEL`、`OPENAI_BASE_URL`、`ANTHROPIC_BASE_URL` 沒有對應的 chart 參數，要加成 Secret `aiops-secrets` 的額外 key。

例：用 OpenAI 相容的內部服務。

```bash
kubectl -n aiops create secret generic aiops-secrets \
  --from-literal=ES_API_KEY=<…> --from-literal=LLM_API_KEY=<…> \
  --from-literal=LLM_CHAT_MODEL=<模型名> --from-literal=OPENAI_BASE_URL=https://<內部服務>
helm upgrade --install aiops aiops-engine/helmchart -n aiops --set llmProvider=openai …
```

換 key 或模型要重啟 pod（環境變數是 pod 起來時讀的）。
### 匯入歷史事故單（`import-incidents`）

告警分類只拿老師傅確認過的事故投票；系統剛上線時知識庫是空的，先把過去的事故單匯進來。範例 [incidents-example.csv](incidents-example.csv)（10 筆製造業 IT 常見的事故，照格式填）。

```bash
python3 -m main investigator import-incidents incidents.csv --dry-run    # 先檢查：印出第幾筆缺什麼，不寫
python3 -m main investigator import-incidents incidents.csv              # 寫進 aiops_incidents；印出匯入幾筆、略過幾筆
python3 -m main investigator similar-eval --config configs/rca/rca.yaml  # 匯完量一次：分類準確率、recall@3
```

事故單在資料庫時直接讀（SQL 的欄位別名要跟 CSV 的欄位名一樣；sqlite 不用裝東西，其他要 `pip install sqlalchemy` 和驅動）：

```bash
python3 -m main investigator import-incidents --db 'postgresql://aiops:${INCIDENTS_DB_PW}@db.example.internal/itsm' --sql "SELECT subject AS title, opened_at AS start, closed_at AS end, services, category AS root_cause_category, root_cause, action, how_detected, checked FROM incidents WHERE closed_at > now() - interval '2 years'"
```

要定期匯（每天讓分類器多幾票）：`rca.yaml` 加 `incidents_db: {url: …, sql: …}`，排程表加一個 job 跑 `investigator import-incidents --config configs/apm/rca.yaml`；密碼用 `${環境變數}` 寫在 URL 裡，由 Secret 帶進 pod。

一列（CSV）或一個物件（JSON 陣列，或 `{"incidents": [...]}`）是一件。CSV 用 UTF-8（Excel 另存的 UTF-8 BOM 也行）。

| 欄位 | 必填 | 內容 |
|---|---|---|
| `title` | ✓ | 一句話：哪個系統、什麼現象 |
| `start`、`end` | ✓ | 開始、結束時間。`2026-03-12 02:10`、`2026-03-12T02:10:00+08:00` 都可以；**沒寫時區當台北**（`--tz` 或 `--config` 的 `timezone` 可改） |
| `services` | ✓ | 牽涉的服務，用 metrics／trace 裡的服務名（多個用逗號分隔；JSON 給陣列） |
| `root_cause_category` | ✓ | 根因分類，跟回饋表單同一套：`db`、`network`、`config_change`、`deploy`、`external`、`resource`、`bug`、`data`、`other` |
| `root_cause`、`action` | ✓ | 根因、處置 |
| `how_detected` | ✓ | 怎麼看出來的（老師傅的經驗，報告會照抄給值班看） |
| `checked` | ✓ | 查了哪些地方（多個用逗號分隔），例如 `Oracle AWR / 慢查詢,trace 瀑布圖` |
| `by`、`note` | | 處理的人、備註 |
| `routes`、`series`、`slow_segments`、`error_origins`、`patterns` | | **指紋欄位**，有就填：入口 API（`GET /lots/{id}`）、異常的指標（`http.latency_p95_s`）、慢的那段（`wip-worker › Oracle SELECT LOT_HISTORY`）、錯的源頭、新 log 樣式。寫法跟報告裡的一樣；數字會自動換成 `#` |

- **多值欄位**的分隔：逗號；值本身有逗號（SQL）就整欄改用 `|` 分隔。
- **缺必填欄位、時間看不懂、`end` 早於 `start` 的那一筆略過**，印出第幾筆、缺什麼；其他照匯。
- 寫進去的是 `status: confirmed`、`source: import`，相似事故檢索（`alert`）馬上拿得到。`alert_id` 是 `imp-<事故日期>-<檔案裡第幾筆>`。
- **重跑不重複**：`_id` 由 `title`＋`start`＋`services` 算出，同一件再匯就覆蓋（改了 `action` 重匯也是覆蓋同一筆）。改了 `title` 或 `start` 會變成另一筆，舊的要自己刪。
- 比對的分數門檻是 4（見上面「過去相似的事故」）：**只填 `services` 的事故單**自己只有 3 分，要靠同指標（`series`）或同 API（`routes`）才會被附上；填了慢的那段或錯的源頭（各 4 分）最有用。

報告是 Markdown，裡面的關係圖是 mermaid，貼到 GitHub、GitLab 或 Kibana 的 Markdown 都看得到圖。

## 報告長什麼樣子

`e2e/k8s/run-rca-e2e.sh`（e2e/ 已隨自建收值刪除，在 git 歷史）實測：rca-mes-api → rca-wip-worker → Oracle，製造「查 Oracle 從 30ms 變 1.5 秒、connection pool timeout、約 1/4 請求失敗」：

````
## 3. Trace：慢在哪一段
| 段 | 現在 | 平常 | 多了 |
|---|---|---|---|
| rca-wip-worker › SELECT LOT_HISTORY → oracle | 1.50 s | 34 ms | **+1.47 s** |
| rca-wip-worker › GET /wip | 17 ms | 15 ms | **+2.2 ms** |
| rca-mes-api › GET /lots/{id} | 3.0 ms | 3.0 ms | **+0.0 ms** |

## 4. 最慢的那個請求
rca-mes-api › GET /lots/{id}                       |████████████████████████████████████████| 1552.0 ms（自己 3.0）
  rca-mes-api › GET → rca-wip-worker               |████████████████████████████████████████| 1549.0 ms（自己 19.0）
    rca-wip-worker › GET /wip                      | ███████████████████████████████████████| 1531.0 ms（自己 31.0）
      rca-wip-worker › SELECT LOT_HISTORY → oracle | ███████████████████████████████████████| 1500.0 ms（自己 1500.0）

## 5. Log：那幾段發生了什麼事
| 服務 | 樣式 | 現在 | 平常 |
|---|---|---|---|
| rca-wip-worker | 🆕 `Connection pool timeout waiting for connection to <ip> after <n>ms, lot L<n>` | 131 | 0 |
| rca-mes-api | 🆕 `lot L<n> failed: wip-worker returned error` | 33 | 0 |

最慢那個請求的 log（依 trace_id 從各服務撈出來）：
02:14:10  rca-wip-worker error  Connection pool timeout waiting for connection to 10.0.1.5:5432 after 1500ms, lot L100115
02:14:10  rca-mes-api    info   lot L100115 handled in 1550ms

## 結論（給人確認）
- rca-mes-api（GET /lots/{id}）變慢，主要多在 rca-wip-worker › SELECT LOT_HISTORY → oracle：每個請求多花 1.47 s（平常 34 ms）。
- 錯誤最早出在 rca-wip-worker › SELECT LOT_HISTORY → oracle（33 個請求，平常 0）。
- 同時間 rca-wip-worker 新出現 log「Connection pool timeout waiting for connection to 10.0.1.5:5432 …」，131 次。
````

（節錄。舊的 `run-rca-e2e.sh`（已刪，在 git 歷史）產出的完整報告見 [example-report.md](example-report.md)，含第 1 節的異常、第 2 節的請求數與 p50／p95、第 6 節的關係圖。）

## 前提：資料要對得起來

| 要什麼 | 誰做 | 沒有的話 |
|---|---|---|
| 服務有 trace（Elastic APM agent 自動；transaction sample rate 設 1.0） | agent | 第 2～4、6 節是空的，只剩 log |
| log 帶 `trace.id`（APM 的 log shipping 或 ecs-logging） | 應用 | 撈不到「這個請求在各服務的 log」，但第 5 節依服務、時間找樣式還是有 |
| 往外呼叫帶 `peer.service`、`db.system` 或 `server.address` | agent 自動 | 關係圖少了資料庫、外部系統那一格 |
| trace context 有傳到下游（下游的 Server span 的 parent 是上游的 Client span）、trace 的 `service.name` 跟異常的服務名一樣 | agent 自動（跨 HTTP／Kafka 要帶 header） | 算不出「誰呼叫誰」，上下游同時出事各開一件 |
| log 有 level（`log.level`） | 中央 pipeline 解析 | 第 5 節找不到 warn／error |

## 限制

- **請求數**：第 2 節的請求數來自 merged（100% 收，但只有已合成的、1～2 分鐘前的）；真正的量看 metrics。
- **只看最慢的請求**：每次比較 200～300 個請求，夠找出「哪一段變了」，不是統計所有請求。
- **只比一週前**：一週前也出事，或一週前沒資料又剛好出事前就開始變壞，比較結果會失真。報告第 2 節寫了用哪一段當「平常」。
- **log 樣式是簡單的規則**：把數字、IP、ID 換成記號。訊息格式差很多的（例如帶整段 SQL），同一種錯誤可能被分成好幾種。之後換成 log 樣式字典（[docs/rca/README.md](../../../docs/rca/README.md) 的「集合」baseline）。
- **一次一個服務**：`investigate` 不指定服務時，好幾個服務同時異常挑偏離最大的那個。事件整合只沿呼叫關係併一層（直接呼叫者）：A→B→C 三個一起出事是兩件（C 連帶 B；A 自己一件）。

## 驗證

```bash
python3 -m unittest discover -s tests -t .                  # 在 aiops 底下；單元測試，不需要 ES
```

`loop_e2e`（2026-10-25，docker 的 ES 9.5.5、模擬資料，見 [architecture.md「產品主線 8 步」](../../../docs/architecture.md#9-產品主線-8-步)）：第 1 次事故 → 回饋網頁確認 → 第 2 次同樣症狀的報告附上第 1 件（為什麼像、當時的根因、處置、怎麼看出來的）、LLM 摘要（template）每句附依據；`similar-eval` 3/3；誤報 3 次 → 建議 → 回測 → 核准 → 不再報。

舊的 `run-rca-e2e.sh`（已刪，在 git 歷史；現在的對應是 `tests/loop_e2e.py`、`tests/chain_e2e.py`）：兩個模擬服務走自建 Collector → ES，正常 7 分鐘、事故 5 分鐘，再模擬 baseline 記到一筆延遲異常，**不指定服務**跑 investigate，檢查 11 項：從異常找到服務與 API、瓶頸、錯誤源頭、新的 log 樣式、log 依 trace_id 串到兩個服務、關係圖。
