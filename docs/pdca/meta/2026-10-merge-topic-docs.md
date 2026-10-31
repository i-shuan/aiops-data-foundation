# metrics、log、rca、trace、feedback 各合成一份（2026-10）

**狀態**：完成　　**日期**：2026-10-31　　**相關**：分支 `claude/merge-topic-docs`（從 `claude/merge-architecture-docs` 開出，接在 [architecture 合併](2026-10-merge-architecture.md) 之後）
**模組**：跨階段

## Plan：這一次要達到什麼

- **需求**：`docs/` 的共用文件有 30 份，同一個主題被拆成原理／規格／操作／案例好幾份（metrics 9、log 4、rca 4、trace 4、feedback 2），要查一件事得在同一個資料夾翻好幾個檔；同樣的內容在幾份裡各寫一次（例如 ES 8.19 的改動要在 metrics 底下改好幾份）。
- **目的**：這 5 個資料夾各剩 `README.md` 一份，原本各份的內容依「原理、規格、操作、案例」放在同一份的不同章節；全 repo 指到被併掉的檔案的連結都指到新位置，`tools/check-docs.py` 0 個問題。
- **範圍**：改寫成每份 1500～2000 個漢字（使用者規定：不准形容詞過多、不准換句話說、不准流水帳），原本的檔案刪除，細節留在 git 歷史、程式旁的 README（`engine/configs/`）與 PDCA 紀錄。根目錄的 `architecture.md`、`aiops-data-reference.md`、`retention*.md`、`troubleshooting.md` 這次不動。`metrics` 與 `rca` 都講五種做法這類跨資料夾的重複，這次沒處理。

## Do：改動哪些 code、改了什麼、為何要改

沒有程式邏輯的改動；程式與設定檔只改註解裡的檔名。五份各由一個 agent 讀完該資料夾全部原稿後改寫，我逐份對照 `telemetry/`、`engine/` 的程式與設定修正。

| 資料夾 | 原本 | 現在 | 章節 |
|---|---|---|---|
| `docs/metrics/` | 9 份、776 行 | `README.md` 1503 字 | 資料怎麼走、關鍵決定與原因、欄位與資料結構、收了什麼與 baseline、能做到與做不到、.NET 與 Java、部署與驗證、現況與未做 |
| `docs/log/` | 4 份、511 行 | `README.md` 1503 字 | 資料怎麼走、關鍵決定與原因、規格、操作、現況與沒做的 |
| `docs/rca/` | 4 份、827 行 | `README.md` 1543 字 | 六步與 AI 的位置、三種資料怎麼串、動態基準線學習怎麼判斷、方法抓不到的、15 個故障案例、現況、graph RAG 構想 |
| `docs/trace/` | 4 份、326 行 | `README.md` 1546 字 | 資料怎麼走、關鍵決定與原因、欄位、跟 log 與 metrics 串、部署與驗證、六個場景、現況與沒做的 |
| `docs/feedback/` | 2 份、307 行 | `README.md` 1510 字 | 閉環怎麼走、為什麼用 LLMOps、紀律、LLM 何時叫與 token、現況與沒做的、細節去哪看 |

其他改動：
- 刪 18 份被併掉的 md；全 repo 指到它們的連結（含 PDCA 紀錄，只改目標）改指各資料夾的 `README.md`，找不到錨點的拿掉錨點（14 處）。
- 註解與文件裡寫到舊檔名的路徑（`docs/metrics/cases.md` 等）改成 `README.md`，共 31 個檔案；`CLAUDE.md` 只改那一句裡的 `docs/rca/baseline.md`。
- `docs/README.md` 索引：被併掉的 18 列拿掉，5 份的說明改成涵蓋的章節；「依類型找」改寫；開頭註明這五份含規格、操作、案例章節。
- 根目錄 `README.md` 的資料夾表改成一欄主文件。

