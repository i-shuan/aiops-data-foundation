# 應用是 .NET 時：規格

> **類型：規格 Reference**　同主題：[原理](README.md)・[規格（欄位）](reference.md)・[操作（部署、驗證）](how-to.md)・[指標目錄（規格）](catalog.md)・[metrics 能做什麼（原理）](capabilities.md)・[案例與實測](cases.md)・[.NET 原理](dotnet.md)・[.NET 操作](dotnet-how-to.md)

自動埋點實際產生的指標名稱、baseline 與規則怎麼對。

## 實測的指標名稱（自動埋點 v1.12.0，ES 欄位是 `metrics.<名稱>`）

HTTP、連線池兩版同名；**runtime 的名稱 .NET 8 跟 .NET 9 完全不同**。

| 類 | .NET 8（以前） | .NET 9（以後） | 維度 |
|---|---|---|---|
| HTTP | `http.server.request.duration`（直方圖）、`http.server.active_requests` | 同左 | `http.request.method`、`http.route`、`http.response.status_code`（**數字**） |
| GC 次數 | `process.runtime.dotnet.gc.collections.count` | `dotnet.gc.collections` | .NET 8：`generation`；.NET 9：`gc.heap.generation`（gen0、gen1、gen2…） |
| GC 其他 | `process.runtime.dotnet.gc.heap.size`、`…gc.duration`、`…gc.allocations.size` | `dotnet.gc.last_collection.heap.size`、`dotnet.gc.pause.time` | |
| 執行緒池 | `process.runtime.dotnet.thread_pool.threads.count`、`…queue.length` | `dotnet.thread_pool.thread.count`、`…queue.length` | |
| 例外 | `process.runtime.dotnet.exceptions.count`（**沒分種類**） | `dotnet.exceptions`（依 `error.type`） | |
| CPU 核數 | `process.cpu.count` | `dotnet.process.cpu.count` | |
| 連線池（Npgsql） | `db.client.connections.usage`、`db.client.connections.max`、`db.client.commands.duration` | 同左 | `pool.name`、`state`（idle／used） |
| 往外 HTTP | `http.client.request.duration` | 同左 | `server.address`、`http.response.status_code` |

**沒有的：** heap 的上限（.NET 在容器裡 heap 上限預設是容器記憶體的 75%，沒有指標），連線池排隊數（OTLP 這版沒有 `pending_requests`）。

## baseline 與規則

- **baseline 的 `http.*` 序列不用分語言**：[series.yaml](../../engine/configs/baseline/series.yaml) 每條有兩種寫法（`variants`：Java 的欄位名稱、OTLP 的欄位名稱），結果用統一的 keys（`service`、`method`、`route`）。
- **.NET 的規則**（[rules.yaml](../../engine/configs/baseline/rules.yaml)）：.NET 8 以前用 `dotnet8.*`、.NET 9 以後用 `dotnet.*`，一個服務只會有其中一種名稱，不會重複報。

| 規則（.NET 8 ／ .NET 9） | 看什麼 | 對應 Java 的 |
|---|---|---|
| `dotnet8.gen2_gc_frequent` ／ `dotnet.gen2_gc_frequent` | 5 分鐘內 Gen2 GC ≥ 5 次 | `jvm.major_gc_frequent` |
| `dotnet8.exceptions_burst` ／ `dotnet.out_of_memory` | .NET 8：5 分鐘內例外 ≥ 500 次（分不出種類）；.NET 9：有 `OutOfMemoryException` | — |
| `dotnet8.thread_pool_growth` ／ `dotnet.thread_pool_growth` | 執行緒 ÷ CPU 核數 ≥ 16，2 分鐘 | Java 沒有標準指標 |
| `dotnet8.thread_pool_starvation` ／ `dotnet.thread_pool_starvation` | 執行緒池排隊 ≥ 10，2 分鐘（.NET 偵測不到的阻塞：lock、`Thread.Sleep`） | 同上 |
| `db.pool_exhausted_otlp` | 連線池使用中 ÷ 上限 ≥ 90% | `db.pool_exhausted` |

容器層的規則（重啟、記憶體快到上限、CPU 限流…）跟語言無關，照用。
