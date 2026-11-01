# open-issues 可平行做的七項（2026-11）

**狀態**：已完成（只有單元測試；e2e 與 Grafana 實載待叢集空了）　　**日期**：2026-11-01　　**相關**：分支 `claude/parallel-items`；來源 [meta/2026-10-open-issues.md](../meta/2026-10-open-issues.md) 第 2、5、7、9.2、9.3 節
**模組**：④ 判斷、⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：open-issues 累積了一批「範圍明確、不用叢集、不碰 trace 合併分支正在改的檔案」的小項目。HA 測試佔著 es-test 叢集，這些項目只靠單元測試就能驗，先做掉。
- **目的**：下面七項各有程式或文件的改動，單元測試可驗；e2e 等叢集空了再補跑。
- **範圍**：

| # | 項目 | 來源 | 主要改的檔案 |
|---|---|---|---|
| 1 | 文件：重建 ES 要列 `train` 為標準步驟；`detect` 窗口 10 分鐘的限制 | 第 2、7 節 | `aiops-engine/README.md` |
| 2 | B6a：`service.name` 等於 container 名稱算缺 | 9.2 | `configs/baseline/quality.yaml`、`detector/quality.py` |
| 3 | `tune --show`：看某條序列、某個對象最終生效的值與來源 | 第 5 節「想補」 | `detector/cmd_baseline.py`、`detector/model.py`、`detector/__main__.py` |
| 4 | 每筆異常記是哪一版產生的（`engine_version`） | 9.2 | `detector/es.py`、`detector/cmd_baseline.py`、`detector/owner.py`、`helmchart/templates/env.yaml` |
| 5 | 基準線不見的告警：`detect` 寫 `detect-status`，Grafana 規則送平台 | 第 7 節 | `detector/cmd_baseline.py`、`grafana/code/alert-rules.yaml` |
| 6 | 漏報的入口：整合進 Grafana 回饋儀表板，不另做網頁（使用者決定） | 9.3 | `feedback_web/app.py`、`feedback_web/__main__.py`、`grafana/code/dashboards/aiops-feedback.json` |
| 7 | code-review 三個殘項：metrics 回饋跨段、回測重複查 ES、相似事故 ES 前 20 筆截斷 | 9.3 | `detector/cmd_metrics.py`、`detector/cmd_backtest.py`、`investigator/similar.py` |

  不做：`sample_per_bucket`、ES chart 的 setup Job、snapshot、根因分析限制、retention 文件、Collector、Pod 同儕 A2（原因見 open-issues 的「先別平行」）。不跑 es-test 叢集的 e2e。不合回 master。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `aiops-engine/README.md` | 排程表 `detect` 列寫明窗口 10 分鐘是上限；新增「重建 ES、換 ES 之後」四步（手動 `train` 是第 3 步）；「AIOps 停了」加 `detect-status` 那段 | 第 2、7 節要的兩件文件；基準線不見的機制要有地方看 |
