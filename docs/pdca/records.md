# PDCA 紀錄索引

整個專案的現況與待辦在 [README.md](README.md)；這份是**每一輪紀錄**的索引（依 phase 分，已封存的在最後一節）。

做完一件值得記的事（一個需求、一次改動、一次實測、一個決定），照 [TEMPLATE.md](TEMPLATE.md) 寫一份，檔名 `YYYY-MM-主題.md`，放進對應 phase 的資料夾，並在那個 phase 的表加一行。**上面四張表是「現在還剩什麼」，這裡是「做過什麼、為什麼這樣決定」**，兩邊互相連結，不要抄一份。

四段各寫什麼（只寫結論，不寫調查過程）：

| 段 | 寫什麼 |
|---|---|
| **Plan** | 這一次要達到什麼：需求（誰、什麼情況、什麼問題）、目的（做完變成什麼樣）、範圍（做什麼、不做什麼） |
| **Do** | 改動哪些 code、改了什麼、**為何要改**（每個檔案一列）；沒改 code 就寫「無 code 改動」並說明做了什麼 |
| **Check** | 測試的**目的**（為了確認什麼）、怎麼測、**結果**（觀察到什麼）。不寫「N 個通過」 |
| **Act** | Check 後**沒改善的、還沒解決的**，每項寫**在什麼情況下發現的**、影響、後續 |

排錯的結論不寫在這裡，集中在 [troubleshooting.md](../troubleshooting.md)。

### Phase 1 收值（已封存：`archive/phase1-collect/`）

**自建的收值（Collector）2026-11 已刪除**，這 19 份描述的程式已不在 repo（在 git 歷史），移到 `archive/`；當時的結論與數字不代表現在。metrics、trace、log 怎麼收、怎麼存、三種怎麼關聯（欄位、時間、key）

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase1-collect/2026-10-durability.md](archive/phase1-collect/2026-10-durability.md) | ① 收值線在各種故障下會不會掉、會不會重複（k3s 實測 12 個情境） | 修了兩個：node 端 trace 佇列共用一個檔（多台 trace-sampler 時只有一台收得到）、batch processor 當掉掉 1 秒；修正後 Collector 換版、ES 停 30 分鐘、ES 回 429 都掉 0，kill -9 最多掉 0.2 秒；trace-sampler 當掉掉「decision_wait＋1 秒」那台的、PVC 滿了掉、刪 hostPath 掉停機那段 log |
| [phase1-collect/2026-10-es-startup-401.md](archive/phase1-collect/2026-10-es-startup-401.md) | ① ES 剛起來回 401，Collector 直接丟 | 401 窗口約 1 秒（.security 還沒恢復）；修在 ES 入口：健康檢查用 API key 打 GET /（實測掉 0）；兩台 ES 滾動重啟也有窗口（投票名單只有一台）、新檢查都掉 0；不加 Kafka、不改 Collector |
| [phase1-collect/2026-10-field-unify.md](archive/phase1-collect/2026-10-field-unify.md) | ② 欄位統一：log 身分欄位補 OTel 路徑、Java HTTP 指標在 Collector 改名；metrics ↔ trace 怎麼連 | 在 ingestion 處理、不在查詢層：log 加 alias、Java 指標在 Collector 改名；`variants` 拿掉；`quality.metric_name_legacy` 盯舊名字 |
| [phase1-collect/2026-10-ingest-keys-check.md](archive/phase1-collect/2026-10-ingest-keys-check.md) | ①②③ 收值、共同鑰匙圈、log↔trace 綁定、metrics 時間窗：docker 重驗 | 鑰匙圈交叉查得到；log↔trace 要 log 格式有印 trace_id，純文字的要開 `otel.logs.exporter: otlp`（新加的路，pipeline 原本會丟 trace_id，已修；真的 Java agent、.NET 自動埋點各驗一次）；metrics 三層窗對得上（276／分、13%、p95 3.75 秒）；.NET Framework：trace／metrics 可、ILogger 直送不行 |
| [phase1-collect/2026-10-ingest-simplify.md](archive/phase1-collect/2026-10-ingest-simplify.md) | ①③④ 集中式資料寫入憑證 (Shared Ingest Token)、ES failure store、靜態臨界值規則 | 一把寬 pattern 的 key、一個出口，資料照樣依 group 分；log 的 `k8s-*` 開 failure store（OTel 的預設已開），`quality.ingest_rejected` 有一筆就報；rules 加 `window` 類型，錯誤率 ≥ 5%、p95 ≥ 2 秒保底 |
| [phase1-collect/2026-10-keys-k8s.md](archive/phase1-collect/2026-10-keys-k8s.md) | ①② 共同鑰匙圈在 K8s 上：五種資料、11 個邊緣情境 | 服務名稱改成一律以 Pod label 為準（原本 trace、OTLP 用應用自設的）；改 dept 後 0～3 秒切（Prometheus 等下一次抓，≤30 秒）；hostNetwork 的 OTLP 進 unassigned；trace-sampler 2 台時 trace 幾乎全丟（未修） |
| [phase1-collect/2026-10-k3d-e2e.md](archive/phase1-collect/2026-10-k3d-e2e.md) | ①②③⑧ k3d 上的 K8s 實測：Grafana initContainer、Collector 邊緣身份鑑別與標籤注入 (Edge Identity Sanitization & Injection)、OTLP log、chart | 都跑過：initContainer 裝得起來、SHA 錯停 Init:Error 且舊 Pod 繼續服務；K8s 上身分標籤注入正確；OTLP log 帶 trace_id 對得到 trace；確認開 otlp 有兩份 log；k3d 在這個環境要特別處理（腳本在 e2e/local-k3d） |
| [phase1-collect/2026-10-sdk-inject.md](archive/phase1-collect/2026-10-sdk-inject.md) | ① 不改程式的自動埋點：子執行緒、背景排程、上游 traceparent、兩跳、Kafka 時 log 帶哪個 trace_id | Java agent 2.20.0、.NET 8 共 21 個情境都跟預期一樣；Java 自己 `new Thread`、背景排程、.NET Kafka 消費端的 log 沒有 trace_id；未取樣 `-00` 的 log 有 trace_id 但 ES 沒有 trace |
| [phase1-collect/2026-10-metrics-trace.md](archive/phase1-collect/2026-10-metrics-trace.md) | ①②⑦ metrics 異常 → 用服務＋路由＋時間窗撈 trace 的邊緣情況 | 時間對齊（1.9%）、counter 歸零（少一次抓取、沒負值）、部分變慢（慢的 100% 挑到）都可以；路由、服務名對不上就 0 筆且不說原因；Micrometer 名字不改名；不到 1 秒的變慢只剩 4.6% |
| [phase1-collect/2026-10-log-trace-bind.md](archive/phase1-collect/2026-10-log-trace-bind.md) | ①②⑦ log 的 trace_id／span_id：各種格式、值、時間差、檔案＋OTLP 兩份 | 改前大寫、全 0、格式錯原樣寫入（11 種綁錯），已改成正規化、錯的不設；加 `log.source` 分兩份；investigator 撈 log 多放 30 分鐘 |
| [phase1-collect/2026-10-load-test.md](archive/phase1-collect/2026-10-load-test.md) | ①③ 收值壓測：加量、突發、大行、50 Pod、高基數、千段 trace、ES 慢／拒收 | 正式設定下都掉 0：log 10,000 行、metrics 50,000 點、trace 3,000 span／秒內延遲 < 10 秒，再大只是晚到；佇列縮小時 trace-sampler 丟 45%；log 寫入 index 大過 ES 記憶體會停住（要定 ILM 大小） |
| [phase1-collect/2026-10-retention.md](archive/phase1-collect/2026-10-retention.md) | 保留天數（metrics 降解析度、log 統計、trace 分兩種） | 已合；trace baseline 改成只讀隨機留下的 `traces-generic`（沒用真實資料驗證） |
| [phase1-collect/2026-10-time-alignment.md](archive/phase1-collect/2026-10-time-alignment.md) | ② 時間對齊：log 沒帶時區、收資料延遲、主機時鐘 | 沒帶時區的原本差 8 小時、空白分隔的默默換時間，已修；延遲與時鐘偏差、主機時鐘都抓得到；`NTP_SERVER` 要填 |
| [phase1-collect/2026-10-trace-durability.md](archive/phase1-collect/2026-10-trace-durability.md) | ① trace 收值線要不要 message queue | 不用 Kafka；兩段佇列改寫磁碟；node 端放子 exporter 的做法後來發現有錯、當掉掉多少重新量過（[durability](archive/phase1-collect/2026-10-durability.md)） |
| [phase1-collect/2026-10-trace-merge.md](archive/phase1-collect/2026-10-trace-merge.md) | ①②③⑦ trace 不取樣（100% 收）、每個請求合成一筆：拆 trace-sampler、Collector 直接寫 raw（2 天），engine 每分鐘 `trace-merge` 合成 `traces-merged-*`（7 天），根因分析改讀 merged | 已完成，2026-11-01 合入 master（PR #36） |
| [phase1-collect/2026-11-gitlab-ci.md](archive/phase1-collect/2026-11-gitlab-ci.md) | ①③⑧ 各元件（elasticsearch、collector、aiops-engine、grafana）各自一份 GitLab CI；setup 在 CI 跑時 key 不印進 log、寫成 values 檔；三個 chart 能從 values 建 Secret | setup 的 CI 模式對測試 ES 驗過（log 無 key、writer 403、grafana／probe 200）、chart 渲染驗過；GitLab 上沒跑過；master 原版 setup 對這個測試 ES 本來就失敗（rollover 迴圈） |
| [phase1-collect/2026-11-collector-existing-guide.md](archive/phase1-collect/2026-11-collector-existing-guide.md) | ①② Collector 設定拆成通用與 AIOps 專屬（`collector/code/aiops.yaml`，行為不變）；公司已經有 Collector 時 AIOps 要加哪些（必加、選配、歸屬與安全） | 拆分前後合併設定逐項相等；缺 `aiops.yaml` 會被 Collector 擋下；測試 cluster 實部署資料沒斷；cadvisor、ntp 沒資料沒驗到 |
| [phase1-collect/2026-11-es-setup-helm-job.md](archive/phase1-collect/2026-11-es-setup-helm-job.md) | ③① ES 結構設定改在 K8s 裡用 Job 跑（`elasticsearch/setup-helmchart`，`esSetup.enabled` 開關、`skipKeys`）：key 直接寫進同 cluster 的 Secret，Collector 的由 CI 從 `es-setup-keys` 複製；setup 腳本 Windows `jq` 的 CR 修正 | 測試 cluster 實跑 Job：key 寫對、權限對、`grafana-env` 其他欄位沒被蓋掉、日常 upgrade 不跑、`skipKeys` 不發 key；GitLab 上沒跑過 |
| [phase1-collect/2026-11-es-two-pods.md](archive/phase1-collect/2026-11-es-two-pods.md) | ③① 測試用 ES 改成兩個資料節點＋一個只投票節點：掛一個資料節點 log、trace 都 0 遺失；兩個都停仍掉（log 10.5%、trace 13.8%） | 已完成；全停的遺失記為已知風險 |

