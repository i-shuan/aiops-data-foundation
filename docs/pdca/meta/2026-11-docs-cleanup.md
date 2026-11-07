# 整理舊的 md：先盤點，再決定刪、改、併（2026-11）

**狀態**：進行中（盤點完成；舊紀錄已封存；共用文件的改寫還沒做）　　**日期**：2026-11-07　　**相關**：分支 `claude/docs-cleanup`（從 master `27d58dd` 開）
**模組**：—（文件）

## Plan：這一次要達到什麼

- **需求**：程式結構這一輪大改（`shared`→`common`、設定模型搬進各功能、`detector/`、`investigator/`、`core/` 早已不在），文件有一部分還在講舊的結構、已經刪掉的自建 Collector、不存在的 e2e 腳本。讀的人分不出哪些是現在的。
- **目的**：每一份 md 要嘛描述**現在**的程式與行為，要嘛明確標成歷史紀錄；不再有「指到不存在的檔案或資料夾」。
- **範圍**：這一份只做**盤點**（下面的表）與決定要問使用者的事；動手改是下一步，等使用者看過盤點、回答「要問使用者的事」之後才做。不改程式。

## Do：盤點方法與結果

**方法**：`git ls-files '*.md'` 共 141 份。用腳本對每份數兩件事：①舊詞（`detector/`、`investigator/`、`core/`、`shared/`、`Collector`、`plugin`、`M4`…）出現幾次；②行內程式碼（反引號）裡的路徑，在 repo 裡找不到幾個。數字只是線索，**舊詞有誤判**：`plugin` 在 Grafana 外掛與環境變數 `AIOPS_PLUGIN` 是現在的正確用法、`OTel` 是仍支援的資料來源，所以下面的判斷是人看過那幾行才下的。

**分兩塊**：PDCA 紀錄 112 份（歷史，不改寫）；其餘 29 份（共用文件、README，要讓它描述現在）。

### 一、共用文件與 README（29 份）

