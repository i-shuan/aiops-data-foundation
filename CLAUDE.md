# it-aiops 專案慣例

給每一個在這個 repo 寫程式或寫文件的人（含 Claude）。程式與架構見 [docs/README.md](docs/README.md) 的文件地圖、[docs/architecture.md](docs/architecture.md)。

## 結構

| 資料夾 | 內容 |
|---|---|
| `telemetry/` | ①②③ 收值、清資料、存（Collector、ES 設定） |
| `engine/` | ④～⑨ AIOps（detector、investigator、feedback_web、deploy、configs） |
| `sdk/` | 應用用的 Helm chart、埋點指引 |
| `e2e/` | 測試環境（k3d、k3s）與模擬服務，**不是正式程式** |
| `docs/` | 原理、案例、PDCA 紀錄 |

模組編號只用 ①～⑨：① 收值 ② 清資料 ③ 存 ④ 判斷 ⑤ 異常總表 ⑥ 收斂 ⑦ 根因分析 ⑧ 出口 ⑨ feedback。

## 術語對照表

寫文件一律用左欄。右欄是舊說法，**不要再寫**（PDCA 紀錄是當時的措辭，不回頭改）。

| 正式術語 | 舊說法 | 檢查腳本會擋 |
|---|---|---|
| 動態基準線學習 (Dynamic Baseline Learning) | 學平常的樣子、學「平常」、跟自己的歷史比 | |
| Pod 同儕橫向比對 (Peer Comparison) | 跟兄弟比、兄弟比、Pod 互比（程式裡的名字 `peer` 照舊） | 跟兄弟比 |
| 靜態臨界值規則 (Static Threshold Rules) | 規則（一次就算）、保底規則、固定門檻 | 保底規則 |
| 單點即判定 (Instant Anomaly) | 單次就報、一次就算（`confirm_points: 1`、`instant_z`） | |
| 例外吞噬檢測 (Exception Swallowing Detection) | 吞例外、吞掉的例外（規則 `swallowed`） | |
| 邊緣身份鑑別與標籤注入 (Edge Identity Sanitization & Injection) | 擋冒充、補身分 | |
| 集中式資料寫入憑證 (Shared Ingest Token) | 一把寫入 key | 一把寫入 |
| 資深維運專家回饋 (Expert Feedback) | 老師傅回饋 | |

- 「老師傅」是產品用語（賣點之一），使用者介面和對話可以用；正式文件第一次出現寫「資深維運專家（老師傅）」。「一件」（一件事故）、「值班」、「一把 key」（量詞）都是正常用語，不在表內。
- 檢查腳本只擋「檢查腳本會擋」欄的片語，其餘靠人看。要加新片語：先加進這張表，再加進 [tools/check-docs.py](tools/check-docs.py) 的 `FORBIDDEN`。

## 文件擺放

| 寫什麼 | 放哪裡 |
|---|---|
| 為什麼這樣設計、原理、方法的限制 | `docs/`（log、metrics、trace、rca、feedback） |
| 怎麼跑、參數、輸出在哪、怎麼驗 | 跟程式放一起：`engine/configs/<名>/README.md`、`engine/deploy/`、`e2e/<環境>/README.md`；原理用連結指回 `docs/`，不重寫 |
| 每次工作的紀錄 | `docs/pdca/phase<N>-…/`；不屬於某個 phase 的放 `docs/pdca/meta/`；更新 [docs/pdca/README.md](docs/pdca/README.md) |
| 構想、還沒做的 | 檔頭加一行 `> ⚪ 構想，尚未實作`；部分做了用 `> 🟡`；在 docs/README.md 標狀態 |

- 新增、刪除、改名 md：同一個 commit 更新 `docs/README.md`（PDCA 紀錄更新 `docs/pdca/README.md`）。
- 長文（超過約 300 行）在第一個 `##` 前放 `<!-- toc -->` 和 `<!-- /toc -->`，用 `python3 tools/check-docs.py --toc` 產生，不要手寫目錄。
- 提交前：`python3 tools/check-docs.py`（連結、索引、舊說法），改了程式再跑 `cd engine && python3 -m pytest -q`。

## 工作方式

- **新工作先開 branch、先寫 PDCA 計畫**，再動手。PDCA 四段：**Plan**（需求、目的、範圍）、**Do**（改了哪些程式、為什麼）、**Check**（測試的目的與結果，不寫「N 個測試通過」就算）、**Act**（沒解決的，在什麼情況下發現的）。範本 [docs/pdca/TEMPLATE.md](docs/pdca/TEMPLATE.md)。
- **只有被明確說「合回 master」才合進 master**；「好」不算。
- 告警一個服務一件；通知交給 Grafana；部門決定誰看得到。