### Phase 2 異常分析（`phase2-anomaly/`）

baseline（中位數＋MAD）、規則、資料品質、餵給 baseline 前的清資料與排除

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase2-anomaly/2026-10-adjust-schedule-verify.md](phase2-anomaly/2026-10-adjust-schedule-verify.md) | ②④⑤ 排除與調整、行事曆、慢性病的邊界；第一階段 4 個 CronJob 在 K8s 跑 30 分鐘 | 邊界 31 項全過；修：台灣假日吞掉 24×7 服務的故障、沒寫時區差 8 小時、每天的 train 失敗不重試；CronJob 每分鐘都跑、Forbid、逾時、重試都實測 |
| [phase2-anomaly/2026-10-anomaly-owner.md](phase2-anomaly/2026-10-anomaly-owner.md) | ⑤ 異常總表帶 `dept`、`group` | 資料本來就帶，異常寫回時補；namespace 優先、其次服務、對不到 unassigned；告警依 group 分送的前置 |
| [phase2-anomaly/2026-10-baseline-threshold-tune.md](phase2-anomaly/2026-10-baseline-threshold-tune.md) | ④ 動態基準線的誤報：p95 `instant_z: null`（只改設定；`min_events.rate` 20 → 100 試過，已退回） | 只留 `instant_z: null`：一週 anomaly 1.50% → 0.53%、warning 1.00% → 1.97%，都沒到目標（≤ 0.1%、≤ 1%），p95 超標的點只是從 anomaly 變 warning；真異常都抓到；下一輪改程式：σ 下限＝半個桶寬、錯誤率至少 N 個 5xx |
| [phase2-anomaly/2026-11-baseline-false-alarm.md](phase2-anomaly/2026-11-baseline-false-alarm.md) | ④ 動態基準線誤報第三輪：p95 σ 至少半個桶寬（OTel 邊界）、錯誤率至少 3 個 5xx：一週 anomaly 0.530% → 0.068%、warning 1.97% → 0.008% | 已完成 |
| [phase2-anomaly/2026-11-trace-segments-merged.md](phase2-anomaly/2026-11-trace-segments-merged.md) | ④ trace 每一段的統計改讀合成後的 `traces-merged-*`（nested → composite）：同一窗口與讀 raw 的結果一致 | 已完成 |
| [phase2-anomaly/2026-11-trace-sample-100.md](phase2-anomaly/2026-11-trace-sample-100.md) | ④ trace 每一段每點抽 20 → 100 個請求：耗時 0.1 → 0.3～0.5 秒；10% 請求變慢報得出來、2% 例外吞噬不再誤報 | 已完成 |
| [phase2-anomaly/2026-10-baseline-verify.md](phase2-anomaly/2026-10-baseline-verify.md) | ④ 動態基準線學習：邊緣情境、誤報率、調整介面（合成資料、Collector 的形狀） | 修 4 個：收集中斷時補 0（30 分鐘窗 91 個誤報）、error log 的 λ 學高 40%、壞調整整輪失敗、override 過期時點；一週 anomaly 1.5%（p95 跳桶 88%、錯誤率 1 個 5xx）；調整介面都下一輪生效 |
| [phase2-anomaly/2026-10-chronic-calendar.md](phase2-anomaly/2026-10-chronic-calendar.md) | 慢性病清單與行事曆 | 已合；清單沒送到負責人手上；假日預設連當天故障也不報 |
| [phase2-anomaly/2026-10-data-quality-check.md](phase2-anomaly/2026-10-data-quality-check.md) | 資料品質檢查（路由沒參數化、欄位缺、資料停了、unassigned 變多） | 已合；第一天只靠昨天比的檢查不會報；門檻是估的 |
| [phase2-anomaly/2026-10-data-sources.md](phase2-anomaly/2026-10-data-sources.md) | 資料來源接口＋假資料分開；抓台灣行事曆 | 已合；行事曆來源非官方；負責人真來源還沒有 |
| [phase2-anomaly/2026-10-exclude-adjust.md](phase2-anomaly/2026-10-exclude-adjust.md) | 排除已知干擾（排除時段、重置點、週期放寬） | 已合；重置後暖機 24 小時內不判斷（估的）；只放寬不變嚴 |
| [phase2-anomaly/2026-10-instant-anomaly.md](phase2-anomaly/2026-10-instant-anomaly.md) | ④ 單點即判定 (Instant Anomaly)：依序列設 `confirm_points`、偏太多（`instant_z`）不用等；`tune` 能調 | `http.error_rate` 單點即判定、全域 `instant_z: 15`；模擬資料實測 z 20～49 的單點變 anomaly、z 5.2 的誤報仍是 warning |
| [phase2-anomaly/2026-10-peer-verify.md](phase2-anomaly/2026-10-peer-verify.md) | ④ Pod 同儕橫向比對：副本數 1～5、Pod 換掉、擴縮容、全壞、同名服務的邊緣情境 | 合成資料 37 項改前 8 項錯：新 Pod 暖機被報、擴容時舊 Pod 被報記憶體、全壞只報一個；加 `warmup`、`max_peer_median` 後全對；真的 .NET 換 Pod、2 副本在 cluster 上照預期；2 個副本不判斷、暖機曲線是假設（Act） |
| [phase2-anomaly/2026-10-rules-verify.md](phase2-anomaly/2026-10-rules-verify.md) | ④⑨ 靜態臨界值規則 39 條逐條邊緣驗證；規則草稿的動態調整介面 | 151 案例全過；修 4 個：新對象不到 `for` 就報、Kafka 落後變少被當增加（加 `delta`）、草稿過期仍生效、一條草稿壞整輪不寫 |
| [phase2-anomaly/2026-10-split-detector-cli.md](phase2-anomaly/2026-10-split-detector-cli.md) | ④⑤⑨ detector 的 `__main__.py`（1,100 行）依指令拆檔 | 1,100 行 → `__main__.py` 160 行＋ 10 個檔；只搬不改，200 個測試、ruff、9 個指令實跑都過 |
| [phase2-anomaly/2026-10-trace-quality-verify.md](phase2-anomaly/2026-10-trace-quality-verify.md) | ②④ trace 段異常、資料品質的邊緣情境（自己的 ES 灌合成資料） | 41 PASS 2 FAIL；修 4 個（`db.system.name`、ID 比例抽樣、缺 namespace 時 `quality` 中斷、3 個新檢查）；例外吞噬定義、第一天路由數維持新做法（已決定）；部分請求變慢、Pod 沒服務 label 的 log 抓不到 |
| [phase2-anomaly/2026-11-aiops-retention-backup.md](phase2-anomaly/2026-11-aiops-retention-backup.md) | ③⑤⑧⑨ aiops-* 的保留天數與備份：`detector prune` 每天刪超過天數的異常、trace 段統計、已結案告警；setup 註冊 snapshot 儲存庫與每天的 SLM 政策（人工判斷的三個索引不設天數、只備份） | 臨時索引實測刪對、不重算；測試 chart 三節點掛 `/snapshots`，備份 5 個索引 1.2 秒、刪 `aiops-tuning` 再還原 4 筆對 4 筆；正式儲存庫由管 ES 的人決定 |
| [phase2-anomaly/2026-11-baseline-more-series.md](phase2-anomaly/2026-11-baseline-more-series.md) | ④ 動態基準線補三條序列：p99、慢請求比例（超過 2 秒）、往外呼叫 p95；`detector shift` 每天看哪條序列一階跳變、提 `reset_point`（proposed） | 新 value 寫法 `hist_ratio_above`（`percentile_ranks`）；真資料 p99、慢請求比例各 298 條序列學得出來，往外呼叫沒活資料；`shift` 合成資料對、真 ES 跑完 0 筆（沒換版）；ES `change_point` 聚合要授權，不做 |
| [phase2-anomaly/2026-11-peer-z-and-slope.md](phase2-anomaly/2026-11-peer-z-and-slope.md) | ④ Pod 同儕橫向比對的 z 依其他 Pod 數分級（其他 2 個用 3）；趨勢規則 `container.memory_leak`：30 分鐘 Theil–Sen 斜率、三段低點墊高、照這個速度 ≤ 6 小時到上限 | 合成 e2e：同儕 38／38（10/31 的洩漏案例 z 3.1 抓到）、規則 164／164（8 個新案例）；真 cluster 沒重跑；HPA 擴容後老 Pod 誤報的推論未驗 |