| 檔案 | 行 | 現況 | 建議 | 為什麼 |
|---|---|---|---|---|
| `CLAUDE.md` | 65 | 結構列還寫 `infra/` 有 `clock.py`、沒有 `clock/`、`config/` | **小修** | 這份是給寫程式的人（含 Claude）的第一手說明，不能舊 |
| `README.md`、`aiops-engine/README.md`、`aiops-engine/src/common/README.md` | 94／173／48 | 上一步剛改成現在的結構 | 保留 | — |
| `aiops-engine/src/common/scenario/README.md` | 77 | `plugin` 是 `AIOPS_PLUGIN` 的正確用法；有 6 個程式路徑寫成舊名（`incident/alert_service` 缺 `.py` 等） | **小修** | 路徑補齊 |
| `docs/README.md`、`docs/pdca/README.md` | 75／235 | 索引。`pdca/README.md` 有 19 筆在講 phase1 的自建 Collector | 索引隨改動更新 | 規則：增刪改名 md 同一個 commit 更新 |
| `docs/architecture.md` | 491 | 對照表（§ 模組與程式）指到不存在的 `core/source.py`、`merge.py`、`report.py`、`llm.py`、`aiops-engine/scheduler`；附錄 415～465 行是 phase1 Collector 紀錄的索引 | **改寫那幾張表**；附錄見下「要問的事 1」 | 原理文件，不該指到不存在的檔案 |
| `aiops-engine/configs/baseline/reference.md` | 395 | 11 處「Collector」把 OTel 版的行為當現在；`core/owner.py`、`core/version.py`、`detector/owner.py` 已搬到 `detect/store/owner_repo.py`、`common/infra/env.py` | **改寫**：OTel 版標成「OTel 來源」，路徑改現在的 | 規格文件，欄位與路徑要準 |
| `aiops-engine/configs/baseline/verification.md` | 182 | 指到 `e2e/k8s/run-*-e2e.sh`、`elasticsearch-setup.sh`（repo 裡沒有，e2e 現在是 `tests/*_e2e.py`） | **改寫**：實測紀錄保留，重跑指令改成 `python3 -X utf8 -m tests.<名>_e2e` | 「怎麼重跑」不能指到不存在的腳本 |
| `aiops-engine/configs/baseline/README.md` | 163 | 沒有舊詞、沒有壞路徑 | 保留（抽查） | — |
| `aiops-engine/configs/rca/README.md` | 214 | 3 處 `investigator/severity.py`、`investigator/similar.py`（現在是 `incident/alert_severity.py`、`llm/similar.py`）；`e2e/k8s/run-rca-e2e.sh` 不存在 | **小修** | 路徑 |
| `aiops-engine/configs/rca/example-report.md` | 73 | 報告範例，沒有壞路徑 | 保留；**確認內容跟現在的報告格式一樣** | 這是 `rca_report` 輸出的樣子 |
| `aiops-engine/configs/apm/README.md`、`factory/README.md` | 76／72 | 現行 | 保留；`factory` 的 `plugin` 是正確用法 | — |
| `aiops-engine/fake-data/README.md`、`interfaces.md` | 10／61 | `interfaces.md` 指到 `detector/`、`sources.py`（現在是 `detect/reference_data.py`） | **小修** | 路徑 |
| `docs/troubleshooting.md` | 64 | `detector/rules.py`、`e2e/pipeline/run-alert-flap.py`、`grafana/code/alert-rules.yaml` 都不存在 | **改寫** | 操作文件，指到不存在的檔案會讓人卡住 |
| `docs/aiops-data-reference.md` | 312 | `aiops-engine/feedback/suggest.py`、`trace_stats.py`、`learn/feedback_service.submit` 不是現在的名字 | **小修**（欄位表本身要用現在的索引 mapping 對一次） | 規格 |
| `docs/data-storage-reference.md`、`detection-and-learning.md`、`img/README.md` | 103／75／15 | 沒有舊詞、沒有壞路徑 | 保留（抽查） | — |
| `docs/feedback/README.md`、`docs/rca/README.md` | 102／161 | `adjust.py`、`cmd_baseline.py`、`investigator/` 的舊名各 2 處 | **小修** | 路徑 |
| `docs/deploy-how-to.md` | 124 | `dashboards/forms/`、`deploy/aiops-beat` 等名字要對 helm chart 現在的 | **對一次再改** | 操作文件 |
| `docs/semiconductor-design.md` | 110 | 工廠情境設計；`M4`（舊的分段代號）3 處、`detect/store/models` 缺副檔名 | **小修** | 用詞 |
| `grafana/README.md`、`grafana/code/INSTALL-form-plugin.md` | 168／84 | `plugin`（Grafana 外掛）是正確用法；幾個路徑寫成 chart 內的相對名 | **抽查後小修** | — |
| `docs/pdca/TEMPLATE.md` | 47 | 範本 | 保留 | — |

小計：保留 10 份、小修 11 份、改寫 5 份（`architecture.md` 的表、`reference.md`、`verification.md`、`troubleshooting.md`，加 `deploy-how-to.md` 對過再定）、對一次再定 3 份。

### 二、PDCA 紀錄（112 份，歷史，不改寫）

| 資料夾 | 份數 | 內容 | 建議 |
|---|---|---|---|
| `phase1-collect/` | 19 | **全部是已刪除的自建 Collector**（`architecture.md` 附錄自己也這樣標） | 見「要問的事 1」 |
| `phase2-anomaly/` | 19 | 判斷（baseline、peer、規則） | 保留 |
| `phase3-locate/` | 8 | 併件、根因定位 | 保留 |
| `phase4-rca-llm/` | 7 | 報告、LLM | 保留 |
| `phase5-learning-loop/` | 15 | 回饋與調整 | 保留 |
| `meta/` | 44（含本份） | 重構與流程；其中 `2026-11-feature-folders.md`、`target-architecture.md`、`clean-code.md`、`oop-core.md`、`frameworks.md` 描述的是**中間態**的結構（`shared/`、`core/`） | 保留；它們是當時的措辭（CLAUDE.md 規則），索引裡標「已被後續取代」即可 |

