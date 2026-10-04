# Acceptance — 驗證不走 ArgoCD 的 app

> 每一項都要是**可執行的指令加預期輸出**。
> 「功能正常」「畫面顯示正確」不算驗收項。

## 自動驗收

都從 repo 根目錄、用 Git Bash 跑。#3 ~ #8 先跑這段，把 repo 複製到暫存目錄再改壞，
不動 repo 裡的檔：

```bash
mk() { T=$(mktemp -d); mkdir -p "$T/tests"; cp -r applications platform "$T/"; cp tests/validate.sh "$T/tests/"; E="$T/applications/eqp-collector/helm"; }
run() { "$T/tests/validate.sh" 2>&1 | grep -E "$1|^(PASS|FAIL)|Traceback"; echo "exit=${PIPESTATUS[0]}"; }
```

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 1 | `./tests/validate.sh; echo "exit=$?"` | 五段都印出來，沒有 `Traceback`，最後 `PASS`、`exit=0` | ☑ |
| 2 | `./tests/validate.sh \| grep -E "eqp-collector"` | 第 1 段一行 `OK`、帶「手動 Helm」；第 3 段一行 `OK`、列出 `dept=D1110 group=imc system=eqp` | ☑ |
| 3 | `mk; sed -i 's/group: imc/group: imcc/' "$E/values.yaml"; run eqp-collector` | 第 1 段 `FAIL`，訊息有「不是允許的值」；`FAIL（…）`、`exit=1` | ☑ |
| 4 | `mk; sed -i 's/^namespace: .*//' "$E/values.yaml"; run eqp-collector` | 第 1 段 `FAIL`，訊息有「`namespace` 必填」；`exit=1` | ☑ |
| 5 | `mk; printf 'argocd:\n  namespace: x\n' >> "$E/values.yaml"; run eqp-collector` | 第 1 段 `FAIL`，訊息有「同時有」；`exit=1` | ☑ |
| 6 | `mk; rm "$E/templates/namespace.yaml"; run eqp-collector` | 第 1 段 `FAIL`，訊息有「沒有人貼」；沒有 `Traceback`；`exit=1` | ☑ |
| 7 | `mk; sed -i 's/^  labels:/  labels: {group: it}\n  x-unused:/' "$E/templates/namespace.yaml"; run eqp-collector` | 第 1 段 `OK`，第 3 段 `FAIL`，有「預期」；`exit=1` | ☑ |
| 8 | `mk; sed -i 's/^namespace: .*/namespace: yms-sim-dev/' "$E/values.yaml"; run "eqp-collector\|yms-sim"` | `eqp-collector` 和 `yms-sim` 在第 1 段都 `FAIL`，訊息有「也被」；`exit=1` | ☑ |
| 9 | `./tests/validate.sh \| grep -E "自我檢查|→" \| grep -c FAIL` | `0` | ☑ |
| 10 | `diff <(grep -E "^  OK +(mes-dispatch\|metrics-sim\|yms-sim)" before.txt) <(./tests/validate.sh \| grep -E "^  OK +(mes-dispatch\|metrics-sim\|yms-sim)" \| head -3)` | 沒有輸出：原本通過的三個 app 在第 1 段的結果一字不差 | ☑ |
| 11 | `python tools/check-md-style.py requirements/validate-manual-apps/*.md` | `✅ 全部符合` | ☑ |
| 12 | `python tools/check-md-style.py README.md applications/eqp-collector/README.md` | 違規數不比改之前多（改之前 `README.md` 5、`applications/eqp-collector/README.md` 4） | ☑ |

#10 的 `before.txt` 是實作前存下的 `./tests/validate.sh` 輸出。

## 人工確認

| # | 怎麼做 | 看到什麼算過 | 結果 |
| --- | --- | --- | --- |
| 1 | 讀 `applications/eqp-collector/README.md` 的「10. 跟其他 app 的差別」 | 不再說 `validate.sh` 會失敗；ArgoCD 那條警告還在 | ☑ |
| 2 | `git diff --stat` | 只有 `tests/validate.sh`、`README.md`、`applications/eqp-collector/README.md` 三個既有檔 | ☑ |

## 跟 plan 不一樣的地方

| 多做的 | 為什麼 |
| --- | --- |
| 第 3 段遇到「判不出誰貼 label」的 app 會印一行 `--` 跳過，不渲染 | plan 寫「不改 traceback」。但不跳過的話 #6 會在第 3 段 traceback，第 4、5 段又跑不到，等於沒解決 intent 的問題 |

## 已知未涵蓋

這次驗收**沒有**證明的事。

| 沒證明 | 為什麼 |
| --- | --- |
| cluster 上的 namespace 真的有這些 label | `helm template` 只證明 chart 會產生對的 Namespace。要在需求 2、3 的 k3d 測試驗 |
| ArgoCD 那條路沒被改壞 | `tests/routing` 要本機 k3d 加 ArgoCD，這次沒跑。改動沒碰 `render_labels()` 與 `platform/` |
| `eqp-collector` 接到有 ArgoCD 的環境會怎樣 | 這次不做，README 的警告保留 |

## 寫回文件

| 改了什麼 | 動到哪份文件 | 狀態 |
| --- | --- | --- |
| `validate.sh` 認得手動 Helm 的 app | `README.md` 的「Tests」與「新增一個 App」 | ☑ |
| `eqp-collector` 不再讓 `validate.sh` 失敗 | `applications/eqp-collector/README.md` 第 10 節 | ☑ |
| 指令沒變 | `.agents/project/commands.md` 不用動 | ☑ |
