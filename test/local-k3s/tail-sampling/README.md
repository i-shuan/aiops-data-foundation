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
| [kustomization.yaml](kustomization.yaml) | 把整套裝上去：沿用本機測試的 Collector（`../otel-collector`），加上 trace-sampler、node Collector 多讀一份 `node-tail.yaml`。`kubectl apply -k` 這個資料夾就是兩層；回到原本：`kubectl apply -k ../otel-collector` |
| [node-tail.yaml](node-tail.yaml) | **第一層的改動**：疊在 node Collector 原本的設定後面，trace 的出口從「直接寫 ES」換成 loadbalancing exporter（依 trace_id 送 trace-sampler，用 headless Service 的 DNS 找到每一台） |
| [sampler-config.yaml](sampler-config.yaml) | **第二層 trace-sampler 的 Collector 設定**：收 OTLP → tail_sampling（上面的規則）→ 依 group 送 ES。decision_wait、隨機比例、慢的門檻都用環境變數 |
| [sampler.yaml](sampler.yaml) | trace-sampler 的 K8s 物件：StatefulSet（2 台）＋ headless Service；環境變數 `DECISION_WAIT`、`NUM_TRACES`（記憶體裡最多放幾個還沒決定的 trace）、`RANDOM_PCT`、`SLOW_MS` |
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

（實測中，結果出來後補上）
