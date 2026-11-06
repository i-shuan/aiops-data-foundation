# Feedback：回饋閉環與介面

> **類型：原理 Explanation**

## 閉環步驟

### 怎麼走

| 階段 | 做什麼 | 落在哪 |
|---|---|---|
| 判斷 | ④ 的結果全寫入異常總表，帶 `status`（warning／anomaly）、可信度、粒度、方法 | `aiops-anomalies` |
| 出口 | ⑥ 同服務合成一件，Grafana 依部門分送 | `aiops-alerts` |
| 紀錄 | 告警一開就建一筆 `auto`（帶指紋）；老師傅在回饋網頁確認後改 `confirmed`，存根因分類、處置、怎麼看出來的、查了哪裡 | `aiops-incidents` |
| 回饋 | Grafana 的回饋表單（後端是 engine 的 `api`；通知裡有連結）或指令；記哪一筆、為什麼 | `aiops-feedback` |
| 調整 | [`series.yaml`](../../aiops-engine/configs/baseline/series.yaml) 的 `overrides`，或 [`tune` 指令](../../aiops-engine/configs/baseline/README.md#使用者feedback-調整)（程式 [`cmd_baseline.py`](../../aiops-engine/src/detect/detect_service.py)、寫進哪個索引見 [`learn/store/models.py`](../../aiops-engine/src/learn/store/models.py)）；記原因、誰、何時 | `aiops-tuning`（欄位見 [`adjust.py`](../../aiops-engine/src/detect/series_ops/manual_adjust.py) 的 `TUNING_MAPPING`） |

### 回饋結果與用法

| 結果 | 用法 |
|---|---|
| 確認（根因對） | 寫進 `aiops-incidents`；告警分類只讀 `confirmed` 的（case 只用來投票說新的一件「像哪一類」，不給根因、處置）；同根因累積 3 次，產生規則草稿供人審閱 |
| 根因錯 | 記正確根因，列入評測集，改根因分析或 prompt 時要能答對 |
| 誤報 | 有人標就提調整建議（不累積）：特徵全部算完再查表，老師傅選的「為什麼不是問題」優先；經回測、人核准才生效 |
| 漏報 | 降 z，補序列或規則；在 Grafana 儀表板的「回報漏報」面板（`POST /api/missed`）或指令回報 |

### 怎麼判斷是同一個根因

同一個根因 = 同一個服務加同一個根因分類。程式不比對 `root_cause` 的文字，也不比對指紋。

- 分類是老師傅在表單選的 `root_cause_category`，九選一：`db`、`network`、`config_change`、`deploy`、`external`、`resource`、`bug`、`data`、`other`。
- 計入的事故：`aiops-incidents` 裡 `status` 是 `confirmed` 或 `corrected`、填了 `root_cause_category`、開始時間在最近 28 天內（`feedback suggest --days`，預設 28）。`auto`（沒人看過）和 `false_positive` 不計。
- 次數是事故件數，不是回饋筆數。一件事故涉及多個服務時，每個服務各算一次。
- 同一個服務與分類累積 3 件（`--min-recurring`，預設 3）就寫一筆 `rule_suggestion`（`aiops-tuning`，`status: proposed`）。同一組已經有建議就不重寫。
- 要看哪幾件被算在一起：建議裡的 `incident_ids`；理由欄寫「<服務> 的「<分類>」被確認 N 次」。
- 分類選得不同就分開算；選 `other` 的會把不同原因混在一起。核准前要看這幾件的 `root_cause` 文字，確認是同一個原因。
- 規則草稿的門檻是這幾件事故各自最低的延遲異常值取最小值；沒有延遲類異常就算不出門檻，不能核准。

`override`、`recurring_override` 的建議核准前必須跑 `backtest`：現行設定與「現行＋建議」各重跑過去 N 週。同一個對象的幾筆建議（例如 `min_abs`＋`min_count`）是一個方案，**一組一起測**（`backtest --id a --id b` 或 `--group`），整組有效才逐筆剔除，標出多餘的（核准多餘的會被擋）。結論四種：有效（告警減少、已確認事故沒漏）、無效（範圍內有異常但一筆沒少）、無法判斷（回測窗內沒有落在建議範圍的異常，例如每月最後一天的建議只回測 2 週）、有害（會漏掉已確認事故，整組退回）。沒回測的不能核准。

### 調整：調什麼、怎麼調、哪些能調

調整不改統計公式（168 個時段的中位數與 MAD），只改輸入與參數。調整寫進 `aiops-tuning`（也能寫在設定檔），`train`、`detect` 下一輪讀到就生效。`kind` 與來源的定義在 [adjust.py](../../aiops-engine/src/detect/series_ops/manual_adjust.py)，指令選項在 [__main__.py](../../aiops-engine/src/interface/cli/detect.py)，範例在 [baseline README](../../aiops-engine/configs/baseline/README.md#調整哪些資料不學從哪裡重新學哪些時段放寬)。

| 要調什麼 | 什麼情況 | 怎麼調 | `kind` |
|---|---|---|---|
| 某條序列、某些對象的 `z`、`min_abs`、粒度 `resolution`、`confirm_points`、`instant_z` | 誤報多就放寬，漏報就調低；API 本來就跳動大 | `python3 -m main detector tune --series … --match service=^mes- --z 7 --reason …`，或 [series.yaml](../../aiops-engine/configs/baseline/series.yaml) 的 `overrides` | `override` |
| 某段時間的資料不學 | 事故、維護、壓測 | `adjust add --kind exclude_window --start … --end …`；加 `--apply-to train,detect` 連這段也不報 | `exclude_window` |
| 某個時間點以前的歷史不學 | 換版、架構調整 | `adjust add --kind reset_point --at …`；之後 24 小時（`--warmup`）不判斷 | `reset_point` |
| 週期性時段放寬 | 月底報表批次 | `adjust add --kind recurring_override --cron "0 20 * * *" --duration 4h --days-of-month -1 --z 8`；`--mute` 這段不報 | `recurring_override` |
| 新增一條規則 | 同根因累積 3 次 | `feedback suggest` 產生草稿，核准後 `rules` 自動載入 | `rule_suggestion` |

- `--match` 的對象用正規表示式，欄位可用別名（`service`、`namespace`、`pod`）。
- 同一個對象有多處設定時，對象 > 序列 > `series.yaml`；來源有設定檔與 ES，同 `id` 後寫的蓋前面。
- 人用 `tune`、`adjust` 寫的預設直接生效。`feedback suggest` 寫的是 `status: proposed`，不生效。誤報的建議依特徵查表（[feedback/suggest.py](../../aiops-engine/src/learn/param_suggester.py) 的 `fp_features`、`suggest_for_false_positives`）：

  | 時段固定 | `z` 放寬壓得掉 | 動作 |
  |---|---|---|
  | 是 | 是 | 那 1 小時 `z` 乘 1.4 |
  | 是 | 否 | 那 1 小時不報、不學（`mute` + `train: exclude`） |
  | 否 | 是 | 整條 `z` 乘 1.4 |
  | 否 | 否 | `min_abs` 調到最大偏差 × 1.2 |

  老師傅在表單選的原因優先：換版 → `reset_point`；壓測 → `exclude_window`；批次 → 那 1 小時不報不學（1 筆也算得出時段）；本來就慢 → `min_abs`；量太少 → 再加一筆 `min_count`。漏報，`z` 乘 0.8。回測時告警一筆沒少就「無效，不建議」。
- 生效流程：`backtest --id <id> --group`（預設過去 4 週；`--group` 把同一對象其他 proposed 的建議拉進來一起測）→ `adjust approve --id <id> --by <誰>`。`override`、`recurring_override`、`rule_suggestion` 沒回測或回測不建議時，要加 `--force` 才能核准；其他 `kind` 不用回測。`rule_suggestion` 算不出門檻不能核准；它的回測看提這條規則的事故抓不抓得到、事故以外多報幾次。
- `--expires` 到期自動失效；`adjust remove --id <id>` 刪除。

不能用這些指令調的：
- 統計公式本身。
- [rules.yaml](../../aiops-engine/configs/baseline/rules.yaml) 現有規則的門檻：改 yaml；回饋只能新增 `rule_suggestion`。
- `quality`：不讀 `aiops-tuning`，改 [quality.yaml](../../aiops-engine/configs/baseline/quality.yaml)。

> 回饋表單送出後資料怎麼流、寫進哪幾個索引：[data-storage-reference.md](../data-storage-reference.md#回饋表單的資料流)。

## feedback 有哪些介面

![Grafana 回饋表單](grafana-form.png)

圖是外掛版（儀表板 `aiops-feedback-forms`）的回饋表單，網頁版欄位一樣：告警 id、這件、摘要、AI 摘要、過去相似的事故由系統預填；老師傅選「真的／誤報／真的，但根因報錯了」，填根因分類、根因、處置、怎麼看出來的、查了哪些地方、下次怎麼更快、署名與備註，按送出。

| 介面 | 在哪 | 寫哪個索引 |
|---|---|---|
| 回饋網頁（預設） | engine api 的 `GET /feedback?alert=<alert_id>&full=1`：先看根因全文、再填；Grafana 儀表板 `aiops-feedback`、通知的 `feedback` 連結都指到這裡；開著的件最上面有「重跑根因分析」（`POST /rca`） | `aiops-feedback`、`aiops-incidents`（重跑：`aiops-alerts` 的報告換新） |
| 漏報網頁（預設） | `GET /missed`、`POST /missed`：選序列、填服務、時間、署名；寫的文件、`_id` 跟指令 `feedback add --verdict missed` 一樣 | `aiops-feedback` |
| Grafana 外掛表單（選用） | 儀表板 `aiops-feedback-forms`（chart `plugin.enabled: true` 才載入），Business Forms 外掛 6.3.1；預填 `GET /api/alert`、送出 `POST /api/feedback`；漏報面板 `POST /api/missed`；重跑面板 `POST /api/rca`；要設 `API_CORS_ORIGIN` | 同上 |
| 指令 | `python3 -m main detector` 的 `feedback`（`add`、`pending`、`list`、`summary`、`suggest`）、`incident`（`add`、`list`、`resolve`）、`backtest`、`adjust`（`approve`）、`metrics`；漏報用 `feedback add --verdict missed --series … --match service=… --at …` | `aiops-feedback`、`aiops-incidents`、`aiops-tuning` |

### 跟 aiops-* 索引的欄位的關係

欄位的型別、誰寫、範例在 [aiops-data-reference.md](../aiops-data-reference.md)；這份只講介面寫到哪。

- 表單送出時，一件事故的每筆異常各寫一筆 `aiops-feedback`，同時更新 `aiops-incidents`：

  | 表單選項 | `aiops-feedback.verdict` | `aiops-incidents.status` |
  |---|---|---|
  | 真的 | `true_positive` | `confirmed` |
  | 誤報 | `false_positive` | `false_positive` |
  | 真的，但根因報錯了 | `wrong_root_cause` | `corrected` |

- 老師傅填的根因分類、根因、處置、怎麼看出來的、查了哪裡，存在 `aiops-incidents`；誤報不填這些，只填 `fp_reason`。
- 相似事故檢索只讀 `confirmed`、`corrected`。
- 建議（有人標誤報就提、同根因累積 3 次）由 `feedback suggest` 寫進 `aiops-tuning`，`status: proposed`；`backtest` 通過、人用 `adjust approve` 改成 `active` 才生效。
- 漏報沒有 `alert_id`、`anomaly_id`：Grafana 漏報面板（`POST /api/missed`）或 `feedback add` 寫一筆 `aiops-feedback`（`verdict: missed`、`series`、`keys.service`、`anomaly_at`），不碰 `aiops-incidents`；時間沒寫時區當 `train.timezone`（Asia/Taipei）。