### Phase 3 定位問題（`phase3-locate/`）

metrics → trace → log 找到哪一段、哪行 log；事件整合、排程、通知出口

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase3-locate/2026-10-alert-exit.md](phase3-locate/2026-10-alert-exit.md) | ⑥ 事件整合，通知交給 Grafana | 同服務合成一件寫 `aiops-alerts`（帶 dept、摘要、根因報告）；Grafana 12.2 實測一件一則、依 dept 分送、資料品質送平台、結束發恢復；ES 9 要寫 `orderBy: _count` |
| [phase3-locate/2026-10-alert-flap.md](phase3-locate/2026-10-alert-flap.md) | ⑥⑧ 件還開著，Grafana 卻發恢復通知 | 一件只有一筆、每輪覆蓋；覆蓋落在 Grafana 排定評估與送出查詢之間時查到 0 筆、當成恢復；engine 寫 `@timestamp` 往前 1 分鐘 |
| [phase3-locate/2026-10-rca-demo.md](phase3-locate/2026-10-rca-demo.md) | ①④⑥⑦⑧ 整條線在 K8s 上跑：Pod 連續送 metrics、trace、log → 規則真的偵測 → 一件 → 根因＋LLM → Grafana | Rancher Desktop 實測：事故 3 分 40 秒後開成一件、根因指到 Oracle 那段與那行 log、LLM 摘要帶依據、`/it` 收到通知、儀表板六格都有資料；抓到 Grafana 每分鐘重發（summary 是 label）→ summary 不放會變的字；metrics→trace 是「服務＋API＋時間」的統計對應、不是 trace_id |
| [phase3-locate/2026-10-schedule.md](phase3-locate/2026-10-schedule.md) | ④⑤⑥⑧ 排程上線：CronJob、「AIOps 停了」告警、先只寫入不通知 | Dockerfile、10 個 CronJob、回饋網頁 Deployment、心跳＋Grafana 規則；image 建好跑過，K8s 沒跑過 |
| [phase3-locate/2026-10-single-pod-scheduler.md](phase3-locate/2026-10-single-pod-scheduler.md) | ④⑤⑥⑧ 排程改成一隻常駐 pod：內建排程器（cron、不重疊、逾時、重試）＋回饋網頁兩個 container；排程表進 `configs/schedule/schedule.yaml` | 單元測試 369 過；Rancher Desktop 一隻 pod 實跑：每分鐘不漏、不重疊、逾時砍、重試、壞 key 失敗／修好恢復全過（22 項）；發現同秒起跑會搶建 index，已修；`train` 02:00 自己跑、OOM 後重啟沒測 |
| [phase3-locate/2026-10-detect-latency.md](phase3-locate/2026-10-detect-latency.md) | ④⑥⑧ 偵測的時間：p95 一個點暴衝會不會開事故、事故到通知的延遲 | p95 暴衝（5 分鐘窗 9% 慢）實測第 2 輪（+70 秒）開出事故，只多 1 個慢請求（2.4%）不報；事故到通知的延遲待 ⑧⑨ 實跑 |
| [phase3-locate/2026-10-cross-service-aggregation.md](phase3-locate/2026-10-cross-service-aggregation.md) | ⑥⑦ 跨服務事件聚合（一層父子關係）：下游出事連帶上游只開一件、留下游 | 每輪從 trace 算「誰呼叫誰」（重用 `topology`）；上游異常併進下游那件（`upstream_services`），上游先開的件收攏（`merged_into`）；合成資料 e2e 聚合率 5/5、誤併 0/5；多層只併一層，K8s 上待驗 |
| [phase3-locate/2026-10-scenario4-mtti.md](phase3-locate/2026-10-scenario4-mtti.md) | ④⑥⑦⑧ 情境 4（第三方 API 中斷）注入＋MTTI 量測腳本 | `incident-external`（sms-gateway 10 秒＋504）：報告指到 `POST → sms-gateway`、錯誤源頭同段、log 帶 `sms.example.com` 504、LLM 歸因第三方；`measure-mtti.sh` 各量 3 次：情境 4 中位數 1:32、情境 1 3:15（沒到 3 分鐘，卡在 5 分鐘窗內錯誤率要 2 分多才過 5%）；縮短選項列表，門檻不動；發現開件當下 trace 還沒到、根因分析可能空或指錯段 |

