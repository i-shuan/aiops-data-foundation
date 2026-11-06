# AIOps baseline：怎麼跑、怎麼調

> **類型：操作 How-To**　同主題：[原理](../../../docs/rca/README.md)・[規格](reference.md)・[實測](verification.md)

這份只寫怎麼做：怎麼跑 train／detect／rules／peer／quality／chronic／trace、怎麼調整、怎麼驗證。為什麼這樣設計見 [rca/README.md](../../../docs/rca/README.md)，參數與欄位見 [reference.md](reference.md)，實測結果見 [verification.md](verification.md)。

**目錄**

<!-- toc -->
- [加一條要學的序列](#加一條要學的序列)
- [調靈敏度](#調靈敏度)
- [使用者、feedback 調整](#使用者feedback-調整)
- [trace 每一段的 baseline](#trace-每一段的-baseline)
- [規則：狀態變化、離極限多遠](#規則狀態變化離極限多遠)
- [Pod 同儕橫向比對：同一個服務的 Pod 互相比](#pod-同儕橫向比對同一個服務的-pod-互相比)
- [調整：哪些資料不學、從哪裡重新學、哪些時段放寬](#調整哪些資料不學從哪裡重新學哪些時段放寬)
- [慢性病清單、行事曆](#慢性病清單行事曆)
- [門檻怎麼校、誰來調](#門檻怎麼校誰來調)
- [怎麼跑](#怎麼跑)
- [驗證](#驗證)
<!-- /toc -->

## 加一條要學的序列

> 另見：[原理](../../../docs/rca/README.md)・[規格](reference.md#做什麼)

要加序列，在 `series.yaml` 加一段就好，不用改程式。

## 調靈敏度

| 調整 | 效果 | 代價 |
|---|---|---|
| `z` 調小（例如 3） | 小一點的變化也抓得到 | 誤報變多 |
| `z` 調大（例如 8） | 只抓很明顯的 | 可能漏掉 |
| `min_abs` 調大 | 小服務、小數字的告警變少 | 小服務真的出事可能漏掉 |

`detect` 的 `z` 是全部共用；`min_abs`、`direction` 每條序列各自設。先用預設跑兩、三週，依誤報和漏報再調。要知道漏了哪些，就需要事故紀錄。

## 使用者、feedback 調整

自動選的不一定合適（例如報表 API 本來就跳動大，30 秒太敏感）。可以針對某條序列、某些對象調**粒度、z、min_abs**：

## trace 每一段的 baseline

> 另見：[原理](../../../docs/rca/README.md)・[規格](reference.md#trace-每一段的-baseline)・[實測](verification.md#trace-每一段的-baseline)

```bash
python3 -m detector trace-collect --config configs/baseline/trace.yaml   # 第一次：補過去 7 天的統計（trace 只留 7 天）
python3 -m detector trace-train   --config configs/baseline/trace.yaml   # 每天
python3 -m detector trace-detect  --config configs/baseline/trace.yaml   # 每 5 分鐘（同時累積統計）
```

## 規則：狀態變化、離極限多遠

> 另見：[原理](../../../docs/rca/README.md)・[規格](reference.md#規則狀態變化離極限多遠)・[實測](verification.md#規則狀態變化離極限多遠)

```bash
python3 -m detector rules --config configs/baseline/rules.yaml      # 每分鐘
```

## Pod 同儕橫向比對：同一個服務的 Pod 互相比

> 另見：[原理](../../../docs/rca/README.md)・[規格](reference.md#pod-同儕橫向比對同一個服務的-pod-互相比)・[實測](verification.md#pod-同儕橫向比對同一個服務的-pod-互相比)

```bash
python3 -m detector peer --config configs/baseline/peer.yaml      # 每分鐘
```

## 調整：哪些資料不學、從哪裡重新學、哪些時段放寬

> 另見：[原理](../../../docs/rca/README.md)・[規格](reference.md#調整哪些資料不學從哪裡重新學哪些時段放寬)・[實測](verification.md#調整哪些資料不學從哪裡重新學哪些時段放寬)

```bash
python3 -m detector adjust add --kind exclude_window --match service=^mes-api$ \
    --start 2026-10-20T02:00:00+08:00 --end 2026-10-20T05:00:00+08:00 --reason "DB 鎖表事故"
python3 -m detector adjust add --kind reset_point --match service=^mes-api$ --at 2026-10-22T10:00:00+08:00 \
    --source cicd --status proposed --id mes-api-v2 --reason "v2 上線"
python3 -m detector adjust add --kind recurring_override --match service=^mes-report --series "log\..*" \
    --cron "0 20 * * *" --duration 4h --days-of-month -1 --z 8 --reason "月底報表批次"
python3 -m detector adjust list
python3 -m detector adjust approve --id mes-api-v2 --by bob
python3 -m detector adjust remove  --id mes-api-v2
```

例：mes 的錯誤率常誤報，只放寬 mes：

```bash
python3 -m detector tune --series http.error_rate --match service=^mes- --z 7 --reason "誤報多" --by 王小明
python3 -m detector tune --list                                 # 看現在生效的 override
python3 -m detector tune --series http.error_rate --match service=^mes- --remove   # 還原
```

- 寫進 `aiops-tuning`，下一次 `detect` 起 mes 用 `z: 7`，其他服務不變；`series.yaml` 的預設不動。
- 之後發現是某段事故污染了歷史，再用上面的 `adjust add --kind exclude_window`，隔天 02:00 的 `train` 重學。
- 套用順序與覆蓋規則見 [reference.md](reference.md#使用者feedback-調整)：較晚寫的寬鬆調整會蓋掉較早的精細調整，所以寫 `--match` 時範圍要準，改完用 `--list` 確認。
- 要知道某個對象**最終**用的是哪個值、被哪一筆定的（多筆都符合時 `--list` 看不出來）：

```bash
python3 -m detector tune --show --series http.error_rate --match service=mes-api
#   z = 7.0（來自 3f9c…（aiops-tuning 那筆的 _id）：http.error_rate service=^mes-）
#   min_abs = 0.02（series.yaml 預設）…
```

  `--match` 給對象的實際值（不是正規表示式）；值從 `detect` 用的同一個函式算，不會跟實際判斷兩套。

## 慢性病清單、行事曆

> 另見：[原理](../../../docs/rca/README.md)・[規格](reference.md#慢性病清單行事曆)・[實測](verification.md#慢性病清單行事曆)

```bash
python3 -m detector chronic --config configs/baseline/chronic.yaml   # 每週一次（例如週一早上）
```

## 門檻怎麼校、誰來調

- 門檻都是先估的。正式環境排程後先只寫入、不通知，跑 2～4 週，看每個檢查觸發幾次、哪些是真的，再由平台的人手動改 [quality.yaml](quality.yaml)。
- `quality` 不讀 `aiops-tuning`：不能用 `tune`、`adjust` 依對象調，回饋建議（`feedback suggest`）也不會動它。
- 只跟昨天比的檢查（`log_trace_id_drop`、`*_unassigned_share` 的增量、`*_cardinality` 的倍數）第一天沒有比較對象，不會報；要第一天就有用的設 `max_share` 這類絕對上限。`*_cardinality` 第一天只看上限 500 條和「一半以上像 ID」；昨天有資料、只是沒有這個服務的（新服務），倍數照樣從 0 算。

```bash
python3 -m detector quality --config configs/baseline/quality.yaml   # 每 10 分鐘
```

## 怎麼跑

```bash
cd engine                           # baseline、根因分析是同一個 Python 專案
pip install -r requirements.txt     # pandas、numpy、pyyaml、requests、celery、redis；pydantic（驗設定）、typer（命令列）、fastapi＋uvicorn（api）

export ES_URL=https://<es>:9200
export ES_API_KEY=<kibana-dev-tools.txt 第 3 段發的 engine key>   # 只能讀資料、寫 aiops-*
# export ES_CA=/path/to/ca.crt      # ES 用內部 CA 的話

python3 -m detector train  --config configs/baseline/series.yaml   # 每天跑一次（例如凌晨）
python3 -m detector detect --config configs/baseline/series.yaml --window 10m   # 每 1 分鐘跑一次，看最近 10 分鐘（重跑會覆蓋）
python3 -m detector tune   --series ... --match ... --resolution 5m   # 調整某些對象的粒度、門檻
python3 -m detector quality --config configs/baseline/quality.yaml  # 每 10 分鐘，資料品質檢查
python3 -m detector chronic --config configs/baseline/chronic.yaml  # 每週一次，慢性病清單
```

正式環境用 K8s 上一隻 pod 的排程器：[aiops-engine/README.md](../../README.md)（rules／detect／alert 每 1 分鐘、train 每天；排程表 `configs/schedule/schedule.yaml`）。單機的 crontab 例子：

```
15 3 * * *  cd /opt/aiops && python3 -m detector train  --config configs/baseline/series.yaml >> train.log 2>&1
* * * * *   cd /opt/aiops && python3 -m detector detect --config configs/baseline/series.yaml >> detect.log 2>&1
* * * * *   sleep 30; cd /opt/aiops && python3 -m detector detect --config configs/baseline/series.yaml >> detect.log 2>&1
# crontab 最細 1 分鐘：要每 30 秒就用兩行（一行延後 30 秒）；正式的排程表是每 1 分鐘 --window 10m
```

回測（拿過去某個時間點當「現在」）：`train --end 2026-10-01T00:00:00Z`、`detect --at 2026-10-01T10:00:00Z --window 1D`。

detect 只看**已經結束**的時間窗（再往前退 `lag` 30 秒）：還沒收齊的那一段筆數會偏低，看起來像驟降。重跑同一段時間不會重複寫（異常的 `_id` 由序列、維度、時間決定）。

## 驗證

> 另見：[實測](verification.md#驗證)

```bash
python3 -m unittest discover -s tests -t .              # 在 aiops 底下；單元測試，不需要 ES
ES_URL=... ES_AUTH=... python3 -m tests.synth_e2e      # 模擬資料，需要 ES
ES_URL=... ES_AUTH=... python3 -m tests.adaptive_e2e   # 自動選粒度、卜瓦松、借 baseline，需要 ES
ES_URL=... ES_AUTH=... python3 -m tests.fine_e2e       # 30 秒粒度、連續才算、tune，需要 ES
```
