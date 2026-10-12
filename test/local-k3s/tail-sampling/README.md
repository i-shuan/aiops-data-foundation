# 尾端取樣（tail sampling）實測

一句話：**先把 trace 全部收進來，等整個 trace 到齊後再決定留不留：錯的、慢的、吞掉例外的全留，正常的隨機留 5%。** 這個資料夾是在本機 k3s 上驗證這件事的設定、壓測程式、量測腳本。為什麼要做見 [docs/aiops-trace.md「取樣」](../../../docs/aiops-trace.md#橫跨所有場景的前提取樣)。

> 這裡是**實測用**，還沒改正式的設定（`platform/`）。結果出來、決定要用之後才搬過去。

## 架構

```
應用（全部送，不在應用裡取樣）
   ↓ OTLP
node Collector（每個節點一台，跟現在一樣補 K8s、部門欄位）
   ↓ loadbalancing exporter：依 trace_id 決定送哪一台 → 同一個 trace 的每一段一定到同一台
trace-sampler × 2（StatefulSet）
   ↓ tail_sampling：第一段到了之後等 decision_wait（5 或 10 秒），看整個 trace 決定
   ↓ 依 group 分流
ES（traces-generic.otel-<group>）
```

為什麼要兩層：決定要看「整個 trace」，但同一個 trace 的段來自不同服務、不同節點，會進不同的 node Collector。所以 node Collector 不決定，只依 trace_id 把同一個 trace 送到同一台 trace-sampler。

## 留不留的規則（依序，第一個符合的就決定）

| 順序 | 名稱 | 條件 | 為什麼 |
|---|---|---|---|
| 1 | `random` | 隨機 5%（依 trace_id） | **給 trace baseline 用**：不管快慢對錯都抽，統計才不會被「錯的、慢的全留」拉偏。放第一個，抽到的就記成 random |
| 2 | `error` | 任何一段 status = ERROR | 錯誤源頭追查 |
| 3 | `http-5xx` | 任何一段 HTTP 狀態碼 5xx | status 沒設成 ERROR 的也抓 |
| 4 | `exception` | 任何一段帶例外事件 | 吞掉例外、回 200 的（假性健康） |
| 5 | `slow` | 整個 trace 超過 1 秒 | 慢請求定位 |
| — | 其他 | 不留 | |

每一段會記下是哪一條留下的：ES 的 `scope.attributes.tailsampling.policy`（Collector 的 `recordpolicy` 功能開關）。

## 各檔案

| 檔案 | 是什麼 |
|---|---|
| 兩層 Collector 的設定 | 實測後搬進正式設定：node Collector 的 loadbalancing 出口在 [node.yaml](../../../platform/cluster-addons/otel-collector/node.yaml)，trace-sampler 的設定與 StatefulSet 在 [sampler.yaml](../../../platform/cluster-addons/otel-collector/sampler.yaml)、[trace-sampler.yaml](../../../platform/cluster-addons/otel-collector/trace-sampler.yaml)；本機測試用的覆寫（等 5 秒、隨機 100%）在 [test/local-k3s/otel-collector](../otel-collector/kustomization.yaml) |
| [loadgen.py](loadgen.py) | **壓測產生器**（只用 Python 內建套件）：模擬 lt-gateway → lt-order → DB 三段呼叫，每個服務像真的 SDK 一樣「每 5 秒或滿 512 段送一次」，所以同一個 trace 的段會分批、在不同時間到。請求的種類見下表，每一段都帶 `lt.category`、`lt.run` 用來對答案 |
| [measure.py](measure.py) | **量一輪的結果**：從 ES 數每種請求送了幾個、留了幾個、有沒有缺段、是哪條規則留的；trace-sampler 的記憶體、CPU（kubelet 的數字） |
| [run-tail-sampling.sh](run-tail-sampling.sh) | **整個實測**：裝上兩層 → 每一輪換 decision_wait、流量 → 跑 loadgen（Job）→ 等決定完 → 讀 trace-sampler 的內部計數 → measure.py。跑完換回原本的 Collector（`KEEP=1` 不換回） |

## 壓測的請求種類（loadgen.py）

| 種類 | 比例 | 長什麼樣 | 應該 |
|---|---|---|---|
| `normal` | 89% | DB 5～40ms，全部 200 | 留約 5% |
| `error` | 4% | DB 錯，往上兩段 500 | 全留 |
| `slow` | 3% | DB 1.5～3 秒 | 全留 |
| `very_slow` | 1% | DB 12 秒（每一段都 12 秒後才結束，一起到） | 全留 |
| `slow_after` | 1% | 下游、DB 很快，最上面的服務之後自己又做了 12 秒：底下三段先到，最上面那段 12 秒後才到，**比 decision_wait 晚** | 看得出會不會漏：決定的時候還看不到最上面那段 |
| `swallowed` | 2% | DB 那段帶例外事件，但 status 沒設、全部 200 | 全留 |

## 怎麼跑

```bash
ES_URL=http://<es>:9200 test/local-k3s/tail-sampling/run-tail-sampling.sh
# 預設三輪：decision_wait 5 秒／每秒 200 個請求、10 秒／200、10 秒／500，每輪送 5 分鐘、約 7 分鐘
ROUNDS="dw5:5s:200" DURATION=120 test/local-k3s/tail-sampling/run-tail-sampling.sh   # 只跑一輪、短一點
```

## 結果

2026-10-10，k3s 單節點（4 核），trace-sampler 2 台，每輪送 5 分鐘。每個請求 4 段，所以每秒 500 個請求＝每秒 2,000 段。

| 種類 | 應該 | 等 5 秒／每秒 200 | 等 10 秒／200 | 等 10 秒／500 | **等 30 秒／500** |
|---|---|---|---|---|---|
| normal | 留約 5% | 5.0% | 5.0% | 5.0% | 4.9% |
| error | 全留 | 100% | 100% | 100% | 100% |
| slow（1.5～3 秒） | 全留 | 100% | 100% | 100% | 100% |
| very_slow（每段都 12 秒） | 全留 | 100% | 100% | 100% | 100% |
| **slow_after**（最上面那段 12 秒後才到） | 全留 | **4.3%** ❌ | **4.9%** ❌ | **5.3%** ❌ | **100%** ✅ |
| swallowed（吞掉例外、回 200） | 全留 | 100% | 100% | 100% | 100% |
| 留下的 trace 有缺段 | 0 | 0 | 0 | 0 | 0 |
| 來不及決定就被擠掉 | 0 | 0 | 0 | 0 | 0 |
| trace-sampler 記憶體（每台最高） | | 89MB | 90MB | 114MB | 157MB |
| trace-sampler CPU（每台平均） | | 0.013 核 | 0.013 核 | 0.023 核 | 0.023 核 |
| node Collector CPU（平均） | | 0.067 核 | 0.068 核 | 0.094 核 | 0.090 核 |

**看出來的：**

1. **該留的都留了、而且完整。** 錯誤、慢、吞掉的例外 100% 留下，每個都 4 段齊全：同一個 trace 的段分好幾批、前後差到 5 秒才到，也沒有缺。
2. **量只剩約 15%**（每秒 500 個請求，留下 23,139 個 trace／15 萬）：正常的 5% ＋ 有問題的全部。
3. **隨機那 5% 不偏**：錯的、慢的也有 5% 是被 random 留下的，所以 trace baseline 只用 random 那份，統計跟「全部都記」一樣，不會被「錯的、慢的全留」拉偏。
4. **唯一會漏的：請求比 decision_wait 長，而且底下的段先到。** 例如呼叫下游很快就回來、服務自己之後又做了 12 秒：決定的時候只看到快的那幾段，判斷「不慢」不留；最上面那段 12 秒後才到，照之前的決定丟掉。**decision_wait 要比最長的請求長**：等 30 秒（Collector 的預設）就全部留下，記憶體每台多約 40MB。
5. **資源很省**：每秒 2,000 段，兩台 trace-sampler 各 150MB 左右、0.02 核；node Collector 多出約 0.02～0.03 核（多了依 trace_id 分配）。

**還沒驗證的：**
- 更大的量：這台測試機的 ES 和產生器先到上限。記憶體大致跟「每秒幾個 trace × decision_wait」成正比，`num_traces` 要設成這個數字的 2 倍以上（不然還沒決定就被擠掉）。
- trace-sampler 重開、擴縮時：依 trace_id 的分配會變，正在等的 trace 會被拆到兩台，那段時間的決定可能不完整。
- 比 30 秒還長的請求（批次工作）：一樣會漏，這種用 metrics、log 看。
