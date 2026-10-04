# Plan — 依 `tenant` label 寫進對應的 AccountID

> `2-spec.md` 已確認。分三批，前一批驗收過才做下一批，每批一個 commit。

## 第一批：`group` 改名為 `tenant`

只改名字和允許的值，分流機制先不動。做完 `./tests/validate.sh` 要 `PASS`。

1. 四個 `values.yaml` 的 `observability.group` 改成 `tenant`，值照 spec 的對照。
2. `platform/applicationset.yaml` 與 `eqp-collector` 的 `namespace.yaml`：抄的欄位清單 `group` 改 `tenant`。
3. `platform/vmstack-values.yaml` 與 overlay：抓取時貼的 label、`kube-state-metrics` 的 allowlist 改名。
4. `tests/validate.sh`：欄位名、自我檢查的案例改名；寫 `group` 要 FAIL 並提示已改名。

| 檔案 | 改什麼 |
| --- | --- |
| `applications/mes-dispatch/helm/values.yaml` | `tenant: icsd` |
| `applications/yms-sim/helm/values.yaml` | `tenant: imc` |
| `applications/eqp-collector/helm/values.yaml` | `tenant: imc` |
| `applications/metrics-sim/helm/values.yaml` | `tenant: platform` |
| `applications/eqp-collector/helm/templates/namespace.yaml` | 欄位清單 |
| `platform/applicationset.yaml` | 欄位清單 |
| `platform/vmstack-values.yaml` | 抓取 relabel、allowlist |
| `platform/vmagent-routing-overlay.yaml` | 跟上面一致 |
| `tests/validate.sh` | 欄位名與自我檢查 |

⚠️ 這一批做完時 `remoteWrite` 還是舊的三條，過濾條件對的是 `it` / `imc`。
`icsd` 的資料會暫時落到兜底那條。這是中間狀態，第二批收掉，不能單獨上線。

## 第二批：對照表、防偽造、單條 `remoteWrite`

1. `platform/vmstack-values.yaml`：抓取時沒貼 `tenant` 的 namespace 一律補成 `platform`，讓每個 target 都有 `tenant`。
2. 同一份：送出前的全域 relabel。先清掉進來的 `vm_account_id`，再照對照表填，對不到的改成 `platform` / 1000。
3. 同一份：三條 `remoteWrite` 換成一條，multitenant 路徑，一把 token。路徑標明是佔位。
4. overlay 跟著改，允許的欄位多一個全域 relabel。
5. `tests/validate.sh`：允許的 `tenant` 改成從對照表讀；檢查 AccountID 不重複、`platform` 兜底那條在最後、清 `vm_account_id` 那條在最前、`remoteWrite` 剛好一條。每一條都加改壞的自我檢查。
6. 兩份 `README.md` 改寫 `group` 相關的說明；`.agents/project/commands.md` 標明兩支舊測試壞了。

| 檔案 | 改什麼 |
| --- | --- |
| `platform/vmstack-values.yaml` | 步驟 1 ~ 3 |
| `platform/vmagent-routing-overlay.yaml` | 步驟 4 |
| `tests/validate.sh` | 步驟 5 |
| `README.md` | 分流說明、`values.yaml` 範例、Known limitations |
| `applications/eqp-collector/README.md` | `group` 的 30 處 |
| `.agents/project/commands.md` | `tests/routing`、`tests/ap1105-sim` 標為已過期 |

## 第三批：k3d 實測

1. 新的 `tests/tenant-routing/`：`run.sh` 建獨立的 k3d cluster `tenant-test`，全程帶 `--context k3d-tenant-test`。
2. 裝 operator v0.74.0、一座 VictoriaMetrics cluster、vmauth v1.148.0（驗 token 後轉給 vminsert）。
3. vmagent 的設定取自 `platform/vmstack-values.yaml`，只換網址，跟 `tests/routing` 的做法一樣。
4. 用手動 Helm 部署 `eqp-collector` 的 chart 六次，各換 `namespace` 與 `tenant`：四個租戶、沒貼、打錯。
5. 偽造的那個另外做：一個沒貼 `tenant` 的 namespace 裡放一個只回固定文字的小 pod，吐的 metric 自己帶 `tenant="tsid"` 和 `vm_account_id="1001"`。定義寫在 `backends.yaml`。
6. `check.py` 逐個 AccountID 查：該有的有、不該有的沒有。
7. 改壞對照表的複本再跑一次，要 FAIL。做法跟 `tests/routing` 的 `VMSTACK_VALUES=<改壞的複本>` 一樣。
8. `README.md` 的「Tests」與 `.agents/project/commands.md` 補上這支。

| 檔案 | 改什麼 |
| --- | --- |
| `tests/tenant-routing/run.sh` | 新增 |
| `tests/tenant-routing/backends.yaml` | 新增：VictoriaMetrics cluster、vmauth |
| `tests/tenant-routing/check.py` | 新增 |
| `README.md`、`.agents/project/commands.md` | 補上新測試 |

## 風險與退路

| 風險 | 怎麼發現 | 退路 |
| --- | --- | --- |
| operator v0.74.0 的 VMAgent CRD 不認得全域 relabel 的欄位，被默默裁掉 | 第三批用 server-side dry-run 比對送進去和存下來的 spec，不用 `kubectl explain` | 改成寫在每條 `remoteWrite` 的 relabel 裡。第二批的 YAML 要重寫 |
| 這個版本的 vminsert 不支援從 label 取 AccountID | 第三批：四個租戶的資料全部出現在 AccountID 0 | 退回每個租戶一條 `remoteWrite`。這會推翻 spec，要回頭重談 |
| 偽造用的小 pod 需要一個能回固定文字的 image | `eqp-collector` 的程式只吐固定的 label，已確認不能拿來偽造 | 用本機已有的 image 掛 ConfigMap 回文字，不改 `services/` 底下任何程式 |
| `relabel` 的「對不到就兜底」寫反，正常租戶也被改成 `platform` | 第三批：四個租戶的 AccountID 是空的、1000 爆量 | 第二批的自我檢查先擋一層 |
| 第一批的中間狀態被拿去上線 | `git log` 只有第一批的 commit | commit 訊息與 PR 說明寫明三批要一起上 |
| 本機資源不夠再開一個 k3d cluster | `k3d cluster create` 失敗或 pod 一直 Pending | 請你先停掉 `k3d-spil`。我不會自己停 |
| 改 README 時漏掉某處 `group` | `grep -n -w group` 兩份 README，剩下的每一處要能解釋 | 無 |

## 不在這個 plan 裡的

| 不做 | 為什麼 |
| --- | --- |
| 修 `tests/routing`、`tests/ap1105-sim` | 見 `1-intent.md` |
| 改 `tests/ap1105-sim/README.md` | 它記錄的是舊分流的實測結果，是歷史 |
| `site`、第二條 `remoteWrite`、PVC | 需求 3 |
| 把三個走 ArgoCD 的 app 改成手動 Helm | 另一個需求。第三批用 `eqp-collector` 的 chart 測就夠 |
| 動 `platform/fluent.conf` | log 不在範圍。裡面的 `group` 是 Fluentd 自己的欄位，不是這個 label |
| 順手修 README 既有的 md 格式違規 | 另一個改動 |
