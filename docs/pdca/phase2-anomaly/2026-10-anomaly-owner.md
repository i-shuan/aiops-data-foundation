# 異常總表帶上 dept、group（2026-10）

**狀態**：已完成（k3s e2e 待下次跑）　　**日期**：2026-10-25　　**相關**：分支 `claude/anomaly-owner`（從 `claude/split-detector-cli` 開）
**模組**：⑤ 異常總表（為 ⑧ 告警分送做準備）

## Plan：這一次要達到什麼

- **需求**：三種資料都已帶 `dept`、`group`（Collector 讀 namespace 的 `dept` label 算出），但寫回 `aiops-anomalies` 的異常只帶服務、namespace，沒有部門。告警要依部門分送（IT 值班只收 IT 的），沒有這個欄位就分不了。
- **目的**：每筆異常有 `dept`、`group`，可以 `terms group` 直接分；對不到的標 `unassigned`（跟 Collector 處理沒有 dept 的一樣）。
- **範圍**：只補欄位，不動通知、不動資料流。
- 不加共同鑰匙：鑰匙是接三種資料用的，歸屬是分給誰用的，見 [rca/README.md](../../rca/README.md#歸屬-label-和共同鑰匙是兩種東西)。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/owner.py`（新） | `load_owners`、`owner_of`、`write_anomalies` | 歸屬由寫入端補；不需要新資料來源 |
| `engine/detector/es.py` | `ANOMALY_MAPPING` 加 `dept`、`group`（keyword） | `ensure_index` 會把新欄位補進已存在的 index |
| `cmd_baseline.py`、`cmd_rules.py`、`cmd_peer.py`、`cmd_quality.py`、`cmd_trace.py` | 6 處 `es.bulk_index(ANOMALY_INDEX, docs)` 改 `write_anomalies(es, docs)` | 所有寫異常總表的地方都要有 |
| `engine/tests/test_owner.py`（新） | 9 個測試 | — |
| `docs/rca/README.md` | 加「歸屬 label 和共同鑰匙是兩種東西」與完整清單 | 回答「共同鑰匙圈是加這些 label 嗎」：不是 |
| `docs/architecture.md`、`engine/configs/baseline/README.md` | 文件 | — |

`owner.py` 的做法：

- `load_owners`：每次執行查一次 metrics 最近 2 天，得到「namespace → dept、group」與「服務 → dept、group」。
- `owner_of`：依異常 `keys` 對回去，namespace 優先、其次服務；namespace 優先因為 dept 是 namespace 的 label。
- `keys` 欄位名涵蓋 baseline／規則／peer／quality／trace 的各種寫法。
- `write_anomalies`：補上 `dept`、`group` 再寫。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| namespace 優先 | 單元測試：namespace 與服務指到不同部門 | 以 namespace 為準 |
| 各種 keys 欄位名都對得到 | 單元測試 | `caller`／`callee`（時鐘不同步）、`entry.service`、`service` 都對得到 |
| 找不到的標 unassigned | 單元測試：只有 node 的規則、新服務、有紀錄沒 dept | 都是 `(None, unassigned)` |
| 沒有異常時不多查 | 單元測試 | 不查 ES |
| TSDS 的 `resource.attributes.dept` 能做 terms 聚合（`load_owners` 靠它） | docker：假 Java 應用＋正式 `node.yaml`，補兩組身分 → es-t | `mes-prod → D3120、it`，`wip-prod → D1110、imc` |
| 兩組身分寫到各自的 data stream | 同上 | 分別在 `…otel-it`、`…otel-imc` |
| 規則算出的異常有沒有帶部門 | 跑 `detector rules`（錯誤率 5%、p95 2 秒保底）→ 查 `aiops-anomalies` | mes-api 兩筆 `D3120／it`；wip-worker 的 `D1110／imc` |
| 沒有 dept 的服務 | 同上：先前留下的兩個假服務 | `null／unassigned`（符合預期） |
| 依 `group` 聚合 | 同上 | it 2、imc 2、unassigned 4 |
| 新欄位的 mapping | 刪掉 index 重建；既有 index | `dept`、`group` 都是 keyword |

- docker 測試的兩組身分：mes-api／mes-prod／D3120／it、wip-worker／wip-prod／D1110／imc；Collector 用 `node.yaml` 的 Java 改名＋一把 key。
- **沒測到**：k3s 端對端。
- **沒測到**：同一個服務跨多個 namespace（兩個部門都有 `order-api`）：服務只對到最常見的那個，namespace 在 keys 裡的異常不受影響。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 服務重名跨部門時，只靠服務名的異常歸給其中一個部門 | 設計時 | 告警可能送錯部門 | 先接受；重名時異常 keys 要帶 namespace |
| 新上線、最近 2 天沒資料的服務歸 `unassigned` | 設計時 | 新服務前兩天的告警進平台頻道 | 可接受；`OWNER_DAYS` 可調 |
| 以前寫的異常沒有 `dept`、`group` | 改欄位時 | 只影響這次之後的 | 不補（異常只看最近） |
| 每次執行多一個聚合查詢（metrics 最近 2 天） | 設計時 | 規則 1 分鐘跑一次，每次多一個查詢；沒量 | 若慢，快取在檔案或 `aiops-tuning` |

- 只靠服務名的異常：log 的 `service`、trace 的 `entry.service`。
- 服務命名規則還沒定案，見 [pdca/README.md](../README.md) 待辦。
- **決定**：歸屬由寫入端補，不改查詢端；不新增共同鑰匙。
- **下一輪**：告警出口依 `group` 分送（[alert-exit](../phase3-locate/2026-10-alert-exit.md)）。
