# 假資料

**這個資料夾的東西全是假的**，只是為了讓程式現在就能跑、測試有資料可用。名稱、日期都是編的（行事曆的日期都在 2099 年，不會影響真的訓練）。**正式環境要換成真的來源**，格式與換法見 [fake-data/interfaces.md](interfaces.md)。

| 檔案 | 假的是什麼 | 對應的接口 | 換成真的 |
|---|---|---|---|
| [owners.json](owners.json) | 服務歸哪個團隊（團隊名稱都有「【假】」） | `owners`（[chronic.yaml](../configs/baseline/chronic.yaml) 的 `owners_source`） | 改成 `http` provider，或換掉這個檔 |
| [calendar.json](calendar.json) | 一筆測試假日、一筆維護、一筆活動 | `calendar`（[calendar.yaml](../configs/baseline/calendar.yaml) 的 `sources`） | 真的假日用 `taiwan` provider（已啟用）；維護、活動寫 `calendar.yaml` 的 `calendar:` 或接 `http` |

**不在這裡的（不是假資料）**：門檻、天數、序列定義（`baseline/*.yaml`），是真的設定。