### Phase 4 RCA＋LLM＋graph RAG（`phase4-rca-llm/`）

相似事故檢索、LLM 摘要；graph RAG 構想在 [rca/graph-rag.md](../rca/README.md)

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase4-rca-llm/2026-10-llm-summary.md](phase4-rca-llm/2026-10-llm-summary.md) | ⑦⑨ LLM 摘要（RAG 的生成） | 輸入包＋每句附依據的檢查＋template 退路；捏造 id 整段不顯示實測過；真的模型（閘道 401）沒跑過、沒評測集 |
| [phase4-rca-llm/2026-10-similar-incidents.md](phase4-rca-llm/2026-10-similar-incidents.md) | ⑦⑨ 相似事故檢索（RAG 的檢索） | 指紋＋欄位比對、自己算分寫出為什麼像、只檢索已確認；第 2 件附上第 1 件實測過；門檻權重是估的、模擬沒 trace、舊事故匯入格式未寫 |
| [phase4-rca-llm/2026-10-incident-import.md](phase4-rca-llm/2026-10-incident-import.md) | ⑦⑨ 歷史事故單匯入＋Top-3 召回率（POC 任務 4） | `investigator import-incidents` 吃 CSV／JSON、缺欄位略過、重跑不重複、沒時區當台北，範例檔 10 筆；20 筆**合成**事故留一法 recall@3 = 10/20（50%）：指紋找的是「同一個地方又出事」，同根因在不同服務的找不到；真實事故單匯入後要重量 |
| [phase4-rca-llm/2026-11-rca-shared-dep-trend-commonality.md](phase4-rca-llm/2026-11-rca-shared-dep-trend-commonality.md) | ⑥⑦ 不同服務卡同一個外部依賴的件互相標同源（不合併）；報告多「出事前的趨勢（突然／慢慢惡化）」與「慢請求的共同點（Pod、版本、業務 ID、SQL）」 | 單元測試 12 個；測試 ES 今天的 MTTI 事故實跑：趨勢判「突然」對、共同點抓到換版後的新 Pod（標「平常沒見過」）；同源沒在真環境看過 |
| [phase4-rca-llm/2026-11-demo-alert-drilldown-feedback.md](phase4-rca-llm/2026-11-demo-alert-drilldown-feedback.md) | ⑧⑨ 示範儀表板：⑤ 一列一件，title 格點眼睛跳出 Grafana 抽屜看根因全文（件多存 `report_titled`）、最下面是老師傅回饋網址；點標題開報告或回饋；⑥ 拿掉；報告全文尾端帶回饋網址；回饋頁 `full=1` 全文＋表單 | Grafana 實開：抽屜顯示全文與回饋網址；HTML 自畫抽屜被 Grafana 過濾、不關安全設定做不到；沒在回饋頁送出 |
| [phase4-rca-llm/2026-11-demo-trace-id.md](phase4-rca-llm/2026-11-demo-trace-id.md) | ⑧ 示範儀表板 ③ 改成原始 span，每一列有 trace_id；④ 每行顯示 trace_id，搜尋框搜得到 | 瀏覽器確認同一 trace_id 的 5 段都在；terms 依耗時排序會缺段（shard 各取前幾名），不用 |
| [phase4-rca-llm/2026-11-rca-retry-no-trace.md](phase4-rca-llm/2026-11-rca-retry-no-trace.md) | ⑥⑦ 沒有 trace 的件每 2 分鐘重跑根因分析（最多 5 次）；還沒就緒時不說「沒變慢」 | 真實的件重跑成功；基準乾淨時根因指到注入的 `SELECT LOT_HISTORY`；同一件內第二次事故不重跑 |

### Phase 5 learning loop＋動態門檻（`phase5-learning-loop/`）

值班回饋、事故紀錄、回測、門檻調整、量化

