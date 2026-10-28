# 靜態臨界值規則逐條邊緣驗證、規則草稿的動態調整介面（2026-10）

**狀態**：已完成　　**日期**：2026-10-28　　**相關**：分支 `worktree-agent-a1e90466f85efa1cb`（基準 `14a12ed`）；[rule-threshold](../phase5-learning-loop/2026-10-rule-threshold.md) 的 Act「規則核准後一直有效，沒有過期」
**模組**：④（判斷：靜態臨界值規則）、⑨（learning loop 的規則草稿）

## Plan：這一次要達到什麼

- **需求**：`rules.yaml` 39 條靜態臨界值規則只有 `run-rules-e2e.sh` 的 5 種壞法跑過，邊界（剛好等於門檻、`for` 剛好持續、資料缺漏、Pod 已刪除、重跑）沒人驗；learning loop 核准的規則草稿（`aiops-tuning` 的 `rule_suggestion`）只有單元測試。
- **目的**：每條規則都有「觸發」與「不觸發」案例，用真的 `detector rules` 對 ES 9 跑；規則草稿的生效、不生效、讀不到 `aiops-tuning` 各有實測。規則有錯就修。
- **範圍**：自己的 ES（`elasticsearch-setup.sh` 裝好的 index 結構）灌合成 metrics，不經 Collector、不上共用 cluster。門檻選擇的問題只記在 Act，不改門檻。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/rules.py` | `for`：對象要在 for 開頭之前就有資料才判斷 | 原本只要求 for 裡最早的點過一半；新 Pod 從第一筆就 Pending，2.5 分鐘就報 `for: 5m` |
| 同上 | 新增 `type: delta`（量測值淨增加：最後 − 最前） | `increase` 把變小當成計數器歸零，落後變少反而算成增加 |
| `engine/configs/baseline/rules.yaml` | `kafka.consumer_lag_growing` 改 `type: delta` | Kafka 落後是量測值，不是計數器 |
| `engine/detector/cmd_rules.py` | `approved_rules` 加 `expires_at`（跟 `--at` 的時間比）、草稿要有 `name` | README 寫「`expires_at` 到期自動失效」，規則草稿沒照做 |
| 同上 | 每條規則各自 try：失敗只跳過那條、其他照跑照寫，最後 exit 1 | 一條核准的草稿缺欄位（KeyError），整輪規則都沒寫進 `aiops-anomalies` |
| `engine/tests/rules_edge_e2e.py`（新） | 151 個案例＋重跑＋動態調整，對 `ES_URL` 跑，每項 PASS／FAIL | 可重跑；`ENGINE_DIR` 指到別份程式可重現修之前 |
| `engine/tests/test_rules.py` | 新對象要等滿 `for`；`delta` vs `increase`；`delta` 列入合法 type | 修的兩個 bug 各有不需要 ES 的測試 |
| `engine/tests/test_cmd_rules.py` | 過期不載入、`status` 要剛好是 `active`、草稿沒 `name` 不載入 | 同上 |
| `engine/configs/baseline/README.md`、`docs/troubleshooting.md` | `delta`、`for` 的新語意、失敗隔離、過期；四條排錯 | 跟程式一致 |

合成資料的做法（`rules_edge_e2e.py`）：照 ES 9 OTLP 入口的樣子寫進 TSDS 資料流 `metrics-<receiver>.otel-p2rules`——`resource.attributes`、`attributes` 是維度、`metrics.<名稱>` 用 `dynamic_templates` 指定 `gauge_double`／`exponential_histogram`（型別、`time_series_metric` 跟入口建的一致）；`service.name` 填 Pod label 的值。每個案例一個獨立對象，`NOW` 固定、用 `--at` 跑。

## Check：測試的目的與結果

### 每條規則：觸發／不觸發

`ES_URL=http://127.0.0.1:31501 python -X utf8 -m tests.rules_edge_e2e`（ES 9.5.3，2,314 份文件、151 個案例；一輪 `detector rules --at` 39 條規則觸發 54 個）。修後連跑兩次都是「規則：PASS 156／156」（151 案例＋ context、沒有別的規則觸發、39 條都有觸發案例、39 條都有不觸發案例、exit 0）。

