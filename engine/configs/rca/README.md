# AIOps 根因分析：Metrics → Trace → Log

一句話：**baseline 發現某個服務異常之後，自動照「Metrics 發現異常 → Trace 縮小到哪一段 → Log 找出原因」查一遍，產出一份給人確認的報告。**

這是 [docs/rca/README.md](../../../docs/rca/README.md) 六個步驟裡的 2～4 步（合併、定位、找原因）。不需要 AI 模型：都是查詢加統計。

## 三種資料各做什麼

| | 比喻 | 在這裡做什麼 |
|---|---|---|
| **Metrics** | 體溫計：有沒有異常 | [engine/configs/baseline](../baseline/README.md) 寫進 `aiops-anomalies` 的異常：哪個服務、哪支 API、什麼時候 |
| **Trace** | X 光：請求走了哪些服務、每段多久 | 1. **慢在哪一段**：出事時最慢的請求跟平常比，每一段多花多少時間<br>2. **錯誤從哪裡開始**：最底下出錯的那段，不是一路往上傳的<br>3. **瀑布圖**：最慢那個請求每一段的時間<br>4. **呼叫關係圖**：從 trace 自動畫出誰呼叫誰 |
| **Log** | 病歷上的字：到底發生什麼事 | 1. 用 `trace_id` 把最慢那個請求在**各服務**的 log 撈出來<br>2. 可疑服務的 warn／error 歸成樣式，跟平常比：**新出現的**、變多的排前面 |

trace 是中間那座橋：沒有它，只知道「mes-api 變慢」，要在幾十個服務的 log 裡大海撈針；有了它，直接知道是 wip-worker 查 Oracle 那段，只要看 wip-worker 那幾分鐘的 log。

## 怎麼判斷「慢在哪一段」

每一段只算**自己**花的時間：wip-worker 處理 1.5 秒、其中 1.5 秒在等 Oracle，那 wip-worker 自己是 0 秒，等 Oracle 那段是 1.5 秒。這樣慢的那段不會一路算到最上層，排第一的就是真正變慢的地方。

```
出事時：最慢的 200 個請求＋最多 100 個出錯的
平常  ：一週前同一段時間的請求（沒有資料就用出事前、同樣長的一段）
   ↓ 每個請求在每一段自己花了多久，兩邊平均
   ↓ 多最多的排第一
```

錯誤也一樣：一個請求裡好幾段都標了錯誤時，只算最底下那段（上面的只是把錯誤往上傳）。

## 怎麼跑

```bash
cd engine                                # baseline、根因分析是同一個 Python 專案
pip install -r requirements.txt
export ES_URL=https://<es>:9200
export ES_API_KEY=<baseline 的 key>      # elasticsearch-setup.sh 發的「AIOps baseline key」：能讀 log、trace、aiops-*

# 從 baseline 找到的異常開始：最近 30 分鐘裡最嚴重的那個服務
python3 -m investigator investigate --config configs/rca/rca.yaml

# 指定服務、API、時間
python3 -m investigator investigate --config configs/rca/rca.yaml --service mes-api --route "GET /lots/{id}" \
    --start 2026-10-10T02:00:00Z --end 2026-10-10T02:15:00Z --out report.md

# 服務之間的呼叫關係（最近 1 小時、最近 2,000 個請求）
python3 -m investigator topology --config configs/rca/rca.yaml --window 1h

# 事件整合：最近的異常依服務合成一件、新的那件跑一次根因分析＋相似事故＋LLM 摘要，寫 aiops-alerts（Grafana 讀它發通知）
# 要定期跑，建議每 1～2 分鐘（rules、detect 之後）；排程見 deploy/
python3 -m investigator alert --config configs/rca/rca.yaml

# 相似事故檢索的評測：留一法 recall@3（每件已確認的事故輪流當新的，同一個根因分類有沒有排進前 3）
python3 -m investigator similar-eval --config configs/rca/rca.yaml

# 老師傅的回饋入口（通知裡的連結點進來填；寫 aiops-feedback、aiops-incidents）
FEEDBACK_PORT=8080 python3 -m feedback_web
```

### 事件整合（`alert`）