| 檔案 | 主題 | 結論 |
|---|---|---|
| [phase5-learning-loop/2026-10-check-feedback.md](phase5-learning-loop/2026-10-check-feedback.md) | Check：事故紀錄與值班回饋 | 已合；沒有告警出口、回測；回饋靠指令 |
| [phase5-learning-loop/2026-10-expert-feedback.md](phase5-learning-loop/2026-10-expert-feedback.md) | ⑧⑨ 老師傅的回饋入口 | 小網頁（`feedback_web`）：預填告警、一鍵判斷、存根因分類、處置、怎麼看出來的、查了哪裡；每筆異常一筆回饋、事故變 confirmed；沒登入、漏報沒入口 |
| [phase5-learning-loop/2026-10-fix-critical.md](phase5-learning-loop/2026-10-fix-critical.md) | ⑥⑧⑨ 修四個會讓整輪失敗或蓋掉資料的問題：LLM 不擋告警、回測時區、事故不被蓋（scripted upsert）、alert 時限 | 各補測試、修前重現過；docker ES 實測確認過的事故不被蓋；LLM 時限仍是估的 |
| [phase5-learning-loop/2026-10-fix-feedback-ux.md](phase5-learning-loop/2026-10-fix-feedback-ux.md) | ⑨⑦ 漏報建議被吃掉、網頁送出失敗沒回應、摘要變空白；**真的 LLM 第一次跑** | 三個修了各有測試；OpenAI 實跑 12 次：預算要 2000、告警本身要有 id 才有依據可引，修後 6/6 通過、5～8 秒一件 |
| [phase5-learning-loop/2026-10-grafana-form.md](phase5-learning-loop/2026-10-grafana-form.md) | ⑧⑨ 回饋表單放進 Grafana（Business Forms 外掛） | **定案用這個**（外掛放資料夾、不換 image，小網頁當備案）；Grafana 12.2 實測：預填、送出寫回 ES、誤報路徑都對；12.2 只能用外掛 6.3.1；回饋服務網址寫死、要開 CORS；身分還是自己填 |
| [phase5-learning-loop/2026-10-grafana-plugin-init.md](phase5-learning-loop/2026-10-grafana-plugin-init.md) | ⑧⑨ Grafana 在 K8s 上用 initContainer 裝回饋表單外掛 | 從 Nexus 下載、核對 SHA-256、解到 emptyDir；docker 模擬驗過（裝、跳過、SHA 不符不裝、簽章 valid）；之後在 k3d 上跑過（[k3d-e2e](archive/phase1-collect/2026-10-k3d-e2e.md)） |
| [phase5-learning-loop/2026-11-grafana-plugin-lfs.md](phase5-learning-loop/2026-11-grafana-plugin-lfs.md) | ⑧⑨ 回饋表單外掛 zip 用 git lfs 放進 repo（`grafana/plugins/`）；`load-plugin.sh` 切成 ConfigMap 帶進 cluster，chart `plugin.source: configmap` 時 initContainer 接回、核對、解開，不連 Nexus | 真 zip 裝進測試 Grafana，表單預填與送出都走通（寫入 `aiops-feedback`、`aiops-incidents`） |
| [phase5-learning-loop/2026-11-grafana-official-values.md](phase5-learning-loop/2026-11-grafana-official-values.md) | ⑧ `grafana/code/` 的 provisioning 檔合成 Grafana 社群 chart（`grafana-community/grafana`，原 `grafana/grafana` 已棄用）的 values（`grafana/values-official.yaml`，腳本產生、不手改） | 測試 cluster 實裝：4 個 data source、3 條告警規則、3 個通知管道、分送規則、3 張儀表板都載入；抓到並修：`alerting` 要 map、admin 密碼沒生效、Grafana key 只能讀 aiops-alerts、換 key 要重啟 |
| [phase5-learning-loop/2026-10-grafana-demo.md](phase5-learning-loop/2026-10-grafana-demo.md) | ⑦⑧⑨ Grafana 上看根因報告、填回饋、回饋後門檻變化（截圖） | 兩個唯讀儀表板；metrics→trace→log 報告齊全、表單送出、核准後批次異常 3 → 1 |
| [phase5-learning-loop/2026-10-loop-update.md](phase5-learning-loop/2026-10-loop-update.md) | ⑨④ 回饋回到系統：更新知識庫、門檻加回測 | `backtest`（現行 vs 建議重跑）、沒回測不能核准、漏報降 z、同根因 3 次規則草稿；誤報 3 次 → 回測 → 核准 → 不再報實測過；月底批次放寬 z 是治標 |
| [phase5-learning-loop/2026-10-metrics-kpi.md](phase5-learning-loop/2026-10-metrics-kpi.md) | ⑨ 量化：學之前、學之後的數字 | `metrics` 指令兩段並排；模擬資料驗算法，真實數字等試點 |
| [phase5-learning-loop/2026-10-rule-threshold.md](phase5-learning-loop/2026-10-rule-threshold.md) | ⑨④②⑧ 規則草稿把門檻算出來、核准就自動載入；行事曆拿掉國定假日、歲修一筆一筆；第一階段只開 4 個 CronJob | 門檻 = 各件最低異常值的最小值（峰值實測不觸發）；核准後 `rules` 載入、第 2 次事故時間觸發；沒有過期、只處理延遲類 |
| [phase5-learning-loop/2026-11-suggest-and-rule-backtest.md](phase5-learning-loop/2026-11-suggest-and-rule-backtest.md) | ⑨ 誤報建議依時間、偏差選週期放寬／`min_abs`／z；「升級成規則」核准前也要回測 | 已完成 |
| [phase5-learning-loop/2026-10-feedback-nl-extract.md](phase5-learning-loop/2026-10-feedback-nl-extract.md) | ⑨⑦ POC 任務 2：回饋一句話的 LLM 抽取（四欄 → `aiops-tuning` proposed）＋採納率兩題 | 26 句語料：真的模型（gpt-6-luna）四欄全對 26／26，關鍵字退路 22／26（錯在誤報措辭沒關鍵字）；驗不過只存原文；`suppress_during_cron` 的 `schedule` 不猜、要人補；`metrics` 多採納率與抽取率 |

### 跨階段（`meta/`）

整條線的盤點、文件對程式、目錄改名