| 規則 | 觸發案例 | 不觸發案例 | 結果 |
|---|---|---|---|
| `http.error_rate_floor` | 20 個請求錯 1 個（剛好 5%） | 4.9%；19 個請求錯 26%（min_events）；錯誤在 5 分 25 秒前 | ✅ 1／3 |
| `http.latency_p95_floor` | 30 個請求都 3 秒 | 都 1.5 秒；18 個請求 5 秒（min_events） | ✅ 1／2 |
| `container.restarted` | +1；Pod 已刪 5 分鐘（report_gone）；歸零 5→6→0→1 算 2 | 一直 3；重啟在 10 分鐘外；只有 1 點 | ✅ 3／3 |
| `deployment.unavailable` | 6/6 點不夠；5/6（83%）；新建 3 分 5 秒；中間缺 2 分鐘頭尾都不夠 | 4/6；最後一點恢復；換版 1 分鐘；新建 2.5 分鐘；縮到 0；已刪；缺一段只剩 2 點 | ✅ 4／7 |
| `statefulset.unavailable`、`daemonset.unavailable` | 3 分鐘少 1 個（剛好等於） | 短暫 1 分鐘；已刪 | ✅ 各 1／2 |
| `pod.failed` | phase 4；最後一筆 95 秒前 | phase 3（Succeeded）；已刪 3 分鐘 | ✅ 2／2 |
| `pod.pending` | 新 Pod Pending 5 分 5 秒 | 新 Pod 4 分 5 秒；轉 Running；中間一點 Running；已刪 | ✅ 1／4 |
| `job.failed` | 0→1；Job 已刪 4 分鐘（report_gone） | 一直 0 | ✅ 2／1 |
| `node.not_ready` | 最後 2 點（剛好 for 1m） | 只最後 1 點；節點已拿掉 | ✅ 1／2 |
| `node.clock_offset` | 剛好 100ms；−150ms（abs）；39 分鐘前量的；999ms | 99.999999ms；41 分鐘前；之前偏、最新正常 | ✅ 4／3 |
| `node.clock_offset_critical` | 剛好 1 秒 | 999ms；41 分鐘前 | ✅ 1／2 |
| `node.memory_pressure`、`node.disk_pressure` | 最新一點 1 | 最新一點 0；節點已拿掉 | ✅ 各 1／2 |
| `scrape.down` | 最後 2 點 up=0 | 只最後 1 點；Pod 已刪（最後 up=0） | ✅ 1／2 |
| `container.memory_near_limit` | 剛好 90% 蓋滿 2 分鐘 | 89.99%；沒設上限；中間一點 85%；已刪；新容器 1 分 35 秒 | ✅ 1／5 |
| `node.memory_high` | 剛好 90% 10 分鐘 | 89%；已拿掉 | ✅ 1／2 |
| `node.disk_high` | 剛好 85% 5 分 5 秒 | 84%；90% 只 4 分鐘；已拿掉 | ✅ 1／3 |
| `node.disk_full` | 剛好 95% 1 分鐘 | 94.9%；只最後 1 點 99% | ✅ 1／2 |
| `container.cpu_throttled` | 剛好 25%；計數歸零後仍 50% | 24%；已刪；只 1 點 | ✅ 2／3 |
| `jvm.old_gen_after_gc_high` | G1 Old Gen 剛好 80%；Tenured Gen 90% | 79%；Eden 95%（filter）；已刪 | ✅ 2／3 |
| `jvm.major_gc_frequent` | 5 分鐘剛好 5 次 | 4 次（另 3 次在 for 外）；minor GC 42 次；已刪 | ✅ 1／3 |
| `jvm.gc_overhead` | 每 30 秒 GC 3 秒（剛好 10%） | 9.7%；已刪 | ✅ 1／2 |
| `db.pool_exhausted` | used 9／max 10（max 在另一份文件，join） | 8／10；idle 10、used 1；沒有 max；已刪 | ✅ 1／4 |
| `db.pool_exhausted_otlp` | 9／10 | 89%；已刪 | ✅ 1／2 |
| `db.pool_waiting` | 剛好 1 個、1 分鐘 | 0；已刪 | ✅ 1／2 |
| `dotnet.gen2_gc_frequent`、`dotnet8.gen2_gc_frequent` | 5 分鐘剛好 +5 | +4；gen0 +200（只 .NET 9 那條）；已刪 | ✅ 1／3、1／2 |
| `dotnet.out_of_memory` | OutOfMemoryException +1 | 別種例外 +1000；已刪（沒 report_gone） | ✅ 1／2 |
| `dotnet8.exceptions_burst` | 5 分鐘剛好 +500 | +499；已刪 | ✅ 1／2 |
| `dotnet.thread_pool_starvation`、`dotnet8.…` | 佇列剛好 10、2 分鐘 | 9；已刪 | ✅ 各 1／2 |
| `dotnet.thread_pool_growth`、`dotnet8.…` | 32 條／2 核（剛好 16） | 15；沒有 CPU 核數；已刪 | ✅ 各 1／3 |
| `kafka.consumer_lag` | 剛好 10,000、3 分鐘 | 9,999；group 已不在 | ✅ 1／2 |
| `kafka.consumer_lag_growing` | 5 分鐘淨增加剛好 5,000 | 4,999；一直變少（追上來）；group 已不在 | ✅ 1／3 |
| `eap.secs_disconnected` | 最後 2 點斷線 | 只最後 1 點；資料停 3 分鐘 | ✅ 1／2 |
| `eap.plc_scan_slow` | 剛好 50ms、2 分鐘 | 49.9ms；資料停了 | ✅ 1／2 |
| `eap.secs_retransmits` | 5 分鐘剛好 +10 | +9；資料停了 | ✅ 1／2 |

