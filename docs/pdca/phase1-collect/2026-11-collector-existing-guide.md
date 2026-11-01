# collector：設定拆成通用與 AIOps 專屬，並寫公司已經有 Collector 時要加哪些（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/collector-aiops-additions`；[collector/README.md](../../../collector/README.md)
**模組**：① 收值、② 清資料

## Plan：這一次要達到什麼

- **需求**：公司的 Collector 可能已經有人在管。使用者問「Collector 哪些是 AIOps 設定」，要一份可以拿去跟管 Collector 的人對照的最小清單。
- **目的**：
  - 在 `collector/README.md` 加一節，把設定分成「AIOps 必加」「歸屬與安全」「選配」，每項指到 `code/` 裡的區塊名稱與用途；不複製整段 yaml（避免兩份不同步）。
  - 使用者要求把 Collector 設定拆成「通用」和「AIOps 專屬」：AIOps 專屬的 receiver、processor 和只有 AIOps 用的兩條 pipeline 移到新檔 `collector/code/aiops.yaml`，行為不變。
- **範圍**：做：README 一節、`aiops.yaml` 拆檔、chart／部署腳本／CI 帶第三個檔、測試 cluster 實部署。不做：把 `metrics/in`、`metrics/otlp`、`traces/in` 三條混合的 pipeline 也拆開（processor 順序有關，用 forward connector 要改資料路徑，風險大）；對公司實際的 Collector 實測。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `collector/README.md` | 新增「公司已經有 Collector：AIOps 要加哪些」：三個表（必加、歸屬與安全、選配）、pipeline 與順序、前提（image 版本、Pod label、ES 端結構）；檔案表、部署指令加 `aiops.yaml` | 內容全部從 `code/` 讀出來整理，區塊名稱一致 |
| `collector/code/aiops.yaml`（新）、`node.yaml` | 照原文搬 7 塊：receiver `ntp`、`prometheus/cadvisor`；processor `groupbyattrs/cadvisor`、`transform/cadvisor`、`metricstransform/java-http`、`transform/java-http`、`cumulativetodelta`、`transform/hist-sum`、`transform/exceptions`、`resource/node`；pipeline `metrics/cadvisor`、`metrics/ntp`。`node.yaml` 236 → 132 行，剩通用的 receiver 與 pipeline（引用 `aiops.yaml` 的 processor） | 公司有自己的 Collector 時，AIOps 要加的東西集中在一個檔 |
| `collector/helmchart`、`e2e/k8s/deploy-collector.sh`、`run-stress.sh`、`collector/.gitlab-ci.yml` | `config.aiops`（ConfigMap 多 `aiops.yaml`）、node Collector 多 `--config=/conf/aiops.yaml`（cluster 不變）、`--set-file` 帶第三個檔 | 載入第三個檔 |
| `values.yaml`、`series.yaml`、`run-metrics-e2e.sh` 的註解 | 檔名 `node.yaml` → `aiops.yaml` | 跟著搬 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 清單裡的區塊名稱、pipeline 順序跟 `code/` 一致 | 逐項對 `code/` 原文；程式比對清單裡每個名稱在 `code/` 都有定義 | 一致；文件檢查過 |
| 拆分前後的設定完全相同 | 用 Collector 的合併規則（map 遞迴合併、list 取代）把拆分前 `common＋node` 與拆分後 `common＋node＋aiops` 各自載入比對 | 相等：receivers 6、processors 16、exporters 1、extensions 3、pipelines 7 條，逐項一致 |
| 缺了 `aiops.yaml` 會被擋下 | 真的 Collector（0.162.0）`validate`，只給 `common＋node` | 失敗：`pipelines::metrics/in: references processor "metricstransform/java-http" which is not configured`；cluster 的 `common＋cluster` 通過 |
| chart 載入第三個檔 | `helm template` | node 的參數多 `--config=/conf/aiops.yaml`，cluster 沒有 |
| 真的在測試 cluster 跑得起來、資料沒斷 | `e2e/k8s/deploy-collector.sh` 部署（ConfigMap 變成 4 個檔），等 rollout，再看 ES 最近 3 分鐘寫入 | node、cluster 都 Running、log 沒有設定錯誤；metrics 3,680 筆、log 1,224、trace raw 543、kubeletstats 2,167，直方圖（`cumulativetodelta`、`hist-sum`）最新一筆是 7 秒前 |

**沒測到的**：`prometheus/cadvisor`、`metrics/ntp` 兩條路：這個測試環境沒有帶 CPU limit 的 Pod（拆分前 2 小時、拆分後都是 0 筆，不是拆分造成的），ntp 是 30 分鐘一次、沒等；拿這份清單去改真的、不同版本的公司 Collector（`extract_sum_metric` 等在較舊版本可能沒有，我們用 `otel/opentelemetry-collector-contrib:0.162.0`）。離線 `validate` 兩邊都卡在 `kubeletstats` 要讀 ServiceAccount 的 CA 憑證（容器外沒有），所以改用實部署驗證。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `metrics/in`、`metrics/otlp`、`traces/in` 還是在 `node.yaml`，引用 `aiops.yaml` 的 processor | 這三條混合通用與 AIOps 的 processor，順序有關 | 公司的 Collector 要自己在對應的 pipeline 裡插入那些 processor（README 寫了順序） | 要完全分開得用 forward connector 把 AIOps 的處理接在後面，要改資料路徑並重測 baseline，沒做 |
| 沒有「公司現有 Collector 長什麼樣」的資訊 | 使用者沒給 | 清單是從我們自己的設定倒推的，公司那邊可能已經有同名 processor 或不同的 pipeline 結構 | 拿到公司的 Collector 設定再逐項對 |