| 檔案 | 主題 | 結論 |
|---|---|---|
| [meta/2026-10-docs-vs-code.md](archive/meta/2026-10-docs-vs-code.md) | ①～⑨ 文件對程式：整條線一份看完（[architecture.md](../architecture.md)）、總覽狀態對齊 | 只改文件；6 處對不上改掉 5 處；code-review 的 10 項對過程式，4 項要修、記在流程文件的「不足」 |
| [meta/2026-10-restructure.md](archive/meta/2026-10-restructure.md) | 全部 目錄改名：專案名 it-aiops | `platform/`→`telemetry/`、`aiops/`→`engine/`、`apps/`→`sdk/`（2026-11 又改回 `apps/`）、`test/`→`e2e/`；Python 套件改名；行為不變，200 個單元測試與連結檢查過 |
| [meta/2026-10-restructure-components.md](archive/meta/2026-10-restructure-components.md) | 全部 依部署元件重整：`collector/`、`elasticsearch/`、`grafana/`、`aiops-engine/` 各有 `code/`＋`helmchart/`；部署一律 Helm，kustomize、純 yaml、`e2e/local-k3d/` 刪掉；`e2e/local-k3s` 改名 `e2e/k8s` | chart render 跟舊 manifest 逐資源比對等價；Rancher Desktop 刪光重裝：log 54、metrics 46、trace 24 全過；rca-demo、schedule、es-2node 等五支改了部署指令的腳本沒實跑 |
| [meta/2026-10-sonar.md](meta/2026-10-sonar.md) | 跨階段 SonarQube 第一次掃：範圍固定、真的問題修掉、剩的寫清楚 | `e2e/` 不掃；bug 5 → 0、vulnerability 121 → 4（剩 4 個都有理由）；`doc_id` 不變、https 選用 |
| [meta/2026-10-diataxis.md](meta/2026-10-diataxis.md) | 跨階段 共用文件採用 Diátaxis：每份只做一種事（原理／操作／規格／案例） | 7 份混合文件拆成 19 份；檢查腳本擋沒標類型的；連結 0 斷；內容沒掉 |
| [meta/2026-10-diataxis-baseline.md](meta/2026-10-diataxis-baseline.md) | ④ baseline 文件依 Diátaxis 拆開（715 行 → 原理／規格／操作／案例四份） | 以段落為單位歸類；內容 585 行全在；46 條連結改指新位置；0 個斷 |
| [meta/2026-10-product-line-table.md](archive/meta/2026-10-product-line-table.md) | 跨階段 「產品主線 8 步」表改成 Phase × 模組（一列一個模組） | 8 步拆成 13 列；步 1～8 都在；模組對應的 Phase 跟第 7 節一致 |
| [meta/2026-10-open-issues.md](meta/2026-10-open-issues.md) | 跨階段 討論累積的未決問題：trace 合併、偵測時間、快速通道、一直壞著的異常、回饋調整、根因分析限制、基準線與調整索引的備份、相似事故檢索往哪裡走 | 未立案；只整理現況與傾向；延遲數字是推算，沒有量測 |
| [meta/2026-10-compat-es8-grafana13.md](meta/2026-10-compat-es8-grafana13.md) | ③⑧ 公司版本 ES 8.19.23、Grafana 13.0.7 相容性實測 | 8.19 沒有 `_otlp` 入口，Collector 出口要改 `elasticsearch` exporter（實測寫入後 pipeline、形狀都一樣）；其餘原樣可用；Grafana 13 外掛 6.3.1／6.3.5 都能裝、表單送出 OK |
| [meta/2026-10-collector-es-exporter.md](archive/meta/2026-10-collector-es-exporter.md) | ①③ Collector 出口改 elasticsearch 寫入元件（ES 8.19 沒有 `_otlp`），重測 ES 停機、沒回應 | 三輪實測：預設設定 ES 停 180 秒掉 58%；改 `retry` 一直重試、`timeout: 0`、`retry_on_status` 含 401 後掉 3%（只剩連線黑洞那 10 秒）、沒回應 90 秒 0 遺失 |
| [meta/2026-10-pin-es8-grafana13.md](meta/2026-10-pin-es8-grafana13.md) | ①③⑧ manifest 與文件釘選成 ES 8.19.23、Grafana 13.0.7；補測 ES 429、Collector 重啟、多節點、完整故障測試、Grafana 13 通知 | Grafana 13 通知全過；Collector 當掉／換版／sampler 重啟 0 遺失；ES 429、多節點、PVC 塞滿、ES 停機 600 秒、真正 OOM 沒測到 |
| [meta/2026-10-metrics-es8-shape.md](meta/2026-10-metrics-es8-shape.md) | ①③④ metrics 測試、文件、`hist_count`、合成資料測試對齊 ES 8.19 的直方圖存法（`histogram`、`values`＋`counts`）；Collector 把直方圖的 sum 另抽成 `_sum` | k3d 上 metrics e2e 45 項、規則邊緣 156 項全過；原本「每筆 ≤ 140」是空跑（讀錯欄位）已修；`jvm.gc_overhead` 改讀 `_sum`；`baseline_edge_e2e` 的 log 段仍走 `/_otlp`（8.19 跑不了） |
| [meta/2026-10-e2e-es8-rerun.md](meta/2026-10-e2e-es8-rerun.md) | ①②③④⑦ `e2e/local-k3s` 在 k3d＋ES 8.19.23 逐支重跑，失敗的分清測試假設／環境／產品問題 | 9 支有結果（metrics 46、keys 84、trace-id 15、rca 11、metrics-trace 16、trace-baseline 9 全過）；修 2 處測試的假設（時間格式、節點名）；`scenarios label` 的「通過」是假的、`outage`（停 ES 中途重啟 Collector）掉資料待查；後 9 支因 Docker 中斷沒跑 |
| [meta/2026-10-merge-architecture.md](archive/meta/2026-10-merge-architecture.md) | 跨階段 `architecture.md` 與 `architecture-reference.md` 合成一份 | 章節編號本來就接續，全部原樣併入；連結改指新位置（含 PDCA 紀錄的 12 個，只改目標）；`check-docs.py` 0 個問題；`architecture.md` 變 460 多行 |
| [meta/2026-10-merge-topic-docs.md](archive/meta/2026-10-merge-topic-docs.md) | 跨階段 `metrics`、`log`、`rca`、`trace`、`feedback` 共 23 份合成各一份 README，每份 1500～2000 字 | 刪 18 份、改寫 5 份；全 repo 連結改指新位置；對程式修正 8 處與現況不符的敘述；欄位表、指令、逐案例數字刪掉（在 git 歷史） |
| [meta/2026-10-feedback-readme.md](archive/meta/2026-10-feedback-readme.md) | ⑨ `docs/feedback/README.md` 縮成閉環步驟與介面 | 留兩張閉環表、三種介面（Grafana 表單附圖、回饋網頁、指令）與表單選項對應 `verdict`／`incidents.status`；LLMOps、紀律、LLM、現況全刪（在 git 歷史） |
| [meta/2026-10-metrics-trace-readme.md](archive/meta/2026-10-metrics-trace-readme.md) | ①②③ `docs/metrics/README.md`、`docs/trace/README.md` 改成流程圖加四個重點 | 各一張圖、種類與來源、清洗步驟、壓縮與保留、共同鑰匙圈與自己的欄位；能做到與做不到、.NET 比較、案例、六個場景全刪（在 git 歷史） |
| [meta/2026-10-retention-split.md](archive/meta/2026-10-retention-split.md) | ③ retention 三份只留三種資料共用的部分，各資料的細節移進 log、metrics、trace README | 共用留原則、差別表、容量估算、保留一覽、套用與量測方法；metrics 的降解析度與每筆大小、log 的 EAP 與降量、trace 的保留依據與取樣做法各移進自己的 README |
| [meta/2026-10-log-readme.md](archive/meta/2026-10-log-readme.md) | ①②③ `docs/log/README.md` 改成流程圖加四個重點（樣式格式、什麼時候與怎麼清洗、多久壓縮） | 圖＋格式表＋欄位名稱表＋兩個前後對照例子＋壓縮與保留時間表；Fluent Bit 比較、部署與換 key、現況全刪（在 git 歷史）；例子沒實際送一行驗證 |
| [meta/2026-10-data-reference.md](meta/2026-10-data-reference.md) | ④～⑨ 新增 AIOps 資料格式規格（aiops-alerts／anomalies／incidents／feedback／tuning 的欄位、誰寫、範例） | 欄位對 mapping 無漏；兩輪對程式與實際 ES mapping 共修正 14 處不精確；欄位、型別、列舉、範例、儀表板腳本比對全部一致；欄位與 mapping 仍沒有自動比對 |
| [meta/2026-10-docs-governance.md](meta/2026-10-docs-governance.md) | 跨階段 文件治理：索引＋狀態標籤、設定與原理分離、術語寫進 CLAUDE.md、檢查腳本 | `docs/README.md` 地圖、`CLAUDE.md`、`tools/check-docs.py`；活的文件舊說法 0；pdca 根目錄雜項收進 `meta/` |
| [meta/2026-11-slim-repo.md](meta/2026-11-slim-repo.md) | ③⑧ 移除 GitLab CI yaml；盤點可精簡的 yaml 與不必要的檔 | 進行中；先刪 CI，其餘只盤點 |
| [meta/2026-11-frameworks.md](meta/2026-11-frameworks.md) | ④～⑨ engine 換三個框架：pydantic 驗設定、typer 命令列、FastAPI api | 747 個單元測試過；31 條排程表字面指令、api 的 bytes 比對守住行為不變；`--match a b` 要改成 `--match a --match b`；k3d 沒部署 |
| [meta/2026-11-oop-core.md](archive/meta/2026-11-oop-core.md) | ④⑤⑦ 核心邏輯物件導向化（一）：ES 實作 Backend 刪 18 處 `is_backend`；`Anomaly` 與 `Detector` 基底，rules／peer／quality 繼承 | 769 個單元測試過；`is_backend` 0 處；五種異常文件樣式與舊程式逐鍵比對相同；總行數 +189（搬與去重，沒刪）；沒連 ES |
| [meta/2026-11-vo-models.md](archive/meta/2026-11-vo-models.md) | ④～⑨ 資料物件化（VO）：盤點在函式之間流動的 dict（Alert、Incident、Fingerprint、Span、Findings 等 11 項），分 P0／P1／P2 | 進行中；步驟 1 `Fingerprint`、`Incident`（`core/incident.py`）做完，四條寫入路徑改走 `to_doc()`，773 個單元測試過、golden 逐欄位比對只差 null 不再寫 |
| [meta/2026-11-clean-code.md](meta/2026-11-clean-code.md) | ②～⑨ 程式整理：A 一個階段一個資料夾（⑨ → `feedback/`）、B 拆大函式與子命令、C 依賴只往一個方向＋`test_layers`、D 跨階段文件改模型 | 四步都做完（分支 `claude/clean-code`）：785 個單元測試過、`test_layers` 鎖依賴方向；函式 >100 行 2 → 0、函式內 import 71 → 3、反向依賴 5 → 0；Alert 的 golden 比對只差 null 不再寫 |
| [meta/2026-11-target-architecture.md](archive/meta/2026-11-target-architecture.md) | ②～⑨ 目標架構：core（連線、entity、repository）／scenario／service／interface 四層，逐檔搬遷表與 S0～S5 步驟 | 已完成；已合進 master |
| [meta/2026-11-feature-folders.md](meta/2026-11-feature-folders.md) | 程式改成一個功能一個資料夾：detect／learn／incident／llm／shared，逐檔來源與依賴順序 | 已完成；已合進 master |
| [meta/2026-11-apm-overview-dashboard.md](meta/2026-11-apm-overview-dashboard.md) | ⑧ 整合儀表板：APM 的 metrics → trace → log 接事故與根因，一頁看完（重做被刪掉的示範儀表板） | 已完成；已合進 master |
| [meta/2026-11-config-settings.md](meta/2026-11-config-settings.md) | 設定集中管理：環境變數 Settings、參數 yaml 回 Pydantic 模型（不倒回 dict）、時間統一，附設定盤點與順序 | 已完成，合回 master |
| [meta/2026-11-docs-cleanup.md](meta/2026-11-docs-cleanup.md) | 整理舊的 md：29 份共用文件逐份盤點（保留／小修／改寫）、112 份 PDCA 紀錄的處置、要先問使用者的三件事 | 已完成 |
| [meta/2026-11-source-adapter.md](meta/2026-11-source-adapter.md) | ①②④⑦ 收值（讀的那一側）模組化：OTel、APM 各一個來源模組，可選、之後可並存；先盤點來源相關程式在哪、分三階段、要問使用者的事 | 已完成（階段 0～3：拆 `data/`、來源適配器、自我檢查、並存） |
| [meta/2026-11-mapping-ui.md](meta/2026-11-mapping-ui.md) | ②⑧ 欄位對照在網頁上調：存容器 yaml／內建 SQLite／ES／git PR 比較，進版會不會被洗掉；建議 yaml 當基準、ES 存覆寫；第一版做了歸屬與 log（網頁提、命令列核准、核准前對真的資料試） | 已完成第一版 |
| [meta/2026-11-mapping-trace.md](meta/2026-11-mapping-trace.md) | ② 欄位對照的覆寫擴到 trace：合成時部門、群組用歸屬的欄位名；raw 索引網頁提、命令列核准（核准前驗格式） | 已完成 |
| [meta/2026-11-cases-tier-drop.md](meta/2026-11-cases-tier-drop.md) | ④⑥⑦ 歷史 case 補根因分析（線索排序、先查清單、考卷）、P0 服務升嚴重度、baseline 往下掉提重新學 | 已完成 |
| [meta/2026-11-module-map.md](meta/2026-11-module-map.md) | 文件：①～⑨ 每階段功能拆解與流程圖（module-map.md） | 已完成 |
| [meta/2026-11-postgres-store.md](meta/2026-11-postgres-store.md) | ③⑦⑧⑨ engine 的資料從 ES 搬到 PostgreSQL（一次切過去）、相似事故加向量檢索、Grafana 改讀 PostgreSQL | 已完成（公司的 PostgreSQL 還沒申請） |
| [meta/2026-10-poc-acceptance.md](meta/2026-10-poc-acceptance.md) | ①～⑨ POC 驗收計畫：6 個 KPI、4 個故障情境、Go/No-Go | 四個任務已合入 master；模擬服務、合成資料上：聚合率 100%、解析率 100%、串查 ✅、召回率 50%（要真實事故單）；Go/No-Go 待真實服務 |
| [meta/2026-11-apm-source.md](meta/2026-11-apm-source.md) | ④⑤⑦ engine 讀 Elastic APM 的資料（不改 Collector、ES）：`configs/apm/`、`source: apm`、序列 `scale`／`div`、trace 的 APM 轉換 | 562 個單元測試過（新加 17 個：APM span 轉換、查詢欄位、scale／div、owner／log 欄位）；chart 兩種 source render 對；沒有真實 APM 資料，整條沒在 ES 上跑；例外吞噬、Pod 資源、往外呼叫 p95 在 APM 資料上做不到 |
| [meta/2026-11-remove-collector.md](meta/2026-11-remove-collector.md) | ①②③ 刪自建收值：`collector/`、`apps/`、`e2e/`、ES 的 OTel 入口設定與文件；`elasticsearch/` 縮成只補 `traces-merged-*` 與兩把 key | 刪約 200 個檔；連結檢查從 80 個壞連結修到 0；engine 的 OTel 來源沒動；瘦身後的 setup 腳本與 chart 沒對真實 ES 跑 |
| [meta/2026-11-grafana-files-in-chart.md](meta/2026-11-grafana-files-in-chart.md) | ⑧ Grafana 的 provisioning 檔（data source、告警、儀表板）搬進 helm chart，一行 helm 就裝好；事故下拉在 Grafana 13 空的修正、事故清單寫明一列一件 | 渲染、實裝進新 namespace、重啟後儀表板自己回來都驗過；外掛 initContainer 與 GitLab CI 沒跑 |
| [meta/2026-11-model-modules.md](archive/meta/2026-11-model-modules.md) | ④⑥⑦ 五個模型拆成模組：資料來源介面（ES／檔案／HTTP／程式直接給）＋萬用資料處理器整成各模型的標準輸入；train／detect／peer／rules 改走來源介面 | `sources/`（Backend：memory／file／http 來源＋memory／file／es Store）、`models/`（五個門面）、各讀寫函式開頭「是 Backend 就交給它」；562 個既有測試不動全過、新加 31 個；shape 跟 ES 比：8 種 value 完全一樣、直方圖的 percentile 差 2%、hist_ratio_above 差到 9%（ES 內插）；HTTP 來源只用假 fetch 測 |
| [meta/2026-11-case-classify.md](meta/2026-11-case-classify.md) | ⑦⑨ case 只做告警分類：指紋投票的分類器（信心、留一法準確率、學權重）、報告／摘要／通知只給分類、表單預帶、`import-incidents --db`、precision 自動提門檻建議 | 投票＋信心寫進 aiops-alerts，報告／摘要／通知不再列 case 的根因、處置；留一法準確率、≥ 50 件才學權重；sqlite／SQLAlchemy 讀事故單；606 個測試過；真 DB、真事故單沒跑 |
| [meta/2026-11-semiconductor-design.md](meta/2026-11-semiconductor-design.md) | ④⑥⑦ 半導體（FDC）設計文件：lot 路徑取代 trace id、五個模型與目前設計的對照 | 只有文件、無 code；欄位名是假設，等 MES／FDC 樣本 |
| [meta/2026-11-severity-grading.md](meta/2026-11-severity-grading.md) | ⑥⑧ 嚴重度分級 critical／high／warning：訊號廣度（幾類黃金訊號）＋持續（滑動窗）＋換版上限；Grafana 只通知 critical、high | 合成 5 情境 7 件：改前全 warning → 改後 critical 2、high 1、warning 4，通知管道 0 誤報 0 漏報；Grafana 靜音沒實際 provision |
| [meta/2026-11-suggest-features.md](meta/2026-11-suggest-features.md) | ⑨ 回饋提建議改成特徵查表（時段固定 × z 壓得掉 → mute／那 1 小時 z／整條 z／min_abs）、讀老師傅的原因、不累積、回測擋無效建議 | 同一批 28 筆標記：舊迴圈按完 61% → 61%；新迴圈 61% → 21%、真的 11 筆沒掉；迴圈仍沒排程 |
| [meta/2026-11-backtest-group.md](meta/2026-11-backtest-group.md) | ⑨ 回測改成同一對象的建議整組測、範圍命中、逐筆剔除；結論分有效／無效／無法判斷／有害 | 假資料驗四種結論；測試 ES 上 report-svc 兩筆一組：min_count 必要、min_abs 多餘，核准多餘的被擋；誤報率 61% → 21% 不變 |
| [meta/2026-11-factory-engine.md](meta/2026-11-factory-engine.md) | ④⑤⑥⑦⑨ 工廠版 engine：復用程式、另一套設定 `configs/factory/`；ML／FDC 的異常接進 ⑤（`ingest`）、生產履歷（MES＋S6F11）合成 `lot-paths-*` 取代 trace（`paths-merge`）、後站依 lot 併進前站、嚴重度依參數家族、recipe 變更當換版、機台 alarm／PM／recipe 變更當 log、誤報匯出給 ML | 假資料 728 個 lot、兩個事故 e2e 15/15：ETCH-01 critical、METRO-01 併進它、報告慢在 CH-B、共同點 CH-B、事故前的 PM；CVD-01 換版 warning；671 個測試過；欄位全是假設 |
| [meta/2026-11-plugin-architecture.md](meta/2026-11-plugin-architecture.md) | ④⑥⑦⑨ engine 改成主程式＋plugin：`core/plugin.py` 擴充點（欄位對照、用語、實體 keys、併件依據、換版、報告段、子命令、排程）、`plugins/` 三個內建（observability／factory／metrics_only）、`AIOPS_PLUGIN` 選、RCA 報告拆段 | 682 個測試過（新 11）；工廠 e2e 15/15 不變、**只有指標 e2e 第一次跑 13/13**（沒履歷：報告略過履歷段、log 照出；量測異常併不進前站是已知限制）；chart 三種 plugin render 對；`AIOPS_DOMAIN` 移除 |
| [meta/2026-11-api-beat-worker.md](meta/2026-11-api-beat-worker.md) | ⑤⑥⑦⑧⑨ engine 拆成 api／beat／worker 三種角色（Superset 式）：一個 image、一份 values.yaml 用 `mode` 選一隻 pod 或分散；Celery＋Redis 佇列 `realtime`／`alert`／`batch`；`POST /api/anomalies`（ML／FDC 即時推進 ⑤）、`POST /api/rca`（Grafana 按鈕重跑根因）；畫面全交給 Grafana | 進行中 |
| [meta/2026-11-html-forms.md](meta/2026-11-html-forms.md) | ⑧⑨ 刪本機 sh（`load-plugin.sh`）與外掛 zip、社群 chart 副本；Grafana 回饋預設改用 engine api 的網頁（回饋頁加重跑根因分析、新漏報頁），外掛選用；修工廠版沒有履歷時報告整份失敗 | 720 個測試過；測試 cluster 實裝：儀表板連結帶出 alert id、網頁重跑根因 → worker 重跑、報告不再「失敗：aggregations」 |