| `detector/quality.py`、`configs/baseline/quality.yaml` | `missing` 檢查加 `missing_if_equals: {欄位: 另一欄位}`：`missing:<欄位>` 的 filter agg 變成 `bool.should`（不存在，或 Painless 比對兩欄位相等）；`quality.log_missing_identity`、`quality.metrics_missing_service` 對 `service.name` 設成等於 `k8s.container.name` 也算缺 | Collector 沒 service label 時把 `service.name` 補成 container 名稱，欄位有值但對不到服務，原本的 `must_not exists` 看不到（B6a） |
| `detector/model.py`、`detector/cmd_baseline.py`、`detector/__main__.py` | `effective_for()` 回傳每個參數的生效值與來源（aiops-tuning 的 `_id`，或 `series.yaml overrides#n`）；`score_params()` 抽出 z／min_abs 的取值，`score_tuned` 與新的 `effective_lines()` 共用；`tune --show --series --match` 印每行 | 多筆都符合時後面蓋前面，沒有指令看「現在用的 z 是哪一筆定的」。值從 `detect` 用的同一個函式拿，`--show` 不會跟實際判斷兩套 |
| `core/version.py`（新）、`detector/owner.py`、`detector/es.py`、`helmchart/templates/env.yaml` | `ENGINE_VERSION`（環境變數，沒設是 `dev`）；`write_anomalies` 每筆補 `engine_version`；mapping 加 keyword 欄位；chart 的 ConfigMap 給 `image.tag` | 比較兩版偵測結果不用靠時間推。baseline、規則、peer、quality、trace 全經 `write_anomalies`，一處補就每筆都有；既有 index 由每輪的 `ensure_index` PUT mapping 補欄位 |
| `detector/cmd_baseline.py`、`investigator/cmd_alert.py`、`grafana/code/alert-rules.yaml` | `cmd_detect` 收集沒 baseline 的序列名，每輪寫 `_id: detect-status` 進 `aiops-alerts`（形狀同心跳，`status: ok`、`category: status`、`missing_baseline: N`、`series: […]`）；mapping 加 `missing_baseline`；Grafana 規則 `aiops-baseline-missing`：`missing_baseline > 0` 持續 5 分鐘送平台，`noDataState: OK` | `aiops-baseline` 掉了 `detect` 只印一行就跳過，心跳照寫，原本兩條規則都不響。`for: 5m` 讓剛部署、ES 剛重建還沒 `train` 的前幾輪不響；真的掉了一天才重訓，5 分鐘後一定響 |
| `feedback_web/app.py`、`feedback_web/__main__.py`、`grafana/code/dashboards/aiops-feedback.json` | `submit_missed()`：驗 series（從 `configs/baseline/series.yaml` 讀名字）、service、at（沒時區當 Asia/Taipei，走 pandas 跟 CLI 同一條路）；文件與 `_id` 跟 `feedback add --verdict missed` 完全一樣；`GET /api/missed-form`、`POST /api/missed`；儀表板加第二個表單面板（series 下拉、service、`datetime`、route／pod 選填、by、note） | 漏報只能用指令。使用者決定整合進 Grafana、不另做網頁：入口只有一個，之後 Grafana 帳號可以帶 `by`。`_id` 一樣才能跟 CLI 互相去重、`feedback suggest` 才對得上 |
| `detector/cmd_metrics.py` | `load_feedback()`：回饋改用這段告警的 `alert_id`（`terms`，每 1000 個一批）載入；漏報（沒有 `alert_id`）仍依自己的 `@timestamp` | 原本回饋依自己的時間篩：段結束後才給的回饋漏算（有回饋的比例被低估），段內標前一段告警的回饋混進 `reviewers`、`missed` |
| `detector/cmd_backtest.py` | `_CachedES` 代理：`search` 與含 `_search` 的 GET／POST `req` 依 (method, path, body) 快取，寫入直接轉送；`run_backtest` 內用它 | before／after 兩套設定讀同一批原始資料，只有門檻不同；原本每天每序列查 ES 兩次 |
| `investigator/similar.py` | 候選 20 → `CANDIDATES = 200`，`query`／`search` 加 `size` 參數 | ES 的 `_score` 跟 `explain` 的算法不同（`series` 本地是 min(交集,3) 倍，ES 只算一次 boost），排 ES 第 21 名以後的可能本地分數最高，20 件會截掉 |
| `configs/baseline/README.md`、`reference.md`、`docs/feedback/README.md`、`docs/aiops-data-reference.md`、`grafana/README.md`、`docs/architecture.md`、open-issues | `tune --show` 例子與套用規則、`missing_if_equals`、`engine_version`、`detect-status` 範例與第三條規則、漏報面板與 API、open-issues 八列改「已做（分支）」 | 跟著程式 |

## Check：測試的目的與結果

