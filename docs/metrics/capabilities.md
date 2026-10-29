# metrics 能做到什麼

> **類型：原理 Explanation**　同主題：[原理](README.md)・[規格（欄位）](reference.md)・[操作（部署、驗證）](how-to.md)・[指標目錄（規格）](catalog.md)・[案例與實測](cases.md)・[.NET 原理](dotnet.md)・[.NET 操作](dotnet-how-to.md)・[.NET 規格](dotnet-reference.md)

metrics 回答「有沒有出事、什麼時候開始、離極限多遠」；這份說明能做到什麼、做不到什麼、建議的順序。收了哪些指標、資料結構見 [catalog.md](catalog.md)。

## 三、metrics 能做到什麼

不是每一種問題都要「學平常」。依問題的形狀分五種做法：

| 做法 | 適合什麼 | 例子 | 要學嗎 | 現在 |
|---|---|---|---|---|
| **1. 跟平常比**（baseline） | 有週期、沒有固定標準的數字 | 請求數、延遲、錯誤率、CPU | 要 | ✅ 7 條 |
| **2. 離極限多遠**（飽和度） | 有上限的東西：滿了就出事 | 記憶體 ÷ 上限 > 90%、磁碟剩 < 10%、連線池使用中 ÷ 上限 > 90%、節點 disk/memory pressure | 不用：規則 | ✅ 容器記憶體、節點記憶體、節點磁碟、CPU 限流、連線池、JVM 老年代、GC、.NET 執行緒池（[rules.yaml](../../engine/configs/baseline/rules.yaml)） |
| **3. 慢慢爬**（趨勢） | 每天都在範圍內，但一直往上 | 連線池洩漏、heap 越來越高、磁碟越來越滿：算斜率，預測**幾天後到上限** | 不用學週期，算斜率 | ❌ |
| **4. Pod 同儕橫向比對 (Peer Comparison)** | 同一個服務多個 Pod，一個壞掉被平均稀釋 | 只有 pod-7 錯誤率 100%、只有一個 Pod 記憶體一直漲 | 不用：當下比 | ✅ 錯誤率、p95、CPU、記憶體（[peer.yaml](../../engine/configs/baseline/peer.yaml)，見 [engine/configs/baseline「Pod 同儕橫向比對」](../../engine/configs/baseline/README.md#pod-同儕橫向比對同一個服務的-pod-互相比)） |
| **5. 狀態變化**（事件） | 發生了就是問題 | 重啟次數增加、可用副本 < 要的副本、Pod Failed、節點 NotReady、`up = 0`、Job 失敗 | 不用：規則 | ✅ 全部（k3s 實測：5 種壞法都抓到、正常的不誤報） |

另外兩種用法：

- **看尾巴**：p95 看不到千分之一的請求慢 5 秒（DNS 案例）。直方圖可以算 **p99** 和「超過 N 秒的比例」，再用做法 1 學。
- **對時間**（根因分析用）：異常那一刻，同一個服務的 GC 暫停、連線池等待、CPU 有沒有一起跳。GC 卡 6 秒、連線池滿這種，延遲和 runtime 指標同時變，就能指出原因。

### metrics 只看得到現象嗎？

**不只。** metrics 分兩種：

| | 看什麼 | 例子 | 回答 |
|---|---|---|---|
| **現象指標** | 服務對外的表現（請求數、錯誤率、延遲） | API 變慢、5xx 變多 | 「出事了、從幾點開始」 |
| **原因指標** | 服務靠的資源和狀態（用量、離上限多遠、事件） | 連線池滿了、GC 卡 6 秒、磁碟 0%、OOM 重啟、換版 | 「哪一種原因、在哪個 Pod」 |

現象指標一跳，同一時間看原因指標有沒有一起跳，就能指到原因的**種類**：

| 現象 | 同時跳的原因指標 | 指到的原因 |
|---|---|---|
| 延遲變高 | 連線池使用中 = 上限、等待連線變多 | 連線池耗盡（再往下是洩漏還是量太大） |
| 每 15 分鐘卡 6 秒 | `jvm.gc.duration` 同一時間 6 秒 | Full GC |
| 全部請求逾時 | 節點磁碟剩 0% | 磁碟滿（log 寫不出去卡住） |
| 錯誤變多 | 重啟次數增加、記憶體到上限 | OOM |
| 錯誤變多 | `service.version` 剛換 | 換版造成 |
| 只有 10% 錯 | 依 Pod 分開，只有 pod-7 | 單一 Pod 的問題 |

**metrics 到不了的，是更細的「為什麼」：**

```
出事了         ← metrics（現象）
哪個服務、哪個 Pod   ← metrics（依維度切開、Pod 同儕橫向比對）
哪一種原因      ← metrics（原因指標對時間）：資源滿、GC、OOM、換版
哪一段、呼叫幾次   ← trace：N+1、重試、慢在哪個下游
哪一句、哪一筆    ← trace 屬性、log：哪句 SQL、哪個 lot.id、錯誤訊息
哪一行程式      ← log 的堆疊、人看程式碼
```

例如連線池洩漏：metrics 看得到「連線池滿了」，還能用趨勢預測「4 天後會滿」。但「哪個例外路徑忘了 close」要靠 trace 和 log，最後是人看程式。

**前提：原因指標要收得到。** 現在收了資源（CPU、記憶體、磁碟）、K8s 狀態（重啟、副本、節點），以及 Java、.NET 的 GC、執行緒池、連線池（[metrics/cases.md](cases.md)、[metrics/dotnet.md](dotnet.md)）。

### 對應 15 個案例（[rca/cases.md](../rca/cases.md)）

| 案例 | 用 metrics 怎麼抓 | 做法 |
|---|---|---|
| 3 連線池洩漏 | 使用中 ÷ 上限一路往上 | 2＋3 |
| 5 Full GC | `jvm.gc.duration` 跟延遲 p99 同時跳；heap 鋸齒往上 | 1（p99）＋3＋對時間 |
| 11 DNS 偶發 5 秒 | p99、超過 5 秒的比例 | 1（尾巴） |
| 12 磁碟滿 | 節點或 Pod 磁碟剩餘 | 2＋3 |
| 14 訊息積壓後 OOM | 記憶體 ÷ 上限、重啟次數 | 2＋5 |
| 15 只有一個 Pod 壞 | 依 Pod 比錯誤率 | 4 |
| 8 執行緒池 | 執行緒數（JVM 有）；佇列長度要應用自己開 | 2 |

### metrics 做不到的

| 做不到 | 為什麼 | 要靠 |
|---|---|---|
| 慢在請求裡的哪一段、呼叫了幾次 | metrics 只有整體的數字 | trace（[trace 每一段的 baseline](../../engine/configs/baseline/README.md#trace-每一段的-baseline)） |
| 錯誤被吞掉、回 200 | 狀態碼是 200，metrics 全綠 | log 樣式、業務量指標 |
| 哪一批貨、哪一台機台 | 不能用 `lot.id` 這種值當維度（每個值一條序列，ES 會爆） | trace 的屬性 |
| 從某個慢的資料點直接跳到那一筆 trace | exemplar（帶 trace_id）寫進 ES 時被丟掉（實測 ES 9.5.5：`/_otlp/v1/metrics`、Collector `elasticsearch` 寫入元件都一樣），metrics 帶不了 trace_id | 用服務、時間範圍查 trace |
| 30 秒內的短暫尖峰 | 30 秒才抓一次；直方圖記得住每個請求的耗時，但 CPU、記憶體這種只看得到抓的那一刻 | 直方圖、trace |

## 四、建議順序（待討論）

1. **狀態變化與飽和度（做法 5、2）**：✅ 已做，見 [engine/configs/baseline「規則」](../../engine/configs/baseline/README.md#規則狀態變化離極限多遠)。
2. **Pod 同儕橫向比對（做法 4）**：✅ 已做，見 [engine/configs/baseline「Pod 同儕橫向比對」](../../engine/configs/baseline/README.md#pod-同儕橫向比對同一個服務的-pod-互相比)。
3. **補 baseline**：p99、超過 N 秒的比例、往外呼叫的耗時（`http.client.request.duration`）、重啟次數。
4. **確認 Java agent 的 JVM、連線池指標名稱**，再加飽和度：✅ 已做（[metrics/cases.md](cases.md)）；趨勢見下一項。
5. **趨勢（做法 3）**：要有幾天以上的資料才算得出斜率。
