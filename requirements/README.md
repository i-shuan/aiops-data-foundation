# requirements

每個需求一個資料夾，用**英文 kebab-case** 命名。流程定義在
[`.agents/shared/workflow.md`](../.agents/shared/workflow.md)。

```bash
cp -r requirements/_template requirements/add-export-to-excel
```

| 檔案 | 回答 |
| --- | --- |
| `1-intent.md` | 為什麼要做、解決誰的什麼問題、**不做什麼** |
| `2-spec.md` | 要做成什麼樣、影響到哪些既有行為 |
| `3-plan.md` | 怎麼做、會動到哪些檔案、風險與退路 |
| `4-acceptance.md` | 怎麼算完成 —— 可執行的指令加預期輸出 |

⚠️ 一次只往前一個階段，每份要人確認才寫下一份。一口氣四份生出來的話，
intent 還沒談定，plan 已經假設了一堆沒講好的事。

⚠️ 檔名用 `1-intent.md` 不是 `1. intent.md`。檔名裡有空格的話，`cd`、`cat`、
`git add` 都要加引號，而示範指令被照貼進終端機時會壞掉。
