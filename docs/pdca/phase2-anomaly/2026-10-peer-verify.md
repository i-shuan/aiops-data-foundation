# Pod 同儕橫向比對：副本數與狀態的邊緣情境（2026-10）

**狀態**：已完成　　**日期**：2026-10-28　　**相關**：分支 `worktree-agent-a3bdc0eaa8b708a02`（從 `14a12ed` 開）
**模組**：④ 判斷

## Plan：這一次要達到什麼

- **需求**：Pod 同儕橫向比對（`detector peer`）只在「3 個副本、1 個壞」驗過。副本數不同、Pod 換掉、擴縮容、全部一起壞時，會不會報錯 Pod、漏報、或把正常的 Pod 報出來，沒有人知道。
- **目的**：每個邊緣情境先寫預期，再用合成資料跑真的 ES、真的 `detector peer` 對答案；不符的修掉並補測試，門檻選擇的問題寫進 Act。挑合成資料測不準的，在 cluster 上用真的 .NET 應用確認。
- **範圍**：只測 `peer.yaml` 的 4 個檢查（錯誤率、p95、CPU、記憶體）和 `cmd_peer` 的篩選（fresh、min_volume、min_peers）。不改 Collector、不改分組欄位、不做告警收斂。

### 每個情境的預期

| 情境 | 預期 |
|---|---|
| 3 個 Pod 正常（本來就有點差） | 都不報 |
| 3 個 Pod 1 個壞 | 只報壞的，4 個檢查都報 |
| 只有 2 個 Pod、1 個壞 | 都不報：分不出誰怪（`min_peers: 3`），交給規則 |
| 只有 1 個 Pod | 不判斷 |
| 3 個全壞、程度差不多 | 都不報 |
| 3 個全壞、其中一個更嚴重 | 不能只報一個（其他也壞，交給規則） |
| 3 個壞 2 個 | 都不報（其他 Pod 的中位數本身就壞） |
| 5 個壞 2 個 | 2 個都報 |
| 兩個 Pod 各有小問題（低於門檻） | 都不報 |
| 兩個 Pod 各有不同問題 | 各報各的檢查，不互相干擾 |
| 流量很少的 Pod（10 分鐘 <20 請求）全錯 | 不比錯誤率、延遲 |
| 新 Pod 剛起來（暖機） | 暖機中不報；一起來就壞的，暖機後要抓到 |
| Pod 被換掉（名字變） | 換掉前報舊的；舊名字在 fresh 2 分鐘後消失；新 Pod 不報 |
| 擴縮容 2→5→2 | 正常時任何時間點都不報；有壞的，5 個穩定時抓到、2 個時不比 |
| 同名服務在兩個 namespace | 分開比，不混 |
| `service.name` 是 Pod label：同 label 兩個 Deployment | HTTP 依 label 一起比；CPU、記憶體依 Deployment 分開 |

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/cmd_peer.py` | 新增 `warmup`：window 開頭到 `end - warmup` 之間沒有資料的 Pod 不比；印出幾個暖機中 | 新 Pod 啟動時 CPU 高被報；擴容時新 Pod 記憶體低，把舊 Pod 報成記憶體高 |
| 同上 | `warmup` 不比 window 短就停下 | 那樣所有 Pod 都不比，等於關掉 |
| `engine/detector/peer.py` | `compare` 多 `max_peer_median`：其他 Pod 的中位數到這個值，不報 | 全部都壞、一個更嚴重時只報那一個，看起來像只有它壞 |
| `engine/configs/baseline/peer.yaml` | `warmup: 5m`；錯誤率 `max_peer_median: 0.05`、p95 `2`（秒） | 跟規則 `http.error_rate_floor`、`http.latency_p95_floor` 同門檻：整個服務壞了由規則報 |
| `engine/tests/test_peer.py` | 全壞不均、多數壞；`cmd_peer` 的 fresh／warmup 篩選（假 fetch） | 不需要 ES 也能擋回歸 |
| `engine/tests/peer_edge_e2e.py` | 新增：19 個情境的合成 metrics 寫進真的 ES，在 14 個時間點跑 `detector peer` 對答案 | 邊緣情境的主要驗證；`PEER_CONFIG` 可換設定比改前改後 |
| `e2e/local-k3s/run-peer-edge-e2e.sh` | 新增：真的 .NET 3 副本，正常、Pod 換掉＋暖機、只剩 2 個 | 合成資料測不準啟動行為 |
| `engine/configs/baseline/README.md` | 同儕比對一節加暖機、全壞不挑單一個 | 對齊程式 |

## Check：測試的目的與結果

### 合成資料（自己的 ES 9.5.3，`engine/tests/peer_edge_e2e.py`）

- **怎麼測**：19 個情境各一個 namespace，每個 Pod 每 30 秒寫一筆 HTTP 直方圖（200／500 各一筆）、一筆 kubeletstats，走 `elasticsearch-setup.sh` 裝的 index 結構；在 14 個時間點跑 `detector peer --at`。
- **改前**＝`14a12ed` 的 `peer.yaml`（`PEER_CONFIG` 指過去）；**改後**＝這次的設定。同一支腳本、同一組預期。
- 結果：改前 37 項 **8 項 FAIL**，改後 **37 項全 PASS**（2026-10-28 21:58 跑）。

| 情境 | 預期 | 改前實際 | 改後實際 |
|---|---|---|---|
| 3 個正常（p95 45～60ms、CPU 0.18～0.24） | 不報 | 不報 | 不報 |
| 3 個 1 個壞 | 只報壞的，4 個檢查 | 只報 b：30%、2000ms、1.00 核、600MB | 同左 |
| 只有 2 個、1 個壞 | 不報 | 不報 | 不報 |
| 只有 1 個 | 不判斷 | 不報 | 不報 |
| 3 個全壞、程度差不多 | 不報 | 不報 | 不報 |
| 3 個全壞、一個更嚴重（40%、45%、100%） | 不只報一個 | **只報 c**（100% vs 42%、8000ms vs 2550ms） | 不報 |
| 3 個壞 2 個（30%、30%、0%） | 不報 | 不報 | 不報 |
| 5 個壞 2 個 | 報 2 個 | 報 b、d（30% vs 0%） | 同左 |
| 兩個各有小問題（錯誤 +3 點、慢 1.4 倍） | 不報 | 不報 | 不報 |
| 兩個各有不同問題（錯誤 20%、慢 800ms） | 各報各的 | a 只在錯誤率、b 只在 p95 | 同左 |
| 流量很少（10 分鐘 10 個請求、全錯） | 不比 | 不報 | 不報 |
| 新 Pod 起來 1 分鐘（CPU 1 核） | 不報 | **報新 Pod CPU**（1.00 vs 0.20 核） | 不報 |
| 新 Pod 起來 3 分鐘 | 不報 | **報新 Pod CPU**（0.73 vs 0.20 核） | 不報 |
| 新 Pod 起來 8 分鐘（已正常） | 不報 | 不報 | 不報 |
| 新 Pod 一起來就壞（錯誤 50%），8 分鐘 | 報新 Pod | 報 | 報 |
| Pod 換掉前 | 報舊的 b | 報 b（錯誤率、p95、CPU） | 同左 |
| 換掉 1 分鐘 | 可報 b、不可報新 d | 報 b，**也報 d 的 CPU** | 只報 b（還在 fresh 內） |
| 換掉 3 分鐘 | 不報 | **報 d 的 CPU**（0.73 核） | 不報 |
| 換掉 8 分鐘 | 不報 | 不報 | 不報 |
| 擴容 2→5 後 1 分鐘、3 分鐘（新 Pod 記憶體 200→600MB） | 不報 | **報舊 a、b 記憶體**（600 vs 240、400MB） | 不報 |
| 擴容 6 分鐘、5 個穩定、縮回 1 分鐘、3 分鐘 | 不報 | 不報 | 不報 |
| 擴縮容、a 錯誤 30%：2 個時 | 不報 | 不報 | 不報 |
| 同上：5 個穩定（擴容 6 分鐘、15 分鐘） | 報 a | 報 a（30% vs 0%） | 同左 |
| 同名服務在兩個 namespace（p95 50ms vs 800ms） | 不混、不報 | 不報 | 不報 |
| 同 label 兩個 Deployment（穩定 3＋金絲雀 1 壞） | HTTP 報金絲雀，資源不比 | 報 canary-a 錯誤率（50% vs 0%） | 同左 |

改後的實際輸出（節錄）：

```
PASS  [pe-allbad-uneven @T+0m] 3 個全壞、其中一個更嚴重：其他 Pod 也壞（中位數 42% 錯誤、p95 2.5 秒）：不能只報一個說它是壞的
PASS  [pe-warm @T-7m] 3 個舊 Pod＋1 個剛起來的新 Pod（暖機）：新 Pod 1 分鐘（CPU 1 核、冷啟動慢）：暖機中不該報
PASS  [pe-replace @T-13m] 3 個 Pod，壞的那個被換掉（名字變）：剛換掉 1 分鐘：b 最後的資料還在 fresh（2 分鐘）內，報 b 也合理；新的 d 不能報
        pod_cpu 報不報都可以（實際 ['replace-b']）
