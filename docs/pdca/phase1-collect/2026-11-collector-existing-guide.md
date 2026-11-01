# collector：公司已經有 Collector 時，AIOps 要加哪些設定（2026-11）

**狀態**：已完成（只有文件）　　**日期**：2026-11-01　　**相關**：分支 `claude/collector-aiops-additions`；[collector/README.md](../../../collector/README.md)
**模組**：① 收值、② 清資料

## Plan：這一次要達到什麼

- **需求**：公司的 Collector 可能已經有人在管。使用者問「Collector 哪些是 AIOps 設定」，要一份可以拿去跟管 Collector 的人對照的最小清單。
- **目的**：在 `collector/README.md` 加一節，把 `collector/code/` 的設定分成「AIOps 必加」「歸屬與安全」「選配」，每項指到 `code/` 裡的區塊名稱與用途、AIOps 哪裡會用到；不複製整段 yaml（避免跟 `code/` 兩份不同步）。
- **範圍**：做：README 一節、這份 PDCA。不做：改 `code/` 的任何設定；對公司實際的 Collector 實測。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `collector/README.md` | 新增「公司已經有 Collector：AIOps 要加哪些」：三個表（必加、歸屬與安全、選配）、pipeline 與順序、前提（image 版本、Pod label、ES 端結構） | 內容全部從 `common.yaml`、`node.yaml`、`cluster.yaml` 讀出來整理，區塊名稱跟 `code/` 一致 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 清單裡的區塊名稱、pipeline 順序跟 `code/` 一致 | 逐項對 `node.yaml`（receivers、processors、pipelines）、`common.yaml`（processors、exporters）原文 | 一致；文件檢查過 |

**沒測到的**：拿這份清單去改一個真的、不同版本的 Collector（processor 名稱、`extract_sum_metric` 等在較舊版本可能沒有，我們用的是 `otel/opentelemetry-collector-contrib:0.162.0`）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 沒有「公司現有 Collector 長什麼樣」的資訊 | 使用者沒給 | 清單是從我們自己的設定倒推的，公司那邊可能已經有同名 processor 或不同的 pipeline 結構 | 拿到公司的 Collector 設定再逐項對 |
