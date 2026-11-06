# 歷史事故單匯入＋相似事故 Top-3 召回率（2026-10）

**狀態**：已完成（指令、範例、測試；召回率是 **20 筆合成事故**的數字，真實事故單匯入後要重量）　　**日期**：2026-10-29　　**相關**：分支 `claude/poc-acceptance-plan`，[POC 驗收計畫](../meta/2026-10-poc-acceptance.md)的任務 4；前置 [相似事故檢索](2026-10-similar-incidents.md)（Act 寫了匯入欄位、沒寫指令）
**模組**：⑦ 根因分析、⑨ feedback（知識庫）

## Plan：這一次要達到什麼

- **需求**：相似事故檢索只拿老師傅確認過的事故教人，知識庫現在只有模擬的 2 筆；POC 的 KPI「相似事故 Top-3 召回率 ≥ 70%」沒資料算不出來。使用者手上有 15～30 筆歷史事故單，但沒有指令可以匯。
- **目的**：一支指令把 CSV／JSON 的事故單匯進 `aiops-incidents`（`status: confirmed`），匯完 `similar-eval` 就算得出 recall@3；給一份範例檔讓使用者照格式填；先用 20 筆合成事故把量法跑通、記一個數字。
- **範圍**：
  - 做：`investigator import-incidents`、範例檔、單元測試、20 筆合成事故的 recall@3、留一法的定義寫清楚。
  - 不做：改比對的門檻與權重（看到合成資料的結果也不調：那是估的，要等真實資料）；向量檢索；從工單系統直接抓。

### `similar-eval` 怎麼算（留一法 recall@3）

[similar.py](../../../aiops-engine/code/aiops/service/rca/similar.py) 的 `evaluate`：

1. 從 `aiops-incidents` 取 `status` 是 `confirmed`／`corrected`、**有 `root_cause_category`、有 `fingerprint`** 的全部 N 件。
2. 每一件輪流當「新的」：用它的指紋查其他 N−1 件（`must_not ids` 排除自己），`search` 留分數 ≥ 4 的前 3 件。
3. 前 3 件裡**任何一件的根因分類跟它一樣**就算命中；recall@3 = 命中數 ÷ N。
4. 分數不是 ES 的 `_score`，是 `explain` 自己算：兩邊指紋每個欄位有交集就加權重——同一段變慢 4、錯誤源頭相同 4、同服務 3、同一支 API 2、同樣的新 log 樣式 2、同樣的指標異常 1（最多 3）。

這個定義的兩個後果，決定合成資料怎麼造：

| 後果 | 所以 |
|---|---|
| 某個分類只有 1 件時，那件不可能命中（沒有同類可查），但分母照算 | 合成資料每個根因分類 ≥ 2 筆 |
| 只有 `services` 的事故單自己只有 3 分，不到門檻 4；要同指標（+1）或同 API（+2）才會被附上；慢的那段、錯的源頭各 4 分最有鑑別力 | 匯入格式收這幾個指紋欄位（選填）；合成資料照真實工單的樣子，**有的填、有的不填** |

### 匯入格式（寫在 [configs/rca/README.md「匯入歷史事故單」](../../../aiops-engine/code/configs/rca/README.md#匯入歷史事故單import-incidents)）