## 封存（`archive/`）

描述的東西已經不在 repo 的紀錄，原樣搬到這裡（`git mv`，內容不改，連結已改到新位置）；用 `git log --follow` 看得到原本的歷史。

| 位置 | 份數 | 為什麼封存 |
|---|---|---|
| `archive/phase1-collect/` | 19 | 自建收值（Collector、埋點、ES 的 OTel 入口）2026-11 已刪（[remove-collector](meta/2026-11-remove-collector.md)） |
| `archive/meta/` 的 10 月文件整理 | 10 | `2026-10-restructure*`（`code/`＋`helmchart/` 的舊目錄）、`2026-10-merge-*`、`feedback-readme`、`log-readme`、`metrics-trace-readme`、`product-line-table`、`retention-split`、`docs-vs-code`：描述的文件配置（`docs/log/`、`docs/metrics/`…）已經不是現在的 |
| `archive/meta/` 的舊 Collector 與中間態程式結構 | 5 | `2026-10-collector-es-exporter`；`2026-11-target-architecture`（core／scenario／service／interface 四層）、`oop-core`、`vo-models`、`model-modules`：被後來的「一個功能一個資料夾」取代（現在的結構看 [feature-folders](meta/2026-11-feature-folders.md) 與 [aiops-engine/README.md](../../aiops-engine/README.md)） |