另外兩項：`container.restarted` 的 keys 帶 `resource.attributes.service.name`＝`p2-crash`（Pod label）、`k8s.node.name`（context）✅；沒有 `rules.yaml` 以外的規則觸發 ✅。

### 修之前的程式跑同一份案例

`ENGINE_DIR=<52dda2a 的 engine>`（規則相關檔案跟 `14a12ed` 相同）：規則 PASS 152／156、動態調整 8／10。

| 案例 | 修之前 | 修之後 |
|---|---|---|
| `deployment.unavailable` 新建 2.5 分鐘（for 3m） | 觸發（值 2） | 不觸發；3 分 5 秒才觸發 |
| `pod.pending` 新 Pod 4 分 5 秒（for 5m） | 觸發 | 不觸發；5 分 5 秒才觸發 |
| `container.memory_near_limit` 新容器 1 分 35 秒（for 2m） | 觸發（95%） | 不觸發 |
| `kafka.consumer_lag_growing` 落後 20,000 一路降到 8,000 | 觸發，算成「增加 93,600」 | 不觸發 |
| 規則草稿 `expires_at` 已過 | 照樣載入、觸發 | 不載入 |
| 核准的草稿缺 `index` | KeyError，整輪 0 筆寫入 | 跳過那條；yaml 的 3 筆照寫、exit 1 |

### interval：同一個對象重跑

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 同一分鐘重跑不重複記 | 同一個 `--at` 再跑一次 | 54 筆不變、`_id` 全部一樣 ✅ |
| 同一分鐘晚一點再跑 | `--at NOW+10s` | 54 筆不變 ✅ |
| 下一分鐘另記一筆 | `--at NOW+60s --only container.restarted` | 新的 slot 3 筆（3 個重啟過的對象，含已刪的 Pod）✅ |

### 動態調整介面：`aiops-tuning` 的規則草稿

草稿是 `p2.tuned_lag`（value、落後 ≥ 5,000、只看 `p2-lag-9999`），每次跑 `rules --only p2.tuned_lag,kafka.consumer_lag`。動態調整：PASS 10／10。

