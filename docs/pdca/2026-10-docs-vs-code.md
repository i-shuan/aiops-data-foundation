# 文件對程式：整條線一份看完、狀態對齊（2026-10）

**狀態**：完成（只改文件）　　**日期**：2026-10-25　　**相關**：分支 `claude/flow-doc`；接在 [product-loop](2026-10-product-loop.md) 的整合驗證之後
**模組**：①～⑨（文件）

## Plan：這一次要達到什麼

- **需求**：PR #16 把 ⑥⑧⑨ 和 LLM 都接上了，但總覽文件（`architecture.md`、`system-stages.md`、根 README）還標 📝「還沒做」；baseline README 的「怎麼跑」寫 detect 每 30 秒，`deploy/` 是每 1 分鐘。第一次看的人會以為閉環沒做，或照錯的頻率排。另外沒有一份「從一個應用到 Grafana 告警」的流程，要跨五六份文件才拼得出來。
- **目的**：(1) 一份 md 從應用到告警把每步的「做什麼、程式在哪、多久跑、怎麼驗」排成一張表，加一個實際跑出來的例子；(2) 總覽文件的狀態跟程式一致；(3) 誠實列出不足與過度設計（含 `/code-review` 的 10 項對過程式後的判斷）。
- **範圍**：只改文件；程式的問題記下來，不在這一輪修。

## Do：改動哪些 code、改了什麼、為何要改

無 code 改動。

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `docs/end-to-end-flow.md`（新） | 整條線的圖、九步的表（做什麼／程式／頻率／怎麼驗）、一件事從頭到尾的時間線（loop_e2e 的輸出）、測試目的對照、文件對不上程式的清單、不足、過度設計 | 一份看完 |
| `docs/architecture.md` | 架構圖與模組表：⑥⑧⑨、LLM 從 📝 改成實際狀態（✅／⚠️），指到程式位置 | 跟程式一致 |
| `docs/system-stages.md` | 第 7 節標清楚四層收斂只做了②；第 8、9、10 節狀態；第 11 節狀態總表；建議順序改成下一步 | 同上 |
| `README.md` | 「最大缺口是 Check」改成「缺真實資料、真的 LLM、K8s」；閱讀順序加 end-to-end-flow | 同上 |
| `engine/configs/baseline/README.md` | detect 的頻率改成每 1 分鐘 `--window 10m`，指到 `deploy/` | 跟 CronJob 一致 |
| `engine/configs/rca/rca.yaml` | `rca:` 註解補「失敗不擋這一件；相似事故、LLM 也一樣」 | 註解漏了 |

## Check：測試的目的與結果

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 文件說的跟程式一樣 | 逐項對：架構表的每個 ✅ 找得到對應的檔案和測試；頻率對 `deploy/gen-cronjobs.py`；收斂四層對 `alerts.merge` | 對不上的 6 項列在 end-to-end-flow「文件跟程式對不上的」，這次改掉 5 項，舊事故匯入格式仍是「沒寫」 |
| 連結不斷 | 掃改過的 md 的相對連結 | 0 個壞連結 |
| `/code-review` 的 10 項是不是真的 | 對照程式：`cmd_alert.py` LLM 那段沒 try、`cmd_incident` 存 naive 時間、`_mget` 再 `bulk_index`、CronJob 110 秒 | 前 4 項確認會讓整輪失敗或資料被蓋；其餘 6 項對但影響小。都記在 end-to-end-flow「不足」第 2 點 |

## Act：Check 後沒改善的、還沒解決的

- **4 個程式問題還沒修**（在什麼情況下發現：code-review 後對程式）：alert 的 LLM 沒接 try、回測的時區、事故覆蓋的競態、alert CronJob 的時限。下一條分支修，各補單元測試。
- **過度設計那一表是判斷，不是量測**：砍不砍要等真實資料跑過（例如 trace baseline 到底有沒有抓到根因分析抓不到的東西）。
- `docs/rca/README.md`、`docs/feedback/README.md` 裡還有幾處「之後」「📝」的語氣沒逐句對，只改了總覽三份。
