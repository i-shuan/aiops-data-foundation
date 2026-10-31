# RCA ＋ LLM 搭配 graph RAG（構想，還沒做）

> **類型：原理 Explanation**

> ⚪ **構想，尚未實作。** 這份只記錄想法，沒有程式、沒有驗證；已實作的相似事故檢索與 LLM 摘要見 [architecture.md 第 7 節](../architecture.md#7-各模組程式驗證狀態)。

**狀態**：只記錄，沒有程式、沒有驗證。下面標「推論」的是還沒測的判斷。　**日期**：2026-10-27
**模組**：⑦ 根因分析、⑨ feedback

## 現在怎麼做（對得到程式）

| 環節 | 現況 | 在哪 |
|---|---|---|
| 根因分析 | 每一件事故各自算：metrics 異常 → 同服務、同 API、同時間窗的 trace → 用 trace_id 撈 log；輸出一份 Markdown | [engine/investigator/analyze.py](../../engine/investigator/analyze.py)、[es.py](../../engine/investigator/es.py) |
| 呼叫關係 | 從 trace 算出「誰呼叫誰」（calls、errors），只用在**這一件**報告的 mermaid 圖和 `investigator topology` 的表；沒有存下來、沒有跨事故 | `A.topology`、`investigator topology` |
| 相似事故 | 欄位比對（服務、API、慢的那段、錯誤源頭、log 樣式；有權重、有最低分），只找老師傅確認過的；刻意不用向量 | [engine/investigator/similar.py](../../engine/investigator/similar.py) |
| LLM | 只看「輸入包」：這件告警 [T1]、異常 [A1]、慢的那段 [S1]、錯誤源頭 [E1]、log 樣式 [L1]、過去事故 [I1]；每句要附 id，引用不存在的 id 整段不顯示（程式檢查） | [engine/core/llm.py](../../engine/core/llm.py) |

## 構想

把「服務、API、下游依賴（Oracle、簡訊廠商…）、Pod／節點、版本、事故、根因、處置」存成一張圖（節點加邊）。事故來時從異常的服務出發，取附近幾跳的子圖——上下游、共用的依賴、同一次換版、以前一起出過事的——連同相似事故的**根因路徑**一起放進 LLM 的輸入包。圖上每個節點、每條邊也有 id，LLM 要引用，程式照樣檢查。

想補的是現在做不到的三種問題（**推論**，沒有案例證明現行做法答不出來）：

1. 上游都慢，源頭是**共用的依賴**：好幾個服務同時指到同一個 Oracle。現在每件各算各的，不會知道另一件也是同一個源頭。
2. 「過去這個依賴出事時，影響過哪些服務、怎麼處置的」：相似事故只比對單件的指紋，不沿著依賴往外看。
3. 換版或換節點後的連鎖：版本、節點資訊在 Pod 上，但 RCA 沒把它跟呼叫關係接起來。

## 要的資料（大部分已經有）

呼叫關係：trace。事故、根因分類、處置：`aiops-incidents`。namespace、Pod、節點、版本：每筆資料都帶。log 樣式：報告第 5 節。**還沒有**的是把這些存成可以跨事故查的邊表。

## 開放問題

| 問題 | 備註 |
|---|---|
| 圖放哪 | ES 不是圖資料庫。先每次從 ES 現算（像 `topology` 現在這樣），或存成邊表索引；不急著引進圖資料庫 |
| 值不值得 | `similar.py` 自己寫「20～30 件欄位比對就夠」。事故件數少時，圖多出來的可能很有限 |
| 怎麼知道有沒有變好 | 要評測集比較「加了圖，LLM 的根因有沒有比現在準」。目前沒有，是 [architecture.md「還沒做、還不足的」](../architecture.md#10-還沒做還不足的) 列的缺口 |
| 引用檢查 | 現在檢查 [T1][A1]… 這些 id；要延伸到圖的節點、邊，不然圖會變成 LLM 可以亂編的地方 |
| 跟定案的關係 | 「AIOps 以統計＋LLM 為主，不自己訓練模型」（[feedback/llm-mlops.md](../feedback/llm-mlops.md)）：圖是檢索，不是訓練，不衝突 |

## 動手前先做的最小驗證

在 [e2e/local-k3s/rca-demo/](../../e2e/local-k3s/rca-demo/) 多加第二個服務、也呼叫同一個 Oracle，兩邊同時出事。看現行報告能不能指出「兩件的源頭是同一個依賴」。能，就不用做圖；不能，才有理由做。