| 決定 | 內容 | 為什麼 |
|---|---|---|
| 必填 | `title`、`start`、`end`、`services`、`root_cause_category`、`root_cause`、`action`、`how_detected`、`checked`；選填 `by`、`note` | 跟 `aiops-incidents` 和回饋表單同一套欄位；`how_detected`、`checked` 是老師傅的經驗，報告照抄給值班看，少了就只剩根因 |
| 指紋欄位（選填） | `routes`、`series`、`slow_segments`、`error_origins`、`patterns` | 前置 PDCA 寫的「有更好」；沒有就只用服務比對 |
| 多值分隔 | 逗號；值裡有逗號（SQL）整欄改 `\|` | 使用者在 Excel 填，逗號最直覺；SQL 段名有逗號才換 |
| 時區 | 沒寫時區當台北（`detector.adjust.local_iso`，`--tz` 可改） | 跟行事曆、`incident add` 一樣；不轉的話 ES 當 UTC，差 8 小時 |
| `_id` | `title`＋`start`＋`services` 的指紋 | 同一件重匯覆蓋、不重複；改了 `action` 重匯也是覆蓋同一筆（用全部內容算的話，改一個字就多一筆） |
| `alert_id` | `imp-<事故當地日期>-<檔案裡第幾筆>` | 給人看、對得回檔案的第幾列 |
| 寫入 | `index`（覆蓋），不是 `alert` 用的「只更新 auto」upsert | 匯入檔是這幾件的真相；老師傅後來在網頁上改的欄位會被重匯蓋掉（見 Act） |
| 略過 | 缺必填、時間看不懂、`end` 早於 `start` 的那一筆略過，印第幾筆、為什麼；其他照匯 | 一筆壞不擋整份 |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `aiops-engine/code/investigator/importer.py`（新） | `read_rows`（CSV 用 `utf-8-sig`、JSON 陣列或 `{"incidents": […]}`）、`split_list`、`normalize`（一列 → `(_id, 文件)` 或略過原因）、`fingerprint`、`parse`、`write`、`run` | 解析、驗證、寫入分開：單元測試不用 ES；`--dry-run` 讓使用者沒有 ES 權限也能先檢查檔案 |
| 同上 | 指紋的慢段、錯誤源頭過 `similar._seg_name`（數字換 `#`） | 跟 `alert` 自動產生的指紋同一個正規化，不然 `SELECT lots 123` 對不上 `SELECT lots #` |
| `aiops-engine/code/investigator/__main__.py` | 新增 `import-incidents <file> [--config] [--tz] [--dry-run]`；`--config` 對這個子指令改成選填 | 匯入不需要 rca.yaml；給了就拿它的 `timezone` |
| `aiops-engine/code/detector/feedback.py` | `INCIDENT_MAPPING` 加 `source`（keyword）、`note`（text） | mapping 是 `dynamic: false`，不加就查不到「哪些是匯入的」；`ensure_index` 會把新欄位補進既有 index |
| `aiops-engine/code/configs/rca/incidents-example.csv`（新） | 10 筆製造業 IT 常見事故（統計資訊過期、連線池耗盡、憑證過期、Kafka 落後、磁碟滿、簡訊 API 逾時、換版 N+1、DNS、月底鎖表、空指標），欄位齊、含指紋欄位 | 使用者照格式填；也是單元測試的輸入 |
| `aiops-engine/code/tests/incidents-synthetic.csv`（新） | 20 筆＝範例 10 筆＋10 筆；分類 db 5、external 3、config_change／data／resource／deploy／network／bug 各 2 | 召回率評測用；每類 ≥ 2 才算得出來；放 tests 讓單元測試盯著它每筆都齊 |
| `aiops-engine/code/tests/test_import_incidents.py`（新） | 16 個測試（項目見 Check） | |
| `aiops-engine/code/configs/rca/README.md` | 「怎麼跑」加指令；新段落「匯入歷史事故單」：欄位表、分隔、時區、`_id`、重跑、門檻 4 的提醒 | 操作文件跟程式放一起 |
| `docs/README.md`、`docs/pdca/README.md` | 描述加「匯入歷史事故單」；索引加這份 | |

- 合成的 20 筆在跑評測**之前**定稿（見 `tests/incidents-synthetic.csv`），看到結果沒有改資料。

## Check：測試的目的與結果

