# log 收集：Fluent Bit vs OpenTelemetry Collector

> [!NOTE]
> **結論：已改用 OpenTelemetry Collector 收 log**，併進原本收 metrics 的同一個 Collector（見 [elasticsearch.md](elasticsearch.md)）。
> 這份是決定前的比較紀錄：兩個收集器同時跑、讀同一批 log 檔。當時 OTel 是另外開一個 Collector 專收 log，所以記憶體看起來多 200MB；
> 併進原本的 Collector 後，實際只多約 75MB，拿掉 Fluent Bit 又省回 25～50MB。

一句話：**欄位完全一樣、量大都撐得住；OTel 在「收集器重啟」比較好，Fluent Bit 在「key 失效」比較好。**

## 怎麼接

```
OTel Collector（DaemonSet，platform/cluster-addons/otel-logs）
   file_log 讀 /var/log/pods → 補 K8s 資訊與 namespace 的 dept → dept → group（跟 metrics 同一套規則）
   → 依 group 用各自的 key，OTLP 送到 ES 的 /_otlp/v1/logs → logs-generic.otel-<group>
        ↓ 中央 ES：logs-otel@custom 讓這些資料先跑 k8s-logs-otel
        ↓ k8s-logs-otel：整理成 Fluent Bit 的形狀、算 _id、轉到 k8s-<group>-<cluster>、呼叫 k8s-logs
   k8s-<group>-<cluster>（跟 Fluent Bit 同一個資料流、同一條 pipeline）
```

- **拆欄位的規則只有中央一份**，跟用哪個收集器無關。AIOps 讀的欄位完全一樣，用 `agent.type`（`fluent-bit`／`otelcol`）分辨是誰送的。
- 只處理我們 Collector 送的（帶 `group`）。其他團隊用 OTel 送進同一個 ES 的 log 照 ES 預設處理，不受影響。
- **權限：** 轉到 `k8s-<group>-*` 時 ES 會再檢查一次 key 的權限。實測：IT 的 key 送的資料被轉到 IMC 的資料流時被擋（403），所以假冒 `group` 寫不進別的部門。
- **去重複：** 跟 Fluent Bit 的 `generate_id` 一樣，用 Pod、容器、stream、container runtime 給的奈秒時間、內容算出 `_id`，重送時 ES 回 409。

| 檔案 | 用途 |
|---|---|
| [platform/cluster-addons/otel-collector/node.yaml](../platform/cluster-addons/otel-collector/node.yaml) | 收 log 的設定（比較時是另一個 DaemonSet，現在併進節點 Collector） |
| [platform/central/elasticsearch/pipeline-otel.*](../platform/central/elasticsearch/pipeline-otel.painless) | `k8s-logs-otel`：整理形狀、`_id`、轉資料流 |
| [platform/central/elasticsearch/logs-otel-custom.json](../platform/central/elasticsearch/logs-otel-custom.json) | 讓 `logs-*.otel-*` 先跑 `k8s-logs-otel` |
| [test/local-k3s/load/run-load.sh](../test/local-k3s/load/run-load.sh) | `volume`、`outage`、`crash`、`keyfail`、`restart`（比較時兩邊一起數，現在預設只數 OTel） |

比較用的腳本（兩個收集器並行、逐欄位比對）留在 branch `claude/otel-logs-compare`。

## 結果

單節點 k3s、ES 9.5.5、Fluent Bit 4.1.1、OpenTelemetry Collector contrib 0.162.0。兩個收集器同時讀同一批檔案；產生器每行帶序號，用序號算遺失和重複。

### 欄位

6 個測試應用（JSON、logfmt、宣告了還沒規則的格式、純文字……）收 90 秒：每個 Pod 兩邊筆數一樣；**同一行兩邊各一筆的 444 對，比 27 個欄位全部一樣**（訊息、level、logger、格式、服務、group、dept、K8s 資訊、pod label、image、trace、例外……）。

### 量大

| 每秒 | | 遺失 | 重複 | 延遲 p50／p99 | CPU（1 核 = 100%） | 記憶體 |
|---|---|---|---|---|---|---|
| 2,000 行 × 120 秒 | Fluent Bit | 0 | 0 | 0.5s／1.7s | 11% | **38MB** |
| | OTel | 0 | 0 | 0.6s／1.0s | 9% | 242MB |
| 5,000 行 × 120 秒 | Fluent Bit | 0 | 0 | 0.5s／1.6s | 20% | **48MB** |
| | OTel | 0 | 0 | 0.3s／0.6s | 19% | 250MB |