全部只有單元測試與 `helm template`，沒有連 ES、沒有載進 Grafana（es-test 叢集被 HA 測試佔著）。`cd aiops-engine/code && PYTHONUTF8=1 py -m pytest -q`：404 個通過；`tools/check-docs.py` 四項 0 個問題。

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| B6a：沒設選項的欄位行為不變；設了的欄位 agg 仍只有一個 `filter`，`should` 恰兩項（`must_not exists`＋`script`，params 是兩個欄位名） | `test_quality.py::MissingQuery`，假 ES 記 search body | 兩項都符合；同一檢查裡沒列進選項的 `k8s.pod.name` 仍是 `must_not exists` |
| yaml 的兩個檢查真的帶了選項、選項的欄位都在 `fields` 內 | `test_quality.py::Config` 載 `quality.yaml` | 符合 |
| `effective_for`：兩筆重疊（series.yaml 第 1 筆 z=6＋resolution，aiops-tuning `t1` z=7＋min_abs）→ z 的值與來源換成 `t1`，resolution 來源仍是 `series.yaml overrides#1`；值等於 `override_for` | `test_model.py::EffectiveForTest` | 符合 |
| `tune --show` 印的值等於 `score_params`／`status_params` 算的；來源寫對；缺參數或序列不存在回 2 | `test_tune_show.py`，假 ES | `z = 7.0（來自 t-report：…）`、`min_abs = 0.2（series.yaml 預設）`、只被設定檔命中的印 `overrides#1`；回 2 |
| 每筆異常都有 `engine_version`；mapping 有該欄位；chart 的 ConfigMap `ENGINE_VERSION` 等於 `image.tag`，`--set image.tag=0.3.1` 跟著變 | `test_owner.py`、`test_version.py`、`test_deploy.py`（跑 `helm template`） | 符合；`helm template` 成功，預設出 `"0.1.0"` |
| `detect-status`：兩條序列一條沒 baseline → `missing_baseline: 1`、`series: ["b.mem"]`，形狀同心跳；都有也寫一筆 `0`；有 `ensure_index(aiops-alerts)` | `test_detect_status.py`，monkeypatch `cmd_baseline.ES` | 符合 |
| `detect-status` 不會被當成一件：`_open_alerts` 只查 `status: open`、儀表板查 `category:service`、`cmd_metrics` 過濾 `category == "service"` | 讀程式 | 三處都抓不到 `status: ok`／`category: status` |
| 漏報：naive 時間變 `+08:00`、`_id` 等於 CLI 公式、`by` 空 → `anonymous`、空 pod 不進 keys；空／未知 series、空 service、看不懂的 at 丟 `ValueError` 且什麼都沒寫 | `test_feedback_web.py::SubmitMissed` | 符合 |
| HTTP：`/api/missed-form` 回一層 JSON；壞 series 400；好的 200 且有寫入；ES 丟例外 503 含「沒有存成功」 | `test_feedback_web_http.py` | 符合 |
| 儀表板第二個面板：元素 id 集合等於 `/api/missed-form` 的 key；series 選項等於 `series.yaml` 的名字；URL 結尾正確；plugin 版本同面板 1 | `test_grafana_form.py::MissedPanel` | 符合 |
| metrics：回饋查詢是 `terms alert_id`＝載入的告警 id、沒有時間 range；漏報仍依時間且 `must_not exists alert_id`；段結束後 3 天才標的回饋算進 reviewed；1001 件 → 兩個 terms（1000＋1）；沒告警時不送空 terms | `test_metrics.py::LoadPeriod`，假 ES 記 body | 符合（alerts 2、reviewed 1、fp 1、missed 1） |
| 回測快取：同 body（key 順序不同亦同）只打一次 ES；PUT、`_update`、DELETE、`bulk_index` 全部直通；`run_backtest` 兩套設定 × 8 天共 16 次 `detect_series` 拿到的是同一個代理 | `test_backtest.py::CachedES` | 符合 |
| 相似事故：25 件只同服務（3 分，低於門檻）排前面、一模一樣的排第 26 → 預設找得到且 `size == 200`；同資料 `size=20` 回空 | `test_similar.py`，假 ES 依 `size` 切 | 符合，證明舊的 20 件會截掉 |

沒測到的：Painless 腳本沒對真 ES 跑；`aiops-baseline-missing` 規則與漏報面板沒載進 Grafana；e2e B6a、quality、feedback 流程沒跑；pandas 這台原本沒裝，子代理 `pip install pandas` 後才跑得動（requirements.txt 內已有）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [⑧] 漏報面板的 `datetime` 元素型別是照 Business Forms 6.x 的名字寫的，沒在真 Grafana 載過 | 寫面板 JSON 時 repo 內沒有外掛原始碼可比對 | 型別名不對面板會少一個欄位 | 部署到測試 Grafana 時看一次 |
| [④] `effective_for` 對 `adjustments.yaml` 裡沒寫 `id` 的 override 會跟 `series.yaml` 的一起編號成 `overrides#n` | `adjust.load` 不標來源、`adjust.py` 不在這次範圍 | 來源顯示不精確；實務上 `adjustments.yaml` 幾乎只放排除時段與重置點 | 要精確時在 `adjust.load` 給設定檔的每筆標 `source` |
| [④] `tune --show` 的粒度只能說「有 override 就是它，沒有就是序列寫死的或自動選」 | 粒度在 `train` 時套，實際值在 `aiops-baseline` 的 `resolution` | 看不到自動選的結果 | 之後 `--show` 可以順便讀 `aiops-baseline` |
| [⑧] `aiops-baseline-missing` 首次部署、ES 重建後 5 分鐘內沒跑 `train` 一定響一次 | 設計時刻意 | 一次告警 | 部署流程把手動 `train` 排在 5 分鐘內（README 已寫） |
| [⑨] 漏報的 series 下拉是靜態列表，新增序列要同步改面板 | 面板選項不能從 `series.yaml` 動態讀 | 漏改時 `test_grafana_form.py` 會擋 | 照測試提示改 |
| [⑨] 漏報查詢假設漏報一律沒有 `alert_id` | 寫 `load_feedback` 的漏報那一路 | 若日後漏報帶 `alert_id`，會走 terms 那一路，不會重複算 | 不處理 |

- **決定**：漏報不另做網頁，整合進 Grafana 回饋儀表板（使用者決定）。`CANDIDATES = 200`、`for: 5m`、`missing_if_equals` 用 Painless 都是先估的做法，等真環境再校。
- **下一輪**：叢集空了跑 `run-quality`／B6a、feedback、schedule 相關 e2e；把面板與第三條規則載進測試 Grafana 看一次。