PASS  [pe-scale @T-39m] 擴縮容 2→5→2，都正常：剛擴 1 分鐘：新 Pod CPU 高、記憶體低，都不能報（舊的也不能因為記憶體比新的高被報）
PASS  [pe-scalebad @T-34m] 擴縮容 2→5→2，a 一直錯誤 30%：擴 6 分鐘：5 個比，抓到 a
        [warning] 這個 Pod 的錯誤率比同服務其他 Pod 高：pe1356-scalebad、order-api、scalebad-a（30%，其他 4 個 Pod 中位數 0%，z=>999）
全部通過
```

改前的失敗（節錄）：

```
FAIL  [pe-allbad-uneven @T+0m] … http_error_rate 預期 無、實際 ['allbadun-c']
FAIL  [pe-warm @T-7m] … [warning] 這個 Pod 的 CPU 比同 Deployment 其他 Pod 高很多：…warm-new（1.00 核，其他 3 個 Pod 中位數 0.20 核，z=40.0）
FAIL  [pe-replace @T-13m] … pod_cpu 只准報 ['replace-b']、實際 ['replace-b', 'replace-d']
FAIL  [pe-scale @T-39m] … [warning] 這個 Pod 的記憶體比同 Deployment 其他 Pod 高很多：…scale-a（600MB，其他 4 個 Pod 中位數 240MB，z=15.0）
8 項失敗
```

單元測試（不需要 ES）：全壞不均在沒有 `max_peer_median` 時只報最嚴重的、有時不報；新 Pod 沒有 `warmup` 時被報、有時不報、6 分鐘後照樣抓；`warmup` ≥ window 會停下。`engine/` 整個 `pytest` 沒有因此壞掉的。

### 共用 cluster、真的 .NET 應用（`e2e/local-k3s/run-peer-edge-e2e.sh`）

- **怎麼測**：rancher-desktop，namespace `peer-edge`（dept=D1110），`dotnet-app.yaml` 開 3 副本；每分鐘同時用現在的設定和「拿掉 `warmup`、`max_peer_median`」的設定跑 `detector peer`。2026-10-28 23:24～23:54，全部通過。
- 共用 cluster 記憶體排程不下 3 份（編譯 initContainer 只設 limit 2Gi＝request 2Gi），腳本把 request 設成 512Mi、一次加一個副本。

| 情境 | 預期 | 實際 |
|---|---|---|
| 3 個都正常（跑 6 分鐘） | 不報 | 不報 |
| 一個 Pod 連線池漏光 4 分鐘 | 只報它 | 只報它：錯誤率 12%（其他 0%） |
| 刪掉它（名字變） | 舊名字 fresh 內可報、之後不報 | 第 1 分鐘報 2 項，第 2 分鐘起不報 |
| 新 Pod 起來後 12 分鐘 | 不報 | 不報；拿掉 `warmup` 的設定也 0 次 |
| 新 Pod 的 CPU | （記錄） | 起來 30 秒最高 0.31 核，之後 0.13～0.16 核 |
| 縮成 2 個、一個漏光 4 分鐘 | peer 不判斷 | `0 組、0 個 Pod 比過`；服務 5xx 571／3403（17%） |

實際輸出（節錄）：

```
  PASS  換掉前抓到 dotnet-app-85cbcdf99c-z9f85
        [warning] 這個 Pod 的錯誤率比同服務其他 Pod 高：peer-edge、dotnet-app、dotnet-app-85cbcdf99c-z9f85（12%，其他 2 個 Pod 中位數 0%，z=>999）
   23:35:57 刪掉後第 1 分鐘：舊 dotnet-app-85cbcdf99c-z9f85 報 2 項；新 dotnet-app-85cbcdf99c-pmdxb 報 0 項（沒有 warmup 的設定：0 項）；新 Pod CPU avg 0.31 核、max 0.31 核、第一筆 2026-10-28T15:35:27.040Z
   23:36:55 刪掉後第 2 分鐘：舊 dotnet-app-85cbcdf99c-z9f85 報 0 項；新 dotnet-app-85cbcdf99c-pmdxb 報 0 項（沒有 warmup 的設定：0 項）；新 Pod CPU avg 0.18 核、max 0.31 核、第一筆 2026-10-28T15:35:27.040Z
  PASS  舊名字 dotnet-app-85cbcdf99c-z9f85 第 1 分鐘後不再報（-1＝刪掉後一次都沒報）
  PASS  新 Pod dotnet-app-85cbcdf99c-pmdxb 12 分鐘內一次都不報
        （參考）沒有 warmup 的設定：新 Pod 被報 0 次
  PASS  2 個 Pod（dotnet-app-85cbcdf99c-vtx69 壞）：peer 不判斷
        （參考）peer.http_error_rate：0 組、0 個 Pod 比過
        （參考）整個服務最近 3 分鐘 5xx／全部：571 / 3403（規則 http.error_rate_floor 看這個）
