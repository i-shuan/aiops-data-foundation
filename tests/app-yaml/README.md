# 歸屬寫在 app.yaml 的測試

```bash
./tests/app-yaml/run.sh
```

前提是 [../routing/run.sh](../routing/run.sh) 跑過一次（模擬器和三個 tenant 已經在 zk 上）。

## 測什麼

1. **CI 驗證**：[cases/](cases) 裡每個 app.yaml，`validate_observability.py` 的結果要跟檔案第一行的 `# expect:` 一致
2. **template**：用 helm 的模板引擎渲染 [namespace-metadata.tmpl](../../platform/argocd/namespace-metadata.tmpl)，看出來的 label 對不對
3. **路由**：照 ArgoCD 會做的方式把 label 貼到 namespace，跑 [../routing/check.py](../routing/check.py) 的同一套檢查

第 3 步之前會先把 ns-labeler 的測試對照表拿掉，這樣才分得出 label 是這條路貼的，不是 ns-labeler 貼的。

## 結果

```
=== 1. CI 驗證 ===
  bad-double-managed.yaml  預期 FAIL 實際 FAIL ✓
  bad-field-typo.yaml      預期 FAIL 實際 FAIL ✓
  bad-label-value.yaml     預期 FAIL 實際 FAIL ✓
  bad-unknown-dept.yaml    預期 FAIL 實際 FAIL ✓
  sim-both.yaml            預期 OK   實際 OK   ✓
  sim-imc.yaml             預期 OK   實際 OK   ✓
  sim-it.yaml              預期 OK   實際 OK   ✓
  sim-none.yaml            預期 FAIL 實際 FAIL ✓
  sim-plat.yaml            預期 FAIL 實際 FAIL ✓
  sim-typo.yaml            預期 FAIL 實際 FAIL ✓

=== 2. 渲染 template ===
  sim-it     dept=D3120 group=it system=sim       ← group 依 dept 自動推出
  sim-imc    dept=D1110 group=imc system=sim
  sim-both   dept=D3120 group=it.imc              ← 有寫 group 就用寫的；沒填 system 就不寫這個 label

=== 5. 路由檢查 ===
  zk-sim-it-prod     ✓  ·  ·   OK
  zk-sim-imc-prod    ·  ✓  ·   OK
  zk-sim-both-prod   ✓  ✓  ·   OK
  （寫錯的三個都只進兜底）
PASS
```

路由檢查是在**清空三個 tenant 的資料之後**重新確認的，排除讀到上一輪 ns-labeler 標的時候留下的舊資料。

## 這不是 ArgoCD

template 是用 helm 渲染的。helm 和 ArgoCD ApplicationSet 都是 Go template + sprig，這裡用到的函式行為一樣，但**這不是 ArgoCD 本身**。ArgoCD 會不會把 label 貼到已經存在的 namespace、貼的時機是不是在 pod 之前，都沒有驗證，見 [../../platform/argocd/README.md](../../platform/argocd/README.md) 的「沒有驗證的部分」。

## 本機踩到的坑

- **helm 讀不懂 Git Bash 的 `/tmp/...` 路徑。** `mktemp -d` 給的路徑要用 `cygpath -m` 轉成 `C:/...`，不然 helm 會報 `repo not found`（它把找不到的路徑當成 chart repo 的名字）。
- **Windows 的 Python 輸出行尾是 `\r\n`。** `\r` 會黏在 label 值後面變成 `D3120\r`。這次是 kubectl 自己擋下來了；混進別的地方的話，就跟 token 多一個換行一直 401 是同一類看不見的問題。
