# Acceptance — 兩廠雙寫

> 每一項都要是**可執行的指令加預期輸出**。分批驗收，前一批全過才做下一批。

## 第零批：先確認地基（已做）

結果列在 `2-spec.md` 的「先實測過的」，八項都成立。

## 第一批：設定與靜態檢查

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 1-1 | `./tests/validate.sh; echo "exit=$?"` | `PASS`、`exit=0`；第 2 段列出兩個目的地與兩個廠區的檔 | ☑ |
| 1-2 | `./tests/validate.sh \| grep "自我檢查" \| grep -E "只剩一條\|網址一樣\|同一把\|statefulMode\|上限\|PVC"` | 六條都在、都是 `OK`：每一種改壞都被擋下 | ☑ |
| 1-3 | `./tests/validate.sh \| grep "自我檢查" \| grep -E "site"` | 廠區檔的三種改壞（多帶設定、值不在清單、檔名跟值不一致）都被擋下 | ☑ |
| 1-4 | `./tests/validate.sh \| grep -E "自我檢查\|→" \| grep -c FAIL` | `0` | ☑ |
| 1-5 | `helm template t vm/victoria-metrics-k8s-stack --version 0.91.0 -f platform/vmstack-values.yaml -f platform/site-ctsp.yaml \| grep -A3 "externalLabels"` | 渲染出來的 VMAgent 有 `site: ctsp` | ☑ |

## 第二批：k3d 實測

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 2-1 | `./tests/tenant-routing/run.sh; echo "exit=$?"` | 七個情境在兩座都只出現在該去的 AccountID；每筆帶 `site=ctsp`；沒有 `site` 的樣本 0 筆；`PASS`、`exit=0` | ☐ |
| 2-2 | `./tests/tenant-routing/run.sh outage; echo "exit=$?"` | 停機中站 2 照常、站 1 沒有新資料；vmagent 重啟後佇列還在；恢復後站 1 停機期間的筆數 ≥ 站 2 的 95%；`PASS`、`exit=0` | ☐ |
| 2-3 | `VMSTACK_VALUES=<沒開 statefulMode 的複本> ./tests/tenant-routing/run.sh outage; echo "exit=$?"` | 補送那一項 `FAIL`（重啟時佇列消失）；`exit=1` | ☐ |
| 2-4 | `VMSTACK_VALUES=<只剩一條 remoteWrite 的複本> ./tests/tenant-routing/run.sh; echo "exit=$?"` | 站 2 沒有資料的項目 `FAIL`；`exit=1` | ☐ |
| 2-5 | 2-2 跑完後 `kubectl --context k3d-tenant-test -n tr-central get deploy vmauth -o jsonpath='{.spec.replicas}'` | `1`：站 1 的入口有開回來 | ☐ |
| 2-6 | 跑 2-1 前後各一次 `kubectl config current-context` | 一樣 | ☐ |

## 第三批：文件

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 3-1 | `grep -n -E "一條 .?remoteWrite\|只有一條" README.md applications/eqp-collector/README.md` | 沒有殘留「只有一條」的說法 | ☐ |
| 3-2 | `python tools/check-md-style.py README.md applications/eqp-collector/README.md` | 違規數不比改之前多（4 和 4） | ☐ |
| 3-3 | `python tools/check-md-style.py requirements/dual-site-write/*.md .agents/project/commands.md` | `✅ 全部符合` | ☐ |

## 人工確認

| # | 怎麼做 | 看到什麼算過 | 結果 |
| --- | --- | --- | --- |
| 1 | 開線上的架構圖，切到第二張 | 標示從「規劃中」改成已實測，並註明測試範圍 | ☐ |
| 2 | 讀 `platform/vmstack-values.yaml` 的 vmagent 那一段 | 看得懂套之前要先確認什麼：StorageClass、兩把 token、網址是佔位 | ☐ |

## 已知未涵蓋

| 沒證明 | 為什麼 |
| --- | --- |
| 沒有 StorageClass 的 cluster 會怎樣 | k3d 有預設的。只寫進文件 |
| 暫存超過上限 | 要灌滿 20 GiB |
| 20 GiB 撐不撐得住 8 小時 | 要在正式的 cluster 量每小時的暫存量 |
| 兩個廠區各一套 vmagent 同時跑 | 本機只起一套，`site=ctsp` |
| 真正的跨廠斷線 | 用「站 1 的入口關掉」代替，對 vmagent 是同一件事 |
| 中央真正的 vmauth 與網址 | 佔位，待 Infra 確認 |
| AP1105 上 Deployment 換 StatefulSet 的過程 | 需求 4 |

## 寫回文件

| 改了什麼 | 動到哪份文件 | 狀態 |
| --- | --- | --- |
| 一條 `remoteWrite` 變兩條、多了 `site`、PVC | `README.md`、`applications/eqp-collector/README.md` | ☐ |
| 新的 `run.sh outage` | `.agents/project/commands.md`、`README.md` 的 Tests | ☐ |
| 「下一步」變成已實測 | `requirements/multi-site-metrics-write/architecture.html` | ☐ |