全部通過
```

**發現的問題**（都在合成資料重現、修掉）：

1. 新 Pod 啟動時 CPU 高（合成：前 2 分鐘 1 核）會被報成「CPU 比其他 Pod 高很多」，換 Pod 後 3 分鐘還在報。→ `warmup`。
2. 擴容時新 Pod 比舊 Pod 多、記憶體還在長，中位數被拉低，**健康的舊 Pod** 被報記憶體高（600 vs 240MB）。→ `warmup`。
3. 全部都壞、一個更嚴重時只報那一個（100% vs 其他 42%），看起來像只有它壞。→ `max_peer_median`。

**沒測到的**：

- Java 應用的啟動（JIT、heap 長大）在真的 cluster 上沒測；第 1、2 項的啟動曲線是假設的，真的 .NET 應用沒有這個現象（最高 0.31 核）。
- 擴縮容 2→5→2 只在合成資料測；cluster 記憶體不夠同時跑 5 份。
- StatefulSet、DaemonSet 的 Pod 沒有 `k8s.deployment.name`，CPU、記憶體不會分組（讀程式推論，沒測）。
- 同 label 不同用途的兩個 Deployment（例如 api 和 admin）混在 HTTP 同一組：沒測。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 2 個副本的服務沒有同儕比對 | 合成「2 個 1 個壞」、cluster 縮成 2 個 | 常見的 HA 最小副本數，一個壞只能靠規則、baseline | 門檻選擇：要不要 2 個也比（只比 up、加倍數門檻），用試點資料決定 |
| 3 個壞 2 個不報 | 合成（30%、30%、0%） | 多數壞時分不出，靠規則 | 推論：可接受，規則 5% 會報 |
| `warmup` 5 分鐘是估的 | 合成的啟動曲線；真的 .NET 沒差 | heap 長超過 5 分鐘的服務擴容時仍可能誤報舊 Pod | 試點上看 Java 新 Pod 記憶體多久追上，再調 |
| 一起來就壞的新 Pod 前 5 分鐘不報 | 合成「新 Pod 一起來就壞」 | 偵測晚 5 分鐘 | 可接受；規則、baseline 不受影響 |
| `max_peer_median` 跟規則門檻寫兩處 | 改 `peer.yaml` 時 | 規則門檻改了這裡要跟著改 | 之後改成讀 `rules.yaml` |
| 舊 Pod 刪掉後 1～2 分鐘還會被報 | cluster「刪掉後第 1 分鐘報 2 項」 | 告警指到已不存在的 Pod | [⑥] 收斂時把已不存在的 Pod 標出來 |
| CPU、記憶體沒有「全壞」的上限 | 讀程式 | 全部 CPU 都高、一個更高時仍只報那一個 | 推論：CPU、記憶體沒有絕對的壞，先不加 |

- **決定**：採用 `warmup: 5m`、`max_peer_median`（錯誤率 5%、p95 2 秒）；`min_peers` 維持 3。
- **下一輪**：Java 應用在 cluster 上換 Pod、擴容各一次，量新 Pod CPU、記憶體多久追上；試點資料看 2 副本服務佔多少。
