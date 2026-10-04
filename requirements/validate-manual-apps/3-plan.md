# Plan — 驗證不走 ArgoCD 的 app

> `2-spec.md` 已確認。

## 步驟

1. 先留證據：在改之前跑 `./tests/validate.sh`，存下現在的輸出（第 3 段 traceback、exit 1）。
2. `tests/validate.sh` 加判定：有 `argocd:` 是走 ArgoCD；沒有 `argocd:` 而有 `templates/namespace.yaml` 是手動 Helm；兩個都有、兩個都沒有回錯誤。
3. `check()` 依判定分流：手動 app 不查 `argocd` 欄位，改查 `namespace` 必填；`observability` 的檢查兩種共用，不複製。
4. namespace 撞名的檢查：手動 app 的 namespace 取 `values.yaml` 的 `namespace`，跟 `argocd.namespace` 放進同一張表比。
5. 第 3 段加 `render_chart_labels()`：`helm template` 渲染 chart，取出 `kind: Namespace` 的 label；零個或多個 Namespace 回錯誤。手動 app 走這支，其他照舊走 `render_labels()`。
6. 第 4 段加兩條自我檢查：兩個都有、兩個都沒有，一定要被擋下。
7. 第 5 段加一組自我檢查：拿 `eqp-collector` 的 chart 換 `observability` 渲染，要照原值；沒寫 `system` 不能出現空 label。
8. 兩個方向都測：目前的 repo 要 `PASS`；把 `eqp-collector` 的複本各改壞一種，要 `FAIL`。改壞用暫存複本，不動 repo 裡的檔。
9. 改兩份文件裡過期的敘述。

## 會動到的檔案

| 檔案 | 改什麼 |
| --- | --- |
| `tests/validate.sh` | 步驟 2 ~ 7，以及檔頭註解的檢查項目說明 |
| `app/eqp-collector/README.md` | 「10. 跟其他 app 的差別」裡「`tests/validate.sh` 會失敗（已知、刻意保留）」那一條改成現況。ArgoCD 那一條警告保留 |
| `README.md` | 「Tests」的 `validate.sh` 說明補一句手動 app 的檢查；「新增一個 App」補上不走 ArgoCD 時 chart 要帶 `templates/namespace.yaml` |

⚠️ 動到 `tests/`。這是這個需求的本體，已在 spec 確認。

## 風險與退路

| 風險 | 怎麼發現 | 退路 |
| --- | --- | --- |
| 放寬之後，走 ArgoCD 的 app 忘了寫 `argocd:` 沒被擋下 | 步驟 6 的自我檢查：兩個都沒有一定要 FAIL | 自我檢查沒過，腳本本身就 exit 1 |
| 手動 app 的渲染檢查其實什麼都沒比到，永遠 OK | 步驟 8：把複本的 `namespace.yaml` 改成寫死的 label，要 FAIL | 修到改壞時會 FAIL 為止，不然不算完成 |
| Windows 上 `helm template` 的輸出被當成 cp950 解碼 | 中文註解出現時 YAML 解析報 `ReaderError`。寫這份 plan 時已經遇到一次 | 跟現有程式一樣，`subprocess.run` 帶 `encoding="utf-8"` |
| 第 5 段的自我檢查依賴 `eqp-collector` 的 chart | 之後有人刪掉或大改 `eqp-collector`，自我檢查會壞 | 可接受。它是目前唯一的手動 Helm 範本，壞掉時訊息會指向它 |
| 改動讓原本通過的三個 app 變成 FAIL | 步驟 1 存的輸出對照改完後的輸出，三個 app 的那幾行要一字不差 | 還原 `tests/validate.sh` 這一個檔 |

## 不在這個 plan 裡的

實作時可能會想順手做、但**不要做**的事：

| 不做 | 為什麼 |
| --- | --- |
| 把 `validate.sh` 裡內嵌的 Python 拆成獨立的 `.py` | 重構，跟這個需求無關 |
| 把第 3 段的 traceback 改成一般的 FAIL | spec 已列為不涵蓋 |
| 改 `app/eqp-collector/k8s/` 底下任何檔 | 這次只改檢查，不改被檢查的東西 |
| 改 `platform/applicationset.yaml` | ArgoCD 那條路這次不做 |
| 順手修 `README.md` 既有的 md 格式違規 | 另一個改動。這次只確保新加的句子不增加違規數 |
| `group` 改 `tenant` | 總綱拆法第 2 項 |
