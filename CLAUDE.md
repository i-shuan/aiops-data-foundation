# it-aiops 專案慣例

給每一個在這個 repo 寫程式或寫文件的人（含 Claude）。程式與架構見 [docs/README.md](docs/README.md) 的文件地圖、[docs/architecture.md](docs/architecture.md)。

## 結構

| 資料夾 | 內容 |
|---|---|
| `elasticsearch/` | ③ 在公司 ES 上補的東西：`traces-merged-*` 的 template 與 ILM、engine 與 Grafana 的 key（`code/`）、`setup-helmchart/`（在 K8s 裡跑 setup 的 Job） |
| `grafana/` | ⑧ 通知、儀表板、回饋表單（`code/` provisioning、`helmchart/`） |
| `aiops-engine/` | ④～⑨ AIOps（`code/`：主程式 `core/`（含 `plugin.py` 擴充點）、detector、investigator、feedback_web、`sources/`（資料來源介面：ES、檔案、HTTP、程式給）、`models/`（五個模型的輸入規格）；`plugins/`（情境各一個：`observability/` IT、`factory/` 工廠（ML／FDC 的異常接進 ⑤、履歷取代 trace）、`metrics_only/` 只有指標；`AIOPS_PLUGIN` 選）；configs（`apm/` Elastic APM、`factory/`、`metrics_only/`、`baseline/` `rca/` `schedule/` OTel）、Dockerfile；`helmchart/`） |
| `docs/` | 原理、案例、PDCA 紀錄 |

**收值（①②③）是公司現有的 Elastic APM 與 ES，不在這個 repo**（2026-11 自建的 Collector 已刪）。模組編號只用 ①～⑨：① 收值 ② 清資料 ③ 存 ④ 判斷 ⑤ 異常總表 ⑥ 收斂 ⑦ 根因分析 ⑧ 出口 ⑨ feedback。

## 術語對照表

寫文件一律用左欄。右欄是舊說法，**不要再寫**（PDCA 紀錄是當時的措辭，不回頭改）。

| 正式術語 | 舊說法 | 檢查腳本會擋 |
|---|---|---|
| 動態基準線學習 (Dynamic Baseline Learning) | 學平常的樣子、學「平常」、跟自己的歷史比 | |
| Pod 同儕橫向比對 (Peer Comparison) | 跟兄弟比、兄弟比、Pod 互比（程式裡的名字 `peer` 照舊） | 跟兄弟比 |
| 靜態臨界值規則 (Static Threshold Rules) | 規則（一次就算）、保底規則、固定門檻 | 保底規則 |
| 單點即判定 (Instant Anomaly) | 單次就報、一次就算（`confirm_points: 1`、`instant_z`） | |
| 例外吞噬檢測 (Exception Swallowing Detection) | 吞例外、吞掉的例外（規則 `swallowed`） | |
| 資深維運專家回饋 (Expert Feedback) | 老師傅回饋 | |

- 「老師傅」是產品用語（賣點之一），使用者介面和對話可以用；正式文件第一次出現寫「資深維運專家（老師傅）」。「一件」（一件事故）、「值班」、「一把 key」（量詞）都是正常用語，不在表內。
- 檢查腳本只擋「檢查腳本會擋」欄的片語，其餘靠人看。要加新片語：先加進這張表，再加進 [tools/check-docs.py](tools/check-docs.py) 的 `FORBIDDEN`。

## 文件擺放

| 寫什麼 | 放哪裡 |
|---|---|
| 為什麼這樣設計、原理、方法的限制 | `docs/`（log、metrics、trace、rca、feedback） |
| 怎麼跑、參數、輸出在哪、怎麼驗 | 跟程式放一起：`aiops-engine/code/configs/<名>/README.md`（操作）、`reference.md`（規格）、`verification.md`（實測）、各元件的 `README.md`（部署）、`e2e/<環境>/README.md`；原理用連結指回 `docs/`，不重寫 |
| 每次工作的紀錄 | `docs/pdca/phase<N>-…/`；不屬於某個 phase 的放 `docs/pdca/meta/`；更新 [docs/pdca/README.md](docs/pdca/README.md) |
| 構想、還沒做的 | 檔頭加一行 `> ⚪ 構想，尚未實作`；部分做了用 `> 🟡`；在 docs/README.md 標狀態 |

### Diátaxis：每份共用文件只做一種事

`docs/` 底下的共用文件（不含 PDCA 紀錄）開頭都有一行 `> **類型：…**`，四種之一，**不要把兩種寫在同一份**：

| 類型 | 回答 | 檔名慣例 |
|---|---|---|
| 原理 Explanation | 為什麼這樣設計？ | 主題的 `README.md`、`architecture.md`、`<主題>.md` |
| 操作 How-To | 我要做 X，怎麼做？ | `how-to.md`、`troubleshooting.md`、`*-how-to.md` |
| 規格 Reference | 查欄位、參數、現況 | `reference.md`、`catalog.md`、`*-reference.md` |
| 案例 Cases | 實際跑一遍長什麼樣？ | `cases.md`、`scenarios.md` |

- 一個主題要寫的東西超過一種，就在**同一個資料夾**拆成不同檔案（不另開 `explanation/`、`how-to/` 資料夾），並在 `docs/README.md` 標類型。
- 「現況與待辦」「還沒做」留在該主題的原理文件（Diátaxis 沒有「狀態」這一類，放在原理最接近）。
- 步驟（部署、驗證、遇到問題怎麼辦）不要寫進原理或規格；原理（為什麼）不要寫進操作，用連結指過去。
- `aiops-engine/`、`elasticsearch/`、`grafana/` 旁邊的 README 本來就貼著程式（操作、規格），多半不拆，只在 `docs/README.md` 標類型。**例外：`aiops-engine/code/configs/baseline/` 已依類型拆開**——`README.md` 操作、`reference.md` 規格、`verification.md` 案例（實測），原理在 [rca/README.md](docs/rca/README.md)；這個資料夾的 md 同樣要標類型。

- 新增、刪除、改名 md：同一個 commit 更新 `docs/README.md`（PDCA 紀錄更新 `docs/pdca/README.md`）。
- 長文（超過約 300 行）在第一個 `##` 前放 `<!-- toc -->` 和 `<!-- /toc -->`，用 `python3 tools/check-docs.py --toc` 產生，不要手寫目錄。
- 提交前：`python3 tools/check-docs.py`（連結、索引、舊說法），改了程式再跑 `cd aiops-engine/code && python3 -m pytest -q`。

## 工作方式

- **新工作先開 branch、先寫 PDCA 計畫**，再動手。PDCA 四段：**Plan**（需求、目的、範圍）、**Do**（改了哪些程式、為什麼）、**Check**（測試的目的與結果，不寫「N 個測試通過」就算）、**Act**（沒解決的，在什麼情況下發現的）。範本 [docs/pdca/TEMPLATE.md](docs/pdca/TEMPLATE.md)。
- **只有被明確說「合回 master」才合進 master**；「好」不算。
- 告警一個服務一件；通知交給 Grafana；部門決定誰看得到。