單元測試（`tests/test_import_incidents.py`，假的 ES）：

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| CSV 解析：BOM、逗號多值、沒時區當台北、寫入的狀態 | Excel 樣式的 UTF-8 BOM CSV 一筆 | `services`、`checked` 變陣列；`2026-03-12 02:10` → `2026-03-12T02:10:00+08:00`；`status confirmed`、`source import`、`alert_id imp-20260312-01`、`confirmed_by` = `by`；沒給的選填欄位不寫 |
| 值裡有逗號時用 `\|` 分隔；段名去數字 | `slow_segments` 給 `… SELECT a, b FROM lots 123\|…` | 兩段、`123` → `#`；其他指紋欄位是空陣列（不是 None） |
| 欄位名有空白也認得 | 標題列 `title , start,…` | 匯得進去 |
| JSON：陣列、`{"incidents": […]}`、錯的形狀 | 三種檔 | 陣列欄位直接收；服務排序過（順序不同不算另一件）；`{"foo": …}` 直接 `SystemExit` 說明要的形狀 |
| 缺必填欄位的那筆略過、其他照匯、訊息說第幾筆缺什麼 | 3 筆：齊、缺 `action`＋`how_detected`（空白）、缺 `services` | 匯 1 筆；略過 `(2, "缺 action、how_detected")`、`(3, "缺 services")` |
| 時間看不懂、`end` 早於 `start` 略過 | `start: 昨天晚上`；`end` 比 `start` 早 1 小時 | 都略過，訊息帶原值 |
| 指紋去重 | 同一件改服務順序與處置；改 `title`；同一檔解析兩次 | 前者同 `_id`（`alert_id` 跟著第幾筆變，不影響 `_id`）；改 `title` 是另一筆；兩次 id 完全一樣 |
| 時區 | `start` 寫 `Z`；`--tz Asia/Tokyo`；只有日期 | 有寫時區照寫（`+00:00`），`alert_id` 的日期用台北（UTC 11 日 18 點 → `imp-20260312`）；東京 `+09:00`；只有日期 → `00:00:00+08:00` |
| 寫入走 `ensure_index`＋`bulk_index` | 假 ES 記呼叫 | index 是 `aiops-incidents`，mapping 有 `source`；10 筆；空的不呼叫 |
| **匯入的文件能被 `similar.search` 找到**（形狀跟自動產生的一樣） | 範例 10 筆放進 `test_similar` 的假 ES，用「wip-worker、`GET /lots/{id}`、慢在 `wip-worker › Oracle SELECT LOT_HISTORY`」的指紋查 | 第 1 名是 `db`，why 有「同一段變慢（wip-worker › Oracle SELECT LOT_HISTORY）」 |
| 範例檔、合成檔每筆都齊；合成檔每類 ≥ 2；合成檔前 10 筆就是範例檔 | 解析兩個檔 | 10／20 筆、0 略過、20 個不同 `_id`、每類 ≥ 2 |
| CLI `--dry-run` 不碰 ES、印數字 | `main([...])` 抓 stdout | `第 2 筆略過：缺 checked`、`1 筆可匯入、1 筆略過（共 2 筆` |
| 沒弄壞別的 | `cd engine && pytest -q`；`tools/check-docs.py` | 366 passed（含新的 16 個）；連結、索引、舊說法、類型 0 問題 |

ES 實測（自己的 ES 9.5.3 容器，`elasticsearch-setup.sh` 後匯 `tests/incidents-synthetic.csv`）：

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 匯入、重跑不重複 | 匯兩次，看 `_count` | 第 1 次「匯入 20 筆、略過 0 筆」→ `_count` 20；第 2 次一樣的訊息，`_count` **仍是 20**。抽一筆看：`status confirmed`、`source import`、`alert_id imp-20260312-01`、`start 2026-03-12T02:10:00+08:00`、`fingerprint` 6 欄（`error_origins`、`patterns` 是空陣列）。範例檔 `--dry-run`：10 筆可匯入、0 略過 |
| mapping 補上新欄位 | `_mapping` | `source` keyword、`note` text、`fingerprint` 底下 6 個子欄位都在 |
| **recall@3（留一法）** | `similar-eval --config configs/rca/rca.yaml` | **10/20（50%）**，沒到 70%。分類別：db 5/5、data 2/2、external 2/3、network 1/2、config_change 0/2、resource 0/2、deploy 0/2、bug 0/2 |

沒命中的 10 件為什麼沒命中（看每件的前 3 和 why，沒有改資料重跑）：

| 原因 | 件數 | 例子 |
|---|---|---|
| 同分類的另一件在**不同的服務**上，指紋（服務、API、慢的那段、錯的源頭）沒有任何交集 | 9 | 磁碟滿在 report-service、OOM 在 spc-service，都是 `resource`；換版 N+1 在 mes-api、換版慢查詢在 oee-dashboard，都是 `deploy`。其中 3 件連一件 ≥ 4 分的都沒有（`[]`） |
| 同分類的另一件找到了（5 分）但**同分的有 4 件**，`search` 同分時排最早的，被擠到第 4 | 1 | DNS（#8）跟交換器（#16）同服務＋同指標 5 分，跟連線池（#2）、空指標（#10）、連線池再發（#11）也都 5 分；#16 最晚，排第 4 |

- 反過來看：20 件裡**有「同分類且至少一個同服務」的前例**的是 11 件，其中 10 件命中（只漏 #8 的同分）。指紋量的是「同一個地方又出事」，評測量的是「同一種根因」；兩者在 db 這類（同一張表、同一段 SQL 反覆出事）一致，在 resource、deploy、bug 這類（根因一樣、地方不一樣）就對不上。
- **同一個地方、不同根因**會拿高分：憑證過期（#3，`config_change`）和 ERP 供應商維護（#14，`external`）同服務、同 API、同錯誤源頭 `mes-api › POST → erp-gateway`、同指標，互相是對方的第 1 名、10 分。報告會把對方的根因和處置附在最前面；它是「過去相似」不是「就是這個」，但值班容易先信。
- 這些都是合成資料上的觀察；真實事故單哪種情況多（同一個地方反覆出事、還是同一種根因到處出）要匯入後才知道。