CPU 差不多；**OTel 記憶體約多 200MB**（每個節點）。

### 故障（每秒 200 行）

| 情境 | Fluent Bit | OTel |
|---|---|---|
| ES 正常關機 3 分鐘 | 0 遺失、0 重複，恢復後 61 秒補完 | 0 遺失、0 重複，恢復後 37 秒補完 |
| ES `kill -9` 兩次（第 2 次在補送中） | 0 遺失、0 重複 | 0 遺失、**0 重複**（加 `_id` 之前：198 筆重複） |
| **收集器重啟兩次**（ES、key 都正常） | **遺失 342 筆**（每次重啟掉約 0～2 秒） | **0 遺失、0 重複** |
| **IMC 的 key 作廢 3 分鐘** | 換 key 後補回來（遺失的 1,016 筆是換 key 時重啟 Fluent Bit 掉的，見上一列） | **那 3 分鐘全部遺失（36,060 筆）** |

## 兩個要注意的發現

### 1. Fluent Bit 每次重啟會掉一點 log

換版、改設定、節點排空時都會重啟。重啟兩次（每秒 200 行）各跑了幾輪：

| 讀檔方式 | 重啟兩次遺失 |
|---|---|
| `multiline.parser: cri, docker`（原本的設定） | 342 筆、5 筆（每次不一樣） |
| 同上，加長關機等待（`grace` 5 → 25 秒） | 8 筆 |
| `parser: cri`（不接 containerd 切開的長行） | 6 筆、8 筆 |

一開始懷疑是 `multiline.parser` 把最後幾行放在記憶體等，但換掉、加長關機等待都還是會掉幾筆，是 Fluent Bit 關機本身的行為，設定改不掉。OTel 重啟幾次都是 0。這是改用 OTel 的主要原因之一。

### 2. OTel 對「ES 拒收」的處理比 Fluent Bit 差

OTel 寫入元件只對連不上、逾時、429、502、503、504 重試；其他錯誤直接丟：

- **401（key 錯、被作廢）**：整批丟掉，補不回來（上面 key 作廢那列）。這跟 metrics 一樣，見 [metrics.md](metrics.md) 的「坑」。換 key 一定要先發新的、換上、確認後才作廢舊的。
- **某幾筆被拒（例如欄位型別衝突）**：只在 log 印 `Partial success ... dropped`，那幾筆丟掉。
- **409（重送的被擋）也算進「dropped」**：所以 OTel 的丟棄數不能直接當遺失告警，要看錯誤類型（`CONFLICT` 是正常的去重複）。

Fluent Bit 遇到 401 會一直重試，換 key 後補回來。

## 其他差異

| | Fluent Bit | OTel Collector |
|---|---|---|
| 記憶體（每節點） | 約 40～50MB | 約 250MB |
| 設定 | 一份 YAML，分流用 `rewrite_tag` | 跟 metrics 同一套寫法（`k8s_attributes`、`transform`、`routing`），dept → group 規則跟 metrics 共用 |
| 收集器重啟 | 會掉 0～2 秒 | 不掉 |
| key 失效 | 補得回來 | 遺失 |
| 去重複 | 收集器端算 `_id`（`generate_id`） | 中央 pipeline 算 `_id` |
| 跟 metrics 共用一套收集器 | 否，兩套 | 可以（同一種元件、同一套規則；這次測的是分開的 DaemonSet） |

## 決定

改用 OpenTelemetry Collector，併進原本收 metrics 的同一個 Collector：

- log、metrics、之後的 trace 是同一種收集器、同一套分部門規則（`common.yaml`），dept 清單少一處要同步。
- 重啟不會掉 log；去重複、ES 停機、ES 當機跟 Fluent Bit 一樣好。
- key 失效會丟資料這點，metrics 本來就得面對，「先發新 key、確認後才作廢舊的」本來就要照做；之後再做「平滑雙 key」。
- 中央 pipeline 仍接受 Fluent Bit 的資料（`agent.type` 分辨），邊緣 VM 可以用 Fluent Bit。
