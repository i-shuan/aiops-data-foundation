# 目錄改名：專案名 it-aiops（2026-10）

**狀態**：已完成（k3s e2e 待下次跑）　　**日期**：2026-10-25　　**相關**：分支 `claude/restructure-it-aiops`（從 `claude/product-loop` 開）
**模組**：全部（只改位置與名稱，不改行為）

## Plan：這一次要達到什麼

- **需求**：專案定名 **it-aiops**（公司 IT 系統的 AIOps 平台）。
- 舊目錄 `platform/`、`aiops/`、`apps/`、`test/` 名字含糊、層級重疊：`aiops/` 裡又有 `aiops_baseline`、`baseline/`；`platform/central/` 與 `platform/cluster-addons/` 不看內容猜不出是什麼。
- 新加入的人要翻文件才知道東西在哪。
- **目的**：目錄一眼看懂分工，且**行為完全不變**：單元測試照過、Collector 設定照過 `validate`、`elasticsearch-setup.sh` 照樣能跑、文件連結不斷。
- **範圍**：改目錄、Python 套件名、文件與腳本裡的路徑。
- 不改邏輯、不改設定內容。
- 不改 ES 的 index 名稱（`aiops-anomalies`、`aiops-feedback` 等是資料不是程式）。
- GitHub 上的 repo 名稱不在這次（對外的動作，由你決定何時改）。

| 舊 | 新 | 內容 |
|---|---|---|
| `platform/` | `telemetry/` | 收值與存（①②③） |
| `platform/cluster-addons/otel-collector/` | `telemetry/collectors/` | node、cluster、trace-sampler 的 Collector |
| `platform/central/elasticsearch/` | `telemetry/storage/` | mapping、pipeline、ILM、template |
| `platform/central/*.sh`、`retention.conf` | `telemetry/scripts/` | setup、換 key |
| `platform/groups.yaml` | `telemetry/groups.yaml` | 部門 → group |
| `aiops/` | `engine/` | AIOps 分析核心（④～⑨） |
| `aiops/aiops_common/` | `engine/core/` | 共用（ES 連線、trace 工具；之後 RAG 放這） |
| `aiops/aiops_baseline/` | `engine/detector/` | 動態基準線學習 (Dynamic Baseline Learning)、規則、資料品質、回饋 |
| `aiops/aiops_rca/` | `engine/investigator/` | 根因分析 |
| `aiops/baseline/` | `engine/configs/baseline/` | yaml 設定 |
| `aiops/rca/` | `engine/configs/rca/` | yaml 設定 |
| `aiops/{tests,fake-data,interfaces,requirements.txt}` | `engine/…` | 跟著搬 |
| `apps/` | `apps/` | 給應用團隊：chart、範例、規範、CI 檢查 |
| `test/` | `e2e/` | 本機 k3s、ES pipeline 案例 |
| `docs/` | 不動 | |

- `engine/configs/` 下保留 `baseline/`、`rca/` 子資料夾、不攤平：兩個資料夾都有 README.md。

**Python 怎麼跑變了**：

| 舊 | 新 |
|---|---|
| `cd aiops && python3 -m aiops_baseline <指令>` | `cd engine && python3 -m detector <指令>` |
| `python3 -m aiops_rca investigate` | `python3 -m investigator investigate` |

## Do：改動哪些 code、改了什麼、為何要改

| 範圍 | 改了什麼 | 為何 |
|---|---|---|
| 搬檔 | 全部用 `git mv` | git 認得是改名，歷史追得到 |
| Python | 套件名 `aiops_common/baseline/rca` → `core/detector/investigator`，import 跟著改 | 套件名要跟目錄一樣 |
| `telemetry/scripts/elasticsearch-setup.sh` | `cd` 到 `telemetry/`；`elasticsearch/…` 改 `storage/…` | 腳本在 `scripts/`、設定在 `storage/` |
| 同上 | `retention.conf` 改 `scripts/retention.conf` | `cd` 到 `telemetry/` 後，`retention.conf` 在 `scripts/` |
| 文件裡的 markdown 連結 | 用一支腳本：先用舊位置解析，再依搬移後的位置重算相對路徑 | 手改 190 個檔案會漏 |
| 程式碼與文字裡的路徑 | 同一支腳本用規則替換 | 同上 |
| 同上 | `apiVersion: apps/v1` 這種不是我們的路徑不碰 | — |
| `README.md` | 標題 `it-aiops`；目錄段落改寫；加 Python 的執行位置 | — |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 搬完 Python 沒壞 | `cd engine && pytest` | 200 個單元測試過 |
| 指令還起得來 | `python3 -m detector --help`、`python3 -m investigator --help` | 兩個都能起來並列出子指令 |
| 文件連結不斷 | 掃全部 `.md` 的相對連結（412 個）看目標存不存在 | 0 個壞連結 |
| 設定檔沒弄壞 | 全部 `.yaml`、`.json` 解析；全部 `.sh` `bash -n` | 全部通過（例外見下） |
| Collector 設定在新位置還能用 | `otelcol validate`（掛 `telemetry/collectors/`） | common+cluster、sampler 兩組都通過 |
| 同上：node | 同上 | 要 K8s 憑證檔，測試機沒有，跟搬之前一樣 |
| `elasticsearch-setup.sh` 搬完還能跑 | 對 es-t 實際跑 | 28 個步驟都做完、發出 `es-writer` key |
| 同上：`SKIP_KEYS=1` | 同上 | 正常 |
| e2e 腳本的路徑 | 讀腳本：`repo="${here}/../.."` 深度沒變（`e2e/local-k3s` 跟 `test/local-k3s` 同深） | 路徑對得上；**沒有在 k3s 實跑** |
| 同上：kustomization | `../../../telemetry/collectors` 存不存在 | 存在 |

- `apps/examples/mes-api/templates/*.yaml` 是 Helm 範本，本來就不是純 YAML，不算失敗。

**沒測到**：11 支 e2e 腳本在 k3s 實跑；`apps/ci/validate-charts.sh`（要 helm，測試機沒有）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 其他還沒合的分支會跟這次搬移衝突 | `single-writer-key` → `alert-exit` → `product-loop` 的路徑是舊的 | 先合那一串，git 通常認得改名；反過來會大量衝突 | 合併順序見下 |
| 部署端（CI／Rancher／Argo）若寫死舊路徑 | 這個 repo 看不到部署端的設定 | 部署會找不到（例：`platform/cluster-addons/otel-collector`） | 上線前問管部署的人；對照表在上面 |
| 舊的 PDCA 紀錄裡路徑也被改成新的 | 為了連結不斷 | 讀到 2026-10 初的紀錄，路徑是現在的、不是當時的 | 接受；git 歷史有當時的 |
| GitHub repo 還叫 `aiops-data-foundation` | 對外的名稱 | clone 下來的資料夾名是舊的 | 你決定何時改；改了 GitHub 會自動轉址舊網址 |

- **合併順序**：`claude/single-writer-key` → `alert-exit` → `product-loop` → `restructure-it-aiops`；這個分支已經包含前三個，只要合最後一個就全進去。
- **決定**：用建議的結構；`engine/configs/` 下保留 `baseline/`、`rca/` 子資料夾；測試放 `engine/tests/`。