## 要問使用者的事（動手前要定）

1. **phase1 的 19 份 Collector 紀錄**：①原地保留（現狀）；②搬到 `docs/pdca/archive/phase1-collect/`，索引縮成一行；③刪掉（git 歷史還在）。我建議②：它們已經不描述任何現在的程式，但當時的實測數字（落盤、重啟、保留天數）有人可能還要查。
2. **OTel 來源**：根 `README.md`「還沒定案的」第 5 點寫「OTel 來源是否一併刪除待決定」。程式與 `configs/baseline/`、`rca/`、`schedule/` 還在支援它。文件要不要繼續講 OTel 版？決定之前，我只把 `reference.md` 裡的 Collector 說法標成「OTel 來源」，不刪。
3. **`pdca/README.md` 235 行的索引**：要不要把「依 phase」的索引砍成只列**現況與待辦**，每一輪紀錄改成資料夾自己的目錄？目前它同時做「現況」和「歷史索引」，兩種事混在一起（違反 Diátaxis 那條規則）。

## Check：這份盤點可信到哪

- 舊詞次數與壞路徑是**線索**，不是結論：`plugin`、`OTel`、`code/` 都有正確用法，所以每份的建議是看過那幾行才寫的；沒看過全文的標「抽查」、「對一次再定」。
- 沒有逐份讀完 29 份共用文件的全文；`architecture.md`（491 行）、`reference.md`（395 行）只看了被指到不存在路徑的那幾段。動手時每份改完再跑 `tools/check-docs.py`，並對每個改過的路徑 `ls` 一次。
- 壞路徑的偵測是「反引號裡長得像路徑、repo 裡找不到」，會漏掉沒加反引號的，也會誤報（例如 `values-local.yaml` 是使用者自己建的）。

## Act

- 動手改之前先回答上面三件事；回答前不動任何 md。
- 做完之後的長期防護：把「反引號路徑要存在」加進 `tools/check-docs.py`（現在只查連結、索引、舊說法、類型），否則下次搬檔又會留下一批壞路徑。這件事要不要做，也請一併決定。

## 封存的執行（使用者說「舊有的移到 archive」）

- **Do**：`git mv` 34 份到 `docs/pdca/archive/`：`phase1-collect/` 19 份，加 `meta/` 的 15 份（10 月的文件整理 10 份：`restructure`、`restructure-components`、`merge-architecture`、`merge-topic-docs`、`feedback-readme`、`log-readme`、`metrics-trace-readme`、`product-line-table`、`retention-split`、`docs-vs-code`；`collector-es-exporter`；`target-architecture`、`oop-core`、`vo-models`、`model-modules`）。內容不改。腳本把所有 md 的相對連結改到新位置（101 個連結；被搬的檔案自己往外的連結也重算）；`tools/check-docs.py` 的舊說法檢查加上 `docs/pdca/archive/`（跟其他 PDCA 紀錄一樣是當時的措辭）。`docs/pdca/README.md` 加「封存」一節。
- **挑的標準**：描述的程式或文件配置已經不在 repo，而且不是現在結構的依據。**沒搬**的：`feature-folders`（`tests/test_layers.py` 引用它）、`frameworks`、`clean-code`、`plugin-architecture`（`AIOPS_PLUGIN` 還在）、`remove-collector`（說明為什麼刪）、`diataxis`、`docs-governance`。
- **Check**：`tools/check-docs.py` 連結、索引、舊說法、類型都過（搬完之前有 97 個壞連結）。
- **Act**：「要問的事 2、3」（OTel 來源文件、`pdca/README.md` 的索引結構）與共用文件的改寫還沒做；搬動的範圍是我挑的，要增減就 `git mv` 一次。