| 情況 | 預期 | 實際 |
|---|---|---|
| `aiops-tuning` 不存在 | 只跑 rules.yaml、exit 0 | ✅ |
| `status: proposed` | 不載入 | ✅ 沒有「載入」、沒觸發 |
| `adjust approve` 後下一輪 | 載入並觸發，不用重啟、不改 yaml | ✅「從 aiops-tuning 載入 1 條核准的規則：p2.tuned_lag」，9999 ≥ 5000 |
| `status: rejected`、`inactive` | 不載入 | ✅ |
| `active`、`expires_at` 已過（NOW − 1h） | 不載入 | ✅（修之前 ❌ 照樣載入） |
| `active`、`expires_at` 還沒到（NOW + 7d） | 載入 | ✅ |
| 草稿缺 `index` | 只跳過它、yaml 照寫、exit 非 0 | ✅「規則 p2.tuned_lag 失敗，跳過：KeyError: 'index'」，exit 1 |
| `aiops-tuning` 關閉（ES 回 400） | 只跑 rules.yaml、exit 0 | ✅（`adjust.load` 吞掉，沒印訊息） |

單元測試：`pytest -q` 318 passed、5 skipped。

### 沒測到的

| 項目 | 原因 |
|---|---|
| `http.latency_p95_floor` 剛好 2 秒 | `exponential_histogram` 的百分位是 bucket 近似值，做不出「剛好等於」 |
| 經過真的 Collector、應用送的資料 | 合成資料直接寫 ES；欄位名稱照 `rules.yaml` 與 README 實測名稱 |
| `window` 規則的「Pod 已刪除」 | 依服務 × API 算 5 分鐘，不看 Pod |
| 修過的 `for` 對 K8s 上 e2e 的時間 | `run-rules-e2e.sh` 等 6 分鐘、應不受影響；`run-rca-demo.sh` 沒重跑 |
| Collector 去重（TSDS 同對象同時間只收一筆） | 合成資料每個時間點只寫一筆，沒有重送 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `min_ratio: 0.8` 在 6 個點時要 5 點（83%） | `deployment.unavailable` 4/6 不觸發 | 實際比 80% 嚴；點數少時更明顯 | 門檻選擇，看真實資料再定 |
| 中間缺資料、頭尾都成立就觸發 | `p2-dep-gap`：缺 2 分鐘仍觸發 | Collector 重啟時可能多報或少報 | 先接受；需要時加「至少幾點」 |
| 一次重啟記 10 筆（每分鐘一筆、lookback 10m） | interval 下一分鐘又記 | 異常總表量多 | [⑥] 收斂（連續的點合成一段） |
| 已刪的 Pod 重啟仍連報到 10 分鐘 | `p2-crash-gone` 5 分鐘前刪也報 | report_gone 的設計 | 同上 |
| 偏移 ≥ 1 秒時兩條時鐘規則都報 | 999ms 由非 critical 報；1 秒兩條都成立 | 同一件事兩筆 | [⑥] 收斂時合併 |
| `delta` 只看頭尾，鋸齒型落後抓不到 | 改 `delta` 時 | 落後忽高忽低但淨增加小就不報 | 門檻選擇，看真實 Kafka 資料 |
| 沒有東西會寫 `expires_at` | 查 `feedback suggest`、`adjust approve` | 程式支援了，但核准時設不了過期 | [⑨] `adjust approve` 加 `--expires` |
| `aiops-tuning` 讀不到時沒有訊息 | 關閉 index 的案例 | 核准的規則默默沒跑，看不出來 | [⑨] `adjust.load` 讀失敗時印一行 |

- **決定**：`for` 改成「對象在 for 開頭前就有資料」；量測值的淨增加用新的 `delta`，`increase` 只給計數器。
- **下一輪**：K8s 上重跑 `run-rules-e2e.sh`、`run-rca-demo.sh` 看 `for` 的改動；正式排程後用真實資料看 `min_ratio`、`delta` 的誤報漏報（[README.md](../README.md) 的「用真實資料校正門檻」）。