- **合成一件看服務**：同一個服務、30 分鐘內接著來的異常是同一件（`alerts.close_after`）；同一個 dept 的兩個服務是兩件。30 分鐘沒有新的 → `resolved`。
- **哪些會開一件**：critical 規則、baseline 連續才算的 `anomaly`、資料品質；warning 規則和跟兄弟比要同一個服務 2 筆以上；只跳一次的 baseline `warning` 只併進已經開著的那件。
- **送給誰看 dept**：每一件帶 `dept`、`group`（取它的異常的，異常寫入時已補上）；資料品質一律 `group: unassigned`（送平台）。Grafana 依 `dept` 分送，設定範例在 [configs/grafana](../grafana/README.md)。
- 新的那件跑一次 `investigate`：結論寫進 `summary`（Grafana 通知帶這個）、全文存 `report`；結構化結果存 `findings`（慢在哪一段、錯從哪開始、新 log 樣式），也拿來算這件的**指紋**，寫一筆 `aiops-incidents`（`status: auto`）。
- **過去相似的事故**（`alerts.similar`，[investigator/similar.py](../../investigator/similar.py)）：用指紋（服務、API、慢的那段、錯的源頭、異常的指標組合、新 log 樣式）去 `aiops-incidents` 找老師傅**確認過的**（`confirmed`、`corrected`）最像的 3 件，附在報告最前面：當時的根因、處置、怎麼看出來的、誰處理的，和**為什麼像**（哪幾點相同）。分數不到 4（至少一個強特徵：同一段變慢、同一個錯誤源頭；或同服務＋同指標）就不附。用 ES 的欄位比對、自己算分，不用向量，看得出為什麼像。通知摘要多一行「過去相似：…」。
- **LLM 摘要**（`alerts.llm`，[core/llm.py](../../core/llm.py)）：報告最前面三到五句（原因、影響、建議先做什麼），每句結尾附依據的 id（`[A1]` 異常、`[S1]` 慢的那段、`[E1]` 錯誤源頭、`[L1]` log 樣式、`[I1]` 過去事故）；引用了不存在的 id、或有一句沒依據，**整段不顯示**（程式檢查）。模型掛了退回固定句型（`template`），報告照出。provider 用環境變數 `LLM_PROVIDER`（`template`／`anthropic`／`openai`）或 `alerts.llm_provider` 蓋過；`template` 不叫模型，沒有模型也能跑。`openai` 實測過（`LLM_API_KEY`、`LLM_CHAT_MODEL`，可選 `OPENAI_BASE_URL`）：一件 5～8 秒；`anthropic` 只寫了沒跑過。
- **回饋連結**：`alerts.feedback_url`、`report_url`（含 `{alert_id}`）寫進每一件，Grafana 通知帶著；指到 [feedback_web](../../feedback_web/__init__.py)。
- 重跑不會重複：已經在某一件裡的異常、最近結束的件裡的異常都不會再開一次。停了一陣子才跑時，已經過去的件只記錄（`resolved`），不算新開、不會通知。

報告是 Markdown，裡面的關係圖是 mermaid，貼到 GitHub、GitLab 或 Kibana 的 Markdown 都看得到圖。

## 報告長什麼樣子

`e2e/local-k3s/run-rca-e2e.sh` 實測：rca-mes-api → rca-wip-worker → Oracle，製造「查 Oracle 從 30ms 變 1.5 秒、connection pool timeout、約 1/4 請求失敗」：

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

（節錄。`run-rca-e2e.sh` 產出的完整報告見 [example-report.md](example-report.md)，含第 1 節的異常、第 2 節的請求數與 p50／p95、第 6 節的關係圖。）

## 前提：資料要對得起來

| 要什麼 | 誰做 | 沒有的話 |
|---|---|---|
| 服務有 trace（Java agent 自動，見 [tracing.md](../../../sdk/standards/tracing.md)） | chart 自動 | 第 2～4、6 節是空的，只剩 log |
| log 帶 `trace_id`（照 [logback-spring.xml](../../../sdk/standards/java/logback-spring.xml)） | chart＋應用 | 撈不到「這個請求在各服務的 log」，但第 5 節依服務、時間找樣式還是有 |
| 往外呼叫帶 `peer.service`、`db.system` 或 `server.address` | agent 自動 | 關係圖少了資料庫、外部系統那一格 |
| log 有 level（`log.level`） | 中央 pipeline 解析 | 第 5 節找不到 warn／error |

## 限制

- **取樣**：trace 取樣調低的服務，第 2 節的請求數是取樣後的（真正的量看 metrics）。比較「每一段多花多久」不受影響，因為是平均。
- **只看最慢的請求**：每次比較 200～300 個請求，夠找出「哪一段變了」，不是統計所有請求。
- **只比一週前**：一週前也出事，或一週前沒資料又剛好出事前就開始變壞，比較結果會失真。報告第 2 節寫了用哪一段當「平常」。
- **log 樣式是簡單的規則**：把數字、IP、ID 換成記號。訊息格式差很多的（例如帶整段 SQL），同一種錯誤可能被分成好幾種。之後換成 log 樣式字典（[docs/rca/README.md](../../../docs/rca/README.md) 的「集合」baseline）。
- **一次一個服務**：好幾個服務同時異常時，挑偏離最大的那個。合併成同一件事（步驟 2）還沒做。

## 驗證

```bash
python3 -m unittest discover -s tests -t .                  # 在 aiops 底下；單元測試，不需要 ES
ES_URL=http://<ip>:9200 e2e/local-k3s/run-rca-e2e.sh        # k3s 端到端，約 14 分鐘
```

`loop_e2e`（2026-10-25，docker 的 ES 9.5.5、模擬資料，見 [architecture.md「產品主線 8 步」](../../../docs/architecture.md#產品主線-8-步)）：第 1 次事故 → 回饋網頁確認 → 第 2 次同樣症狀的報告附上第 1 件（為什麼像、當時的根因、處置、怎麼看出來的）、LLM 摘要（template）每句附依據；`similar-eval` 3/3；誤報 3 次 → 建議 → 回測 → 核准 → 不再報。

`run-rca-e2e.sh`：兩個模擬服務走真正的路線（Collector → ES），正常 7 分鐘、事故 5 分鐘，再模擬 baseline 記到一筆延遲異常，**不指定服務**跑 investigate，檢查 11 項：從異常找到服務與 API、瓶頸、錯誤源頭、新的 log 樣式、log 依 trace_id 串到兩個服務、關係圖。
