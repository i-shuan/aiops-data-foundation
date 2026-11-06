# 刪除自建收值（Collector、埋點、ES 的 OTel 入口）（2026-11）

**狀態**：已完成　　**日期**：2026-11-02　　**相關**：分支 `claude/remove-collector`；接在 [apm-source](2026-11-apm-source.md) 之後
**模組**：①②③

## Plan：這一次要達到什麼

- **需求**：收值改用公司現有的 Elastic APM（engine 已能讀，見 [apm-source](2026-11-apm-source.md)）。repo 裡還留著自建的 OTel Collector、OTel 埋點指引、ES 的 OTel 入口設定和對應文件，同時存在兩套收值的說法，看的人搞不清楚現在是哪一套。
- **目的**：repo 只剩現在的架構——收值是公司的 Elastic APM，這裡只有 engine、Grafana，以及在公司 ES 上補的最小設定；文件只講這一套。
- **範圍**：
  - 刪：`collector/`、`apps/`、`e2e/`、`elasticsearch/` 的 OTel 入口（pipeline、mapping、ILM、template、transform、測試用 ES chart、換寫入 key 腳本）、`docs/log`、`docs/metrics`、`docs/trace`、`docs/retention*.md`。
  - 留並改：`elasticsearch/` 只剩 `traces-merged-*` 的 template 與 ILM、engine 與 Grafana 兩把 key、snapshot；架構文件、README、索引、CI。
  - 不做：engine 的 OTel 來源（`source: otel`、`configs/baseline/` 等）不動；PDCA 紀錄不改內容（只把指向已刪檔案的連結改成純文字）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `collector/`（16 檔）、`apps/`（12 檔）、`e2e/`（79 檔） | 刪 | 自建收值與它的埋點、模擬服務、端到端腳本；e2e 的腳本都靠 Collector 送資料，沒有它就跑不起來 |
| `elasticsearch/code/storage/` | 只留 `template-traces-merged.json`、`ilm-traces-merged.json`（兩個 `_meta` 說明改成 APM） | 其餘是 log／metrics／trace 的 OTel 入口設定；`traces-merged-*` 是 engine 自己寫的，要先有 nested 的 template |
| `elasticsearch/code/scripts/elasticsearch-setup.sh` | 311 行 → 約 110 行：套 `traces-merged` 的 ILM 與 template、snapshot、發兩把 key（engine：讀 `traces-apm*`、`logs-apm*`、`metrics-apm*`，讀寫 `aiops-*`、`traces-merged-*`；grafana） | 原本發 4 把 key（寫入、唯讀、baseline、健康檢查），其中寫入與健康檢查是給 Collector 的；讀的索引改成 APM 的 |
| `elasticsearch/code/scripts/retention.conf` | 只留 `TRACES_DELETE_AFTER` 與 snapshot 設定 | 其餘天數是 OTel 資料流的 ILM |
| `elasticsearch/setup-helmchart/`、`elasticsearch/.gitlab-ci.yml` | key 從 5 把改 2 把；`targets` 拿掉 collector | 跟腳本一致 |
| `.gitlab-ci.yml`、`sonar-project.properties`、`.gitignore` | 不再 include `collector/.gitlab-ci.yml`；sonar 範圍拿掉 collector、sdk | 資料夾沒了 |
| `elasticsearch/README.md` | 重寫：公司 ES 上補什麼、兩把 key、CI、備份與還原 | 原本一半在講測試用 ES chart 與 Collector |
| `docs/architecture.md` | 架構圖、目標與邊界、3.1～3.3 決策、①②③ 模組職責、各模組現況、驗證表、附錄 | 收值改用 Elastic APM；Collector 的邊緣身份注入、寫入憑證等決策不再成立；Phase 1 的 PDCA 索引標「歷史」 |
| `README.md`、`CLAUDE.md`、`docs/README.md` | 新架構、目錄、索引；CLAUDE.md 的結構表與兩個收值術語列 | 同上 |
| `docs/troubleshooting.md` | 刪收值、log、metrics、trace、.NET、Elasticsearch、本機測試環境六個區塊；新增「Elastic APM 的資料」一區 | 前面六個都是 Collector 與測試環境的問題 |
| `docs/data-storage-reference.md`、`docs/aiops-data-reference.md`、`docs/rca/README.md`、`grafana/*`、`aiops-engine/` 的 README 與 baseline、rca 說明 | 改成 APM 的索引與欄位，或把指到已刪檔案的連結改掉、「實測」標明 e2e 已刪 | 連結檢查 |
| `aiops-engine/helmchart/values-test.yaml`、`grafana/helmchart/values-test.yaml`、`tests/rca_k3s.yaml`、`tests/trace_k3s.yaml` | 刪 | 只給已刪的 e2e 腳本與 demo 用 |
| `docs/deploy-how-to.md`（新） | 部署步驟：不跑 `es-setup`，在 Kibana 手動補 `traces-merged` 的 ILM 與 template、engine 的角色與 key；部署 engine 帶 `source=apm`；接 Grafana | 公司已有 ES，管 ES 的人不一定讓腳本跑；原本的 engine 角色只有寫、沒有讀 |
| `docs/pdca/` 內 10 份紀錄 | 31 個指向已刪檔案的連結改成純文字，內容不動 | 紀錄是當時的措辭，但連結不能斷 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 刪的東西沒有被留下的東西依賴 | 刪完 grep `collector/`、`apps/`、`e2e/`、`groups.yaml`、`rotate-writer`、`es-writer` | 剩的是 PDCA 紀錄（歷史）、engine 的測試 docstring（`trace_quality_edge_e2e.py` 提到取樣 Collector）、`feedback_extract_eval.py` 的本機 `.env` 路徑，都只是文字 |
| 文件沒有斷連結、索引完整 | `tools/check-docs.py` | 連結、索引、舊說法、類型都 0 個問題（第一次刪完 80 個連結壞掉，分三批修完） |
| engine 沒被影響 | `cd aiops-engine && python3 -m pytest -q` | 見下方 Act 的記錄 |
| 兩把 key 的權限寫對 | 讀腳本與 `run.sh`、`values.yaml` 的欄位、`targets` 對應 | 未在 ES 上跑：腳本沒執行過 |

