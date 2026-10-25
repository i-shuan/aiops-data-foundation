# 目錄改名：專案名 it-aiops（2026-10）

**狀態**：已完成（k3s e2e 待下次跑）　　**日期**：2026-10-25　　**相關**：分支 `claude/restructure-it-aiops`（從 `claude/product-loop` 開）
**模組**：全部（只改位置與名稱，不改行為）

## Plan：這一次要達到什麼

- **需求**：專案定名 **it-aiops**（公司 IT 系統的 AIOps 平台）。原本的目錄 `platform/`、`aiops/`、`apps/`、`test/` 名字含糊、層級重疊（`aiops/` 裡又有 `aiops_baseline`、`baseline/`；`platform/central/` 與 `platform/cluster-addons/` 不看內容猜不出是什麼），新加入的人要翻文件才知道東西在哪。
- **目的**：目錄一眼看懂分工，且**行為完全不變**：單元測試照過、Collector 設定照過 `validate`、`elasticsearch-setup.sh` 照樣能跑、文件連結不斷。
- **範圍**：改目錄、Python 套件名、文件與腳本裡的路徑。不改邏輯、不改設定內容、不改 ES 的 index 名稱（`aiops-anomalies`、`aiops-feedback` 等是資料不是程式，不動）。GitHub 上的 repo 名稱不在這次（對外的動作，由你決定何時改）。

| 舊 | 新 | 內容 |
|---|---|---|
| `platform/` | `telemetry/` | 收值與存（①②③） |
| `platform/cluster-addons/otel-collector/` | `telemetry/collectors/` | node、cluster、trace-sampler 的 Collector |
| `platform/central/elasticsearch/` | `telemetry/storage/` | mapping、pipeline、ILM、template |
| `platform/central/*.sh`、`retention.conf` | `telemetry/scripts/` | setup、換 key |
| `platform/groups.yaml` | `telemetry/groups.yaml` | 部門 → group |
| `aiops/` | `engine/` | AIOps 分析核心（④～⑨） |
| `aiops/aiops_common/` | `engine/core/` | 共用（ES 連線、trace 工具；之後 RAG 放這） |
| `aiops/aiops_baseline/` | `engine/detector/` | 學平常的樣子、規則、資料品質、回饋 |
| `aiops/aiops_rca/` | `engine/investigator/` | 根因分析 |
| `aiops/baseline/`、`aiops/rca/` | `engine/configs/baseline/`、`engine/configs/rca/` | yaml 設定（兩個資料夾都有 README.md，所以保留子資料夾，不攤平） |
| `aiops/{tests,fake-data,interfaces,requirements.txt}` | `engine/…` | 跟著搬 |
| `apps/` | `sdk/` | 給應用團隊：chart、範例、規範、CI 檢查 |
| `test/` | `e2e/` | 本機 k3s、ES pipeline 案例 |
| `docs/` | 不動 | |

**Python 怎麼跑變了**：`cd engine && python3 -m detector <指令>`（原 `cd aiops && python3 -m aiops_baseline`）、`python3 -m investigator investigate`（原 `aiops_rca`）。

## Do：改動哪些 code、改了什麼、為何要改

| 範圍 | 改了什麼 | 為何 |
|---|---|---|
| 搬檔 | 全部用 `git mv`（git 認得是改名，歷史追得到） | — |
| Python | 套件名 `aiops_common/baseline/rca` → `core/detector/investigator`，import 跟著改 | 套件名要跟目錄一樣 |
| `telemetry/scripts/elasticsearch-setup.sh` | 腳本在 `scripts/`、設定在 `storage/`，所以 `cd` 到 `telemetry/`，`elasticsearch/…` 改 `storage/…`、`retention.conf` 改 `scripts/retention.conf` | 目錄結構變了，腳本內的相對路徑要跟著 |
| 文件與腳本裡的路徑 | 用一支腳本改：markdown 連結先用舊位置解析、再依搬移後的位置重算相對路徑；程式碼與文字裡的路徑用規則替換；`apiVersion: apps/v1` 這種不是我們的路徑不碰 | 手改 190 個檔案會漏 |
| `README.md` | 標題 `it-aiops`；目錄段落改寫；加 Python 的執行位置 | — |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 搬完 Python 沒壞 | `cd engine && pytest`；`python3 -m detector --help`、`python3 -m investigator --help` | 200 個單元測試過；兩個指令都能起來並列出子指令 |
| 文件連結不斷 | 掃全部 `.md` 的相對連結（412 個）看目標存不存在 | 0 個壞連結 |
| 設定檔沒弄壞 | 全部 `.yaml`、`.json` 解析；全部 `.sh` `bash -n` | 全部通過（`sdk/examples/mes-api/templates/*.yaml` 是 Helm 範本，本來就不是純 YAML） |
| Collector 設定在新位置還能用 | `otelcol validate`：common+cluster、sampler（掛 `telemetry/collectors/`） | 兩組都通過；node 那份要 K8s 憑證檔，測試機沒有，跟搬之前一樣 |
| `elasticsearch-setup.sh` 搬完還能跑 | 對 es-t 實際跑（含 `SKIP_KEYS=1`） | 第一次跑發現 `source retention.conf` 找不到檔案（腳本 `cd` 到 `telemetry/` 之後 `retention.conf` 在 `scripts/`），修掉後 28 個步驟都做完、發出 `es-writer` key；`SKIP_KEYS=1` 也正常 |
| e2e 腳本的路徑 | 讀腳本：`repo="${here}/../.."` 深度沒變（`e2e/local-k3s` 跟 `test/local-k3s` 同深）；kustomization 的 `../../../telemetry/collectors` 存在 | 路徑對得上；**沒有在 k3s 實跑** |

**沒測到**：11 支 e2e 腳本在 k3s 實跑；`sdk/ci/validate-charts.sh`（要 helm，測試機沒有）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 其他還沒合的分支會跟這次搬移衝突 | 改名後，`claude/single-writer-key` → `alert-exit` → `product-loop` 這一串的檔案路徑是舊的 | 先合那一串再合這個，git 通常認得改名；反過來會有大量衝突 | **合併順序：single-writer-key → alert-exit → product-loop → restructure-it-aiops**（這個分支已經包含前三個，所以只要合最後一個就全進去） |
| 部署上線的 CI／Rancher／Argo 如果寫死了舊路徑（`platform/cluster-addons/otel-collector`） | 這個 repo 看不到部署端的設定 | 路徑變了，部署會找不到 | 上線前問管部署的人；`platform/…` → `telemetry/…` 的對照表在上面 |
| 舊的 PDCA 紀錄裡路徑也被改成新的 | 為了連結不斷 | 讀到 2026-10 初的紀錄，路徑是現在的、不是當時的 | 接受；git 歷史有當時的 |
| GitHub repo 還叫 `aiops-data-foundation` | 對外的名稱 | clone 下來的資料夾名是舊的 | 你決定何時改；改了之後 GitHub 會自動轉址舊網址 |

- **決定**：用建議的結構；`engine/configs/` 下保留 `baseline/`、`rca/` 子資料夾；測試放 `engine/tests/`。
