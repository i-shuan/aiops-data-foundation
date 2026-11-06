# 工廠（半導體）：用 ML／FDC 的異常與生產履歷跑 aiops-engine

> **類型：操作 How-To**　原理與對照見 [docs/semiconductor-design.md](../../../docs/semiconductor-design.md)・紀錄 [docs/pdca/meta/2026-11-factory-engine.md](../../../docs/pdca/meta/2026-11-factory-engine.md)・情境怎麼選、三個內建差在哪見 [shared/scenario/README.md](../../src/shared/scenario/README.md)・IT 的設定在 [configs/apm/](../apm/README.md)

同一份 engine、`factory` 情境（[shared/scenario/factory.yaml](../../src/shared/scenario/factory.yaml)、[detect/fab_ingest/](../../src/detect/fab_ingest/ingest.py)）、另一套設定、另外部署。工廠的異常由 ML 團隊的模型（異常跳點）和 FDC 鳴叫系統判，engine **不自己判 FDC 感測值**，從 ⑤ 異常總表開始接：
收斂成一件（同機台；後站的異常依 lot 履歷併進前站）→ 根因分析（慢在哪一站哪個 chamber、壞 lot 的共同點、機台 alarm、事故前的 PM／recipe 變更）→ 通知 → 老師傅回饋 → 誤報匯出給 ML 團隊。

**欄位名全是假設**（沒有 ML 輸出、FDC、MES、EAP 的樣本）：拿到樣本只改各 yaml 裡 `fields`／`keys` 的右邊，程式不動。

## 設定檔

| 檔 | 程式 | 做什麼 |
|---|---|---|
| [ingest.yaml](ingest.yaml) | `detector ingest`（[ingest.py](../../src/detect/fab_ingest/ingest.py)） | ML 的異常跳點、FDC 鳴叫 → `aiops-anomalies`：`keys` 是 equipment、chamber、recipe、step、lot、parameter；`series` 是 `fdc.<參數>`；`level` 是 `ml`／`fdc`；ML 的 strong／weak 對到 anomaly／warning；FDC 的 OOS 是 critical |
| [paths.yaml](paths.yaml) | `detector paths-merge`（[detect/trace/path_builder.py](../../src/detect/trace/path_builder.py)；只有 `factory` 情境有這個子命令） | MES 進出站＋S6F11 機台事件 → 一個 lot 一筆 `lot-paths-<group>-<日期>`，格式跟 `traces-merged-*` 一樣：一站一段（service 是機台、name 是 recipe、耗時是進站到出站、hold／scrap 算錯），站裡的 chamber 加工段掛在底下 |
| [rca.yaml](rca.yaml) | `investigator alert`／`investigate` | 索引（履歷 `lot-paths-*`、log `eqp-logs-*`）與時間尺度（`close_after` 2h、滑動窗 2h、`entry_lag` 12h）；`changes` 讓報告多一段「事故前的變更」。「對象是機台、API 是 recipe、前後站從履歷找、recipe 變更當換版、嚴重度依參數家族」是 plugin 給的，要改才寫進 `alerts` |
| [schedule.yaml](schedule.yaml) | `scheduler` | 每分鐘 `ingest`、`alert`；每 5 分鐘 `paths-merge`；每天 `prune`。沒有 train／detect／rules／peer／trace-* |

跑之前設環境變數 **`AIOPS_PLUGIN=factory`**（[shared/scenario/base.py](../../src/shared/scenario/base.py) 的 `load_scenario`）；chart 是 `--set plugin=factory`。沒有 MES 履歷、只有 ML／FDC 的異常時用 **`metrics_only`**（`configs/metrics_only/`；不合成履歷、不併前後站、報告只有異常／log／事故前的變更）。

## 要給 engine 什麼資料

全部是**讀**既有系統匯出到 ES 的索引；字串欄位要是 `keyword`（lot id、機台 id 要精確比對），訊息是 `text`。

| 索引（預設名） | 一列是什麼 | 必要欄位（可在 yaml 改名） | 誰給 |
|---|---|---|---|
| `factory-ml-anomalies-*` | ML 判出的一個異常跳點（一個 run 一個參數） | 時間、`run_id`、`equipment_id`、`chamber_id`、`recipe_id`、`lot_id`、`parameter`、`value`、`expected`、`score`、`grade`（strong／weak） | ML 團隊 |
| `factory-fdc-alarms-*` | FDC 一次鳴叫 | 時間、`alarm_id`、機台、chamber、recipe、lot、`parameter`、`value`、`limit`、`alarm_level`（OOC／OOS） | FDC 系統 |
| `mes-history-*` | lot 進出一站 | `lot_id`、`step_no`、`equipment_id`、`chamber_id`、`recipe_id`、`recipe_version`、`product_id`、`track_in`、`track_out`、`result`；選填 `operator`、`carrier_id`、`material_lot`、`dept` | MES |
| `eqp-events-*`（選填） | S6F11 一個機台事件（chamber 的 ProcessStart→ProcessEnd） | `lot_id`、`equipment_id`、`chamber_id`、`recipe_step`、`start_time`、`end_time`、`ceid` | EAP |
| `eqp-logs-*` | 機台 alarm（S5F1）、PM／維修紀錄、recipe 變更，一筆一列 | 時間、`equipment_id`、`level`、`event_type`（alarm／pm／recipe_change）、`message`；alarm 有 `lot_id` 才串得到 lot | EAP、CMMS、recipe 管理 |
| `eqp-master` | 一台機台一筆（主檔，沒有時間） | `equipment_id`、`area`、`dept`、`group` | 設備部門 |