**沒測到的**：`elasticsearch-setup.sh` 瘦身後沒對真實 ES 跑；`setup-helmchart` 沒 render 驗證；GitLab 流程本來就沒實跑過。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| engine 的 OTel 來源還在（`configs/baseline/` `rca/` `schedule/`、`source: otel`、`quality.yaml` 的 `k8s-*`、單元測試用 OTel 欄位） | 這次只刪 ①②③，④ 的程式不動 | repo 裡仍有一套讀 OTel 格式的設定，沒有東西產生那種資料；容易讓人以為還能用 | 決定要不要把 `configs/apm/` 升為預設並刪掉 OTel 來源；牽涉 500 多個測試的欄位，另開一輪 |
| e2e 全刪，engine 沒有端到端驗證環境 | e2e 的腳本都靠 Collector 送資料 | `aiops-engine/tests/*_e2e.py`（`loop_e2e` 等直接寫合成資料進 ES）沒有一起刪，仍可手動跑，但缺 K8s 上的整條線 | 要在真實 APM 資料上驗證；需要的話另寫用 APM 格式的合成資料 |
| 示範儀表板（`e2e/k8s/rca-demo/`）一併刪了 | 它靠 Collector 與模擬服務 | 沒有展示用的 demo | 要的話改用 APM 格式的資料重做 |
| `elasticsearch-setup.sh`、`setup-helmchart` 沒實跑 | 沒有 ES 可跑 | key 權限寫錯到實際用才發現 | 第一次部署時照 `elasticsearch/README.md`（已移除，改成 [kibana-dev-tools.txt](../../../elasticsearch/kibana-dev-tools.txt)） 跑，看 `GET _security/api_key` |

- **決策**：收值相關的程式與文件一次刪乾淨，不留「已棄用」的資料夾；歷史在 git。engine 的 OTel 來源先留著，另行決定。
- **下一輪**：真實 APM 資料上跑 `detect`、`trace-merge`（見 [apm-source](2026-11-apm-source.md)）；決定 engine 的 OTel 來源。
