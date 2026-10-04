# .agents — 給 AI 的原子化模組

每份 < 70 行，只放**約束與會壞掉的地方**。要看機制怎麼運作、為什麼這樣
設計，去各目錄的 `README.md`（根目錄、`app/eqp-collector/`、
`tests/ap1105-sim/`）。

⚠️ 兩邊**放的是不同東西**，不是同一份的長短版。寫成濃縮版的話一定會漂。

## shared/ —— 換專案可以直接複製

| | 放什麼 |
| --- | --- |
| [`shared/workflow.md`](shared/workflow.md) | 需求四階段、變更流程與收尾格式 |
| [`shared/writing.md`](shared/writing.md) | 寫文件的八條規則、警告該放哪、加檢查工具的注意事項 |

裡面不提任何專案專屬的指令、路徑或系統名稱。

## project/ —— 只在 aiops-data-foundation 成立

| | 放什麼 |
| --- | --- |
| [`project/commands.md`](project/commands.md) | 真實指令、技術棧、不要讀的目錄、這台機器的坑 |

目前只有這一份。分流、log、部署的約束還散在各 `README.md`，要抽成模組時
在 `AGENTS.md` 的「動手前讀哪個模組」補一列。

## 加一份新模組之前

先問：**它換到別的專案還成立嗎？** 成立就放 `shared/`，不成立放 `project/`。
混在一起的話，想把這套帶去新專案時得逐行挑。