- **沒測到的**：真實事故單（欄位齊不齊、分類填得一不一致、`services` 名字對不對得上 metrics／trace）；`alert` 開新的一件時附上匯入的事故（這段在前置 PDCA 用模擬事故驗過，匯入只是多一個來源，沒再跑 loop_e2e）；超過 1,000 筆（`evaluate` 一次取 1,000）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| **召回率是合成資料的數字** | 這一輪 | 只證明「匯入 → 評測」這條路通、量法對；不能當 KPI 達標或不達標的證據 | 真實事故單匯入後重跑 `similar-eval`，用那個數字填驗收計畫的 KPI |
| **合成 20 筆 recall@3 = 50%，沒到 70%**：9/10 的漏是同分類在不同服務，指紋沒東西可比 | 這一輪 `similar-eval` | 指紋只能找「同一個地方又出事」；KPI 用「同一種根因」當標準答案，對 resource／deploy／bug 這種到處出的根因，欄位比對先天找不到 | 真實資料先量；還是低就二選一：(1) 指紋加 `root_cause`／`how_detected` 的文字比對（前置 PDCA 已預留「不夠準再加」），(2) 驗收時把 KPI 的分母定義寫清楚（有同服務前例的才算） |
| [⑦] 同分時 `search` 排**最早**的（`sort key (-score, start)`） | #8 DNS 的同類被同分的 3 件擠到第 4 | 門檻附近的同分很常見（同服務 3＋同指標 1～2），誰進前 3 看日期 | 改成最近的優先（同一套基礎設施、同一版程式比較像）；這輪不改，怕變成為了數字調 |
| [⑦] 同一個地方、不同根因拿 10 分 | #3 憑證過期 ↔ #14 供應商維護 | 報告最前面附的「當時的根因」可能是錯的那個，LLM 摘要也會引用 [I1] | 「為什麼像」已經寫出是哪幾點相同（都是「哪裡」）；真實資料看誤導幾次再決定要不要把 `root_cause_category` 不同的降權 |
| 必填 9 個欄位 vs 驗收計畫寫的「匯入指令容許缺欄位」 | 定格式時 | 真實工單常沒有 `how_detected`、`checked`，會被略過 | 匯入時看略過幾筆；太多就把 `importer.REQUIRED` 縮到 `title`、`start`、`services`、`root_cause_category`、`root_cause`（一行），缺的欄位報告少一行 |
| 只填 `services` 的事故單自己不到門檻 4 | 看 `explain` 的權重 | 沒有指紋欄位的舊事故只有在同指標、同 API 時才會被附上 | 門檻、權重在前置 PDCA 就是估的，真實資料再調；匯入文件裡提醒填慢的那段、錯的源頭 |
| [⑨] 匯入的事故會被 `feedback suggest` 的「同服務同分類 ≥ 3 次 → 升級成規則」撿到 | 看 `cmd_feedback._confirmed_incidents` | 沒有 `anomaly_ids` 算不出門檻，會多出 `threshold: None`、不能核准的草稿；`metrics` 的「老師傅確認的事故」若時間窗涵蓋也會算進去 | 讓 `suggest_rules` 跳過 `source: import`，或接受這些草稿只是提醒 |
| 重匯會蓋掉老師傅後來在回饋網頁改的欄位 | 設計 `write` 用 `index` 時 | 匯入檔與 ES 兩邊都能改，以檔案為準 | 文件寫明；老師傅改了匯入的事故要同步回檔案 |
| 改了 `title` 或 `start` 重匯會變成另一筆 | 設計 `_id` 時 | 舊的那筆留著，要自己刪 | 沒有「刪匯入」指令；量少先用 Kibana 刪 |

- **決定**：匯入格式照 `aiops-incidents` 欄位加選填的指紋欄位；門檻、權重、合成資料都不為了數字調。
- **下一輪**：使用者給真實事故單 → 匯入 → `similar-eval` 重量，填進 [POC 驗收計畫](../meta/2026-10-poc-acceptance.md)的 KPI 表。