類型標示都維持「原理 Explanation」。這五份含規格、操作、案例章節，與 `CLAUDE.md`「一份只做一種事」不同，是使用者的決定。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 長度符合 1500～2000 字 | 數 Unicode `U+4E00`～`U+9FFF` 的漢字 | 五份 1503、1503、1543、1546、1510，都在範圍內 |
| 連結沒斷、索引完整、沒有舊說法、類型標示合格 | `python3 tools/check-docs.py`（含 `--toc`） | 四項都是 0 個問題 |
| 文件寫的是現況，不是 ES 9 或舊設定 | 逐份對照 `common.yaml`、`sampler.yaml`、`cronjobs.yaml`、`series.yaml` 與 PDCA 紀錄 | 改掉 8 處：保留天數（`metrics-generic` 原始留 90 天、不降解析度）、CronJob 頻率（detect 是每分鐘不是每 30 秒）、寫入憑證（單一集中式 key，不是各 group 一把）、key 失效時的行為（`retry_on_status` 含 401，資料留在佇列，不是整批丟棄）、LLM 摘要（OpenAI 實跑 12 次）、事故時段排除（有，不是 ❌）、metrics baseline 條數（5 條，不是 7 條）、ES 剛起來的 401（已處理） |
| 沒有形容詞堆疊、換句話說、流水帳 | 掃 `非常、完整、強大、簡單、大量、有效、也就是說、首先、接著` | 只剩 6 處是描述事實的用法（例如「完整推理」「有效」指回饋立刻生效），沒有逐項處理 |

- **沒測到的**：沒有逐句對照原稿確認沒有講錯，只抽查數字與有爭議的敘述；agent 回報裡列的「不確定處」我處理了 8 項，其餘（例如 `backtest` 預設幾週、`queue_size` log 與 metrics 是否各自計算）照原稿。沒有在 GitHub 上看呈現。
- 刪掉的內容類型（都還在 git 歷史）：欄位逐項表、ES 文件範例 JSON、部署的指令與 YAML、逐案例的實測數字與過程、15 個故障案例的逐案例表、15 個案例的 baseline 對照、.NET 指標名稱對照與規則表、Kafka 三條串接路線、業務 ID 五步驟、23 項與 46 項驗證的逐項表、業界趨勢與名詞解釋。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 漢字 1500～2000 與資訊量互相拉扯：每份要塞下欄位名、路徑、指令，agent 為了同時達標把欄位表、指令、YAML 都砍了 | 三個 agent 的回報都提到「漢字佔比要 > 43%」 | 要查「某個欄位叫什麼」「部署指令怎麼打」，現在得看 `telemetry/`、`engine/configs/` 的程式與 README，或 git 歷史 | 欄位規格留在 `aiops-data-reference.md`、`engine/configs/*/reference.md`；若常有人找不到，把 log／metrics／trace 的欄位表補回各自 README 的規格章節 |
| `metrics` 的 .NET 指標名稱對照與規則名稱整張刪掉 | metrics agent 的回報 | .NET 8 與 9 規則分開這件事只剩一句 | 規則名稱在 `engine/configs/baseline/rules.yaml`；需要時補回一個短表 |
| 來源互相矛盾的說法靠我逐項對程式處理，沒處理的還有 | agent 回報的「不確定處」 | `backtest` 預設週數、`queue_size` 是否 log 與 metrics 各一份未確認 | 對 `engine/detector/cmd_backtest.py`、`common.yaml` 確認 |
| `rca/README.md` 的「15 個故障案例」只剩五種做法的歸納 | rca agent 的回報 | 逐案例「要看什麼訊號、哪種 baseline」不見了 | 原表在 git 歷史（`docs/rca/cases.md`）；`metrics` 與 `rca` 都講五種做法的重複這次沒處理 |
| 本機 `e2e/` 測試仍有 ES 9 的假設沒處理 | 見 [2026-10-e2e-es8-rerun](2026-10-e2e-es8-rerun.md) | 不在這次範圍 | 另案 |
