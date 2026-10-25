# 異常總表帶上 dept、group（2026-10）

**狀態**：已完成（k3s e2e 待下次跑）　　**日期**：2026-10-25　　**相關**：分支 `claude/anomaly-owner`（從 `claude/split-detector-cli` 開）
**模組**：⑤ 異常總表（為 ⑧ 告警分送做準備）

## Plan：這一次要達到什麼

- **需求**：資料分部門、分資料流都做了（Collector 讀 namespace 的 `dept` label、算出 `group`，三種資料都帶），但 AIOps 算出異常寫回 `aiops-anomalies` 時只帶服務、namespace，**沒有部門**。告警要依部門分送（IT 值班只收 IT 的），沒有這個欄位就分不了。
- **目的**：每筆異常有 `dept`、`group`，可以 `terms group` 直接分；對不到的標 `unassigned`，跟 Collector 對沒有 dept 的處理一樣。
- **範圍**：只補欄位，不動通知、不動資料流。不加共同鑰匙（鑰匙是接三種資料用的，歸屬是分給誰用的，見 [rca/README.md](../rca/README.md#歸屬-label-和共同鑰匙是兩種東西)）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/owner.py`（新） | `load_owners`：每次執行查一次 metrics 最近 2 天的「namespace → dept、group」「服務 → dept、group」；`owner_of`：依異常 `keys` 對回去（namespace 優先，其次服務；keys 欄位名涵蓋 baseline／規則／peer／quality／trace 的各種寫法）；`write_anomalies`：補上 `dept`、`group` 再寫 | 不需要新的資料來源：資料本身就帶；namespace 優先因為 dept 是 namespace 的 label |
| `engine/detector/es.py` | `ANOMALY_MAPPING` 加 `dept`、`group`（keyword） | `ensure_index` 會把新欄位補進已存在的 index |
| `cmd_baseline.py`、`cmd_rules.py`、`cmd_peer.py`、`cmd_quality.py`、`cmd_trace.py` | 6 處 `es.bulk_index(ANOMALY_INDEX, docs)` 改 `write_anomalies(es, docs)` | 所有寫異常總表的地方都要有 |
| `engine/tests/test_owner.py`（新） | 9 個測試 | — |
| 文件 | `docs/rca/README.md` 加「歸屬 label 和共同鑰匙是兩種東西」與完整清單；`docs/architecture.md`、`engine/configs/baseline/README.md` | 回答「共同鑰匙圈是加這些 label 嗎」：不是，兩種東西 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 對照邏輯：namespace 優先、服務其次、找不到 unassigned | 單元測試 | namespace 與服務指到不同部門時以 namespace 為準；`caller`／`callee`（時鐘不同步）、`entry.service`、`service` 都對得到；只有 node 的規則、新服務、有紀錄但沒 dept 的都是 `(None, unassigned)`；沒有異常時不查 ES |
| TSDS 的 `resource.attributes.dept` 能不能做 terms 聚合（`load_owners` 靠它） | docker：假的 Java 應用，Collector 用正式 `node.yaml` 的 Java 改名＋一把 key，補上兩組身分（mes-api／mes-prod／D3120／it、wip-worker／wip-prod／D1110／imc）→ es-t | 聚合出 `mes-prod → D3120、it`，`wip-prod → D1110、imc`，並確認分別寫在 `…otel-it`、`…otel-imc` 的 data stream |
| 端對端：規則算出來的異常有沒有帶部門 | 跑 `detector rules`（錯誤率 5%、p95 2 秒保底）→ 查 `aiops-anomalies` | mes-api 的兩筆是 `D3120／it`，wip-worker 的是 `D1110／imc`；測試中先前留下的、沒有 dept 的兩個假服務是 `null／unassigned`（符合預期）。依 `group` 聚合：it 2、imc 2、unassigned 4 |
| 已存在的 index 補得上欄位 | 刪掉 index 後重建、以及既有 index 的 mapping | `dept`、`group` 都是 keyword |

**沒測到**：k3s 端對端；同一個服務跨多個 namespace（兩個部門都有 `order-api`）時，服務只對到最常見的那個，namespace 在 keys 裡的異常不受影響。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 服務重名跨部門時，只靠服務名的異常（log 的 `service`、trace 的 `entry.service`）會歸給其中一個部門 | 設計時 | 告警可能送錯部門 | 服務命名規則還沒定案（[pdca/README.md](README.md) 待辦）；先接受，重名時要在異常 keys 帶 namespace |
| 新上線、最近 2 天還沒資料的服務查不到部門，歸 `unassigned` | 設計時 | 新服務前兩天的告警進平台頻道 | 可接受；`OWNER_DAYS` 可調 |
| 以前寫的異常沒有 `dept`、`group` | 改欄位時 | 只影響這次之後的 | 不補（異常只看最近） |
| 每次執行多一個聚合查詢（metrics 最近 2 天） | 設計時 | 規則 1 分鐘跑一次，每次多一個查詢 | 沒量；若慢，改成快取在檔案或 `aiops-tuning` |

- **決定**：歸屬由寫入端補，不改查詢端；不新增共同鑰匙。
- **下一輪**：告警出口依 `group` 分送（[alert-exit](2026-10-alert-exit.md)）。
