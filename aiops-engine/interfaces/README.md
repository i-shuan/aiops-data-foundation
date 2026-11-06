# 介面：要別人提供的資料

程式只認**格式**，不管資料從哪來。**接口**（這個資料夾、`aiops/service/detect/providers.py`）是長期的；**假資料**（[fake-data/](../fake-data/README.md)）是現在拿來跑的範例，之後換成真的來源，程式不用改。

| 介面 | 內容 | 誰用 | provider | 假資料 |
|---|---|---|---|---|
| `owners` | 哪個服務歸哪個團隊 | `chronic`（慢性病清單附負責人） | `file`、`http` | [fake-data/owners.json](../fake-data/owners.json) |
| `calendar` | 國定假日、維護日、活動、補班日 | `train`、`detect`（不學、不判斷） | `file`、`http`、`taiwan` | [fake-data/calendar.json](../fake-data/calendar.json) |

設定在 [configs/baseline/chronic.yaml](../configs/baseline/chronic.yaml)（`owners_source`）和 [configs/baseline/calendar.yaml](../configs/baseline/calendar.yaml)（`sources`）。多個來源合在一起；某個失敗只印警告，不讓指令掛掉（這兩種都是輔助資訊）。

## provider

| provider | 設定 | 說明 |
|---|---|---|
| `file` | `path` | 讀本機 JSON |
| `http` | `url`、`headers`、`timeout`、`cache_ttl`、`cache_dir` | GET 一個 URL，回傳同樣格式的 JSON |
| `taiwan`（只有 calendar） | `years`、`url`、`cache_ttl`、`cache_dir` | 抓台灣的行事曆（[TaiwanCalendar](https://github.com/ruyut/TaiwanCalendar)，每年一個 JSON）。放假的日子（國定假日、補假、彈性放假、春節）和補班日各一筆；週六日不列（baseline 本來就依星期幾學）。`years` 不寫就是今年和明年，明年還沒公告（404）就略過 |

**快取**（`http`、`taiwan`）：`detect` 每 30 秒跑一次、每次都讀調整，不能每次打網路。所以抓到的結果存在 `cache_dir`（`taiwan` 預設 `.cache/calendar`，`http` 預設 `.cache/http`），`cache_ttl`（預設 `24h`）內不重抓；對方說沒有（404）也記下來，`cache_ttl` 內不再問。抓失敗時用上次的快取（不論多舊），都沒有就當作沒有，不讓指令掛掉。`cache_ttl: 0` 是每次都抓。

## owners 的格式

```json
{"owners": [
  {"match": {"service": "^mes-"}, "owner": "MES 團隊"},
  {"match": {"namespace": "^pay"}, "owner": "金流團隊"}
]}
```

- `match`：欄位可用別名（`service`、`namespace`、`pod`、`deployment`、`route`、`method`），值是正規表示式。
- 依序找，第一個符合的；順序在前的優先（直接寫在 chronic.yaml 的 `owners` 比來源優先）。
- 也可以直接回傳陣列（不包 `owners`）。

## calendar 的格式

```json
{"timezone": "Asia/Taipei", "calendar": [
  {"date": "2026-10-10", "name": "國慶日", "type": "holiday"},
  {"start": "2026-11-01T02:00", "end": "2026-11-01T06:00", "name": "機房維護", "type": "maintenance"},
  {"date": "2026-11-11", "name": "活動", "type": "event", "match": {"service": "^order"}, "detect": true}
]}
```

| 欄位 | 說明 |
|---|---|
| `date` | 一整天（當地時間） |
| `start`、`end` | 好幾天（含頭尾），或一段時間（可帶時間） |
| `name`、`type` | 記錄用：`holiday`、`makeup_workday`（補班日）、`maintenance`、`event` |
| `match`、`series` | 只影響哪些對象、序列（不寫就是全部） |
| `detect` | `false`（預設）：不學也不報；`true`：只是不學，照常判斷 |

- `timezone` 不寫就用 `train.timezone`。
- 每一筆變成一筆 `exclude_window`（[detector/adjust.py](../aiops/service/detect/adjustments.py)），`adjust list` 看得到。
- 也可以直接回傳陣列（不包 `calendar`）。

## 換成真的來源

1. 寫一個回傳上面格式的服務或檔案（例如從 CMDB 匯出負責人）。
2. 改設定：`owners_source` 加 `{provider: http, url: …}`，或 `calendar.yaml` 的 `sources` 加一筆；把 `fake-data` 那筆刪掉。
3. 程式不用改。要新的 provider（資料庫、其他 API）就在 `sources.py` 加一個分支。
