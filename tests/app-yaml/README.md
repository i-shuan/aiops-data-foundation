# A. app.yaml 驗證　B. app.yaml → namespace label

```bash
./tests/app-yaml/run.sh
```

**不需要 cluster**，只要 python（PyYAML）和 helm。全部符合預期 exit 0，任何一個不符 exit 1。

## A. 驗證：結果要跟 `# expect` 一致

[cases/](cases) 裡每個檔案第一行寫著預期結果。`fail-*` 是應該被擋下的，每一個只犯一種錯：

| 案例 | 擋的是 |
|---|---|
| `fail-missing` | 沒有 `observability` |
| `fail-field-typo` | 欄位名打錯（`grop:`），YAML 會默默忽略它 |
| `fail-unknown-dept` | 不在對照表裡的部門 |
| `fail-illegal-group` | 不存在的 tenant（`it.imcc`） |
| `fail-platform-combo` | `it.platform`：platform 是 fallback，不能組合 |
| `fail-ambiguous` | 歸屬矛盾：dept 是 IMC，group 卻只送 IT |
| `fail-label-value` | 不是合法的 Kubernetes label 值 |
| `duplicate/` | 兩個 app.yaml 宣告同一個 namespace，各自看都合法，要一起驗才抓得到 |

每一個都確認過是被**它要測的那條規則**擋下的，不是剛好被別條擋下。

## B. 渲染：namespace 拿到的 label 要跟 `# labels` 一致

用正式的 [namespace-metadata.tmpl](../../platform/argocd/namespace-metadata.tmpl) 渲染每個 `ok-*` 案例：

| 案例 | app.yaml | namespace 的 label |
|---|---|---|
| `ok-it` | `dept: D3120, system: sim` | `dept=D3120 group=it system=sim` |
| `ok-imc` | `dept: D1110, system: sim` | `dept=D1110 group=imc system=sim` |
| `ok-shared` | `dept: D3120, group: it.imc` | `dept=D3120 group=it.imc`（沒有 system） |

`ok-it` 和 `ok-imc` 的 group 是依 dept 推出來的，`ok-shared` 是明確寫的。`ok-shared` 故意不填 system，確認不會產生空的 system label。

**這三個案例也是 E2E 的輸入**：[../routing](../routing) 用同一支 [render.sh](render.sh) 從這些 app.yaml 渲染 label 再貼到 namespace，所以 B 驗過的就是 C 用的。

## 確認測試會抓到錯

把 template 裡的 `"D3120" "it"` 改成 `"D3120" "imc"` 再跑，`ok-it` 渲染出 `group=imc`、跟預期不符，exit 1。`ok-shared` 照樣通過，因為它的 group 是明確寫的，不靠對照表——這也是對的。

```bash
NAMESPACE_METADATA_TMPL=/path/to/broken.tmpl ./tests/app-yaml/run.sh
```

## 這不是 ArgoCD

渲染用的是 helm。helm 和 ArgoCD ApplicationSet 都是 Go template + sprig，這裡用到的 `dict`、`dig`、`get`、`default` 行為一樣，但這不是 ArgoCD 本身。

## Windows 上踩到的坑

- **helm 讀不懂 Git Bash 的 `/tmp/...` 路徑**，會報 `repo not found`（把找不到的路徑當成 chart repo 的名字）。要用 `cygpath -m` 轉。
- **Windows 的 Python 行尾是 `\r\n`**，`\r` 會黏在 label 值後面變成 `D3120\r`。
- **helm 的輸出有中文註解**，Python 沒被告知用 UTF-8 讀 stdin 的話會解成亂碼後炸掉。render.sh 自己設了 `PYTHONIOENCODING`，直接呼叫也不會壞。