**EPT（E116）不用**：它不帶 lot，串不出履歷；要用就當機台狀態的背景資訊另外看。

## 怎麼跑

### 假資料整條跑一遍（不用等廠方資料）

```bash
cd aiops-engine && ES_URL=http://localhost:19200 ES_AUTH=elastic:changeme-test-only python -m tests.factory_e2e
```

[tests/factory_e2e.py](../../tests/factory_e2e.py) 造 7 天、728 個 lot 的 MES 與 S6F11、兩個事故（ETCH-01 CH-B 壓力漂移連帶 METRO-01 量測偏；CVD-01 換 recipe 後震盪）寫進 `*-synth` 索引，跑 `paths-merge` → `ingest` → `alert` → `feedback add`／`export`，印出 15 項檢查。`--plugin metrics_only` 用同一批假資料但不給履歷（13 項：ETCH-01 仍 critical、報告只有異常／log／PM、METRO-01 自己開一件）。`--dump <資料夾>` 把假資料另存成 NDJSON，`--clean` 清掉。結果寫在共用的 `aiops-anomalies`（`source` 欄位是 ml／fdc）、`aiops-alerts`（service 是那 5 台機台）、`lot-paths-fab1-*`。

### 接真資料

1. ES 開一把 key：讀上表的索引，寫 `aiops-*`、`lot-paths-*`，加 cluster 權限 `manage_index_templates`（`paths-merge` 自己放 `lot-paths` 的 index template；沒權限就請管理員用 [detect/trace/path_builder.py](../../src/detect/trace/path_builder.py) 的 `PATHS_TEMPLATE` 放一次）。
2. 改三份 yaml 的欄位名、索引名；`ingest.yaml` 的 `status`／`severity` 對應表照 ML、FDC 的實際值填。
3. 部署：`helm upgrade --install aiops-factory aiops-engine/helmchart -n aiops-factory --set plugin=factory …`（跟 IT 分開的 namespace 與 ES；同一個 ES 要分 `aiops-*` 前綴的話還沒做，見 PDCA 的 Act）。
4. 離線先試：`tests/factory_e2e.py` 把假的 ML 異常、MES、事件寫進一個測試用的 ES，整條鏈（`ingest`、`paths-merge`、`alert`）跑一遍。

## 哪些功能在工廠版能用

| 功能 | 工廠版 | 說明 |
|---|---|---|
| ④ 判斷（M1～M3） | ❌ 不跑 | 異常由 ML、FDC 判；engine 只接（`ingest`） |
| ⑤ 異常總表 | ✅ | `keys` 換成機台、chamber、recipe、step、lot、parameter；`level` 是 ml／fdc；多了 `source`、`source_id`（來源的 run_id、alarm_id） |
| ⑥ 收斂 | ✅ | 同機台一件；後站（量測）的異常依履歷併進前站那件：優先併進「那件已經有這批 lot」的前站，否則最近且時間對得上的；`lots` 欄位記這件牽涉哪些 lot |
| ⑥ 嚴重度 | ✅ | FDC OOS → critical；幾個參數家族同時異常 → high／critical；recipe 變更後 30 分鐘內上限 warning；滑動窗 2 小時 |
| ⑦ M4 分段 | ✅ | 履歷取代 trace：哪一站、哪個 chamber 多花多久（壞 lot 跟一週前的 lot 比）；最慢的 lot 的甘特圖 |
| ⑦ 共同點 | ✅ | 壞 lot 都在哪個 chamber、哪版 recipe、哪批材料（`commonality.resource`／`attrs`）；兩個 chamber 的機台 `min_lift` 放寬到 1.5 |
| ⑦ log | ✅ | 機台 alarm 的新樣式、最慢 lot 的 alarm（依 lot id）、**事故前 12 小時的 PM／recipe 變更**（`changes`） |
| ⑦ 分類（M5） | ✅ 表換了 | 半導體分類：equipment、process_drift、recipe、material、metrology、pm_related、operator、other；要先有確認過的 case |
| ⑧ 通知 | ✅ | 同一份 Grafana；`dept` 來自機台主檔；Grafana 的回饋表單選項還是 IT 版的（見 Act） |
| ⑨ 回饋 | ⚠️ | 老師傅確認、誤報照常記；**調不到 ML 的門檻**，`detector feedback export --out fp.ndjson` 匯出（回饋＋異常的 run_id、對象、值、分數）給 ML 團隊重訓；`feedback suggest` 的放寬建議對 ml／fdc 的序列沒有意義 |
| chamber matching（M2）、RCA graph | ❌ 沒做 | 見 [semiconductor-design.md](../../../docs/semiconductor-design.md) 的順序 |

## 怎麼驗

- 單元測試（不連 ES）：`python -m pytest -q tests/test_ingest.py tests/test_path_merge.py tests/test_factory_alerts.py tests/test_feedback_export.py tests/test_plugin.py`。
- 整條：上面的 `tests.factory_e2e`，15 項全過才算（履歷一個 lot 一筆、32 筆異常全進、ETCH-01 critical、METRO-01 併進 ETCH-01、報告慢在 CH-B、共同點 CH-B、alarm 新樣式、事故前的 PM、CVD-01 換版 warning、weak 不開件、單點 warning 自己結束、匯出帶 run_id）。
