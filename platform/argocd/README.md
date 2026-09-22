# 併進 devops repo

這個資料夾裡的東西不部署，是要併進你們 devops repo 的。整條資料流見 [../../docs/architecture.md](../../docs/architecture.md)。

| 檔案 | 放到 devops repo 的哪裡 |
|---|---|
| [namespace-metadata.tmpl](namespace-metadata.tmpl) | `applicationsets/devops/applicationset.yaml` 的 `templatePatch` |
| [validate_observability.py](validate_observability.py) | `scripts/`，在 CI 裡跑 |
| [app.yaml.example](app.yaml.example) | 範例，每個 `applications/<app>/app.yaml` 照這個加 `observability` |

## 要改的三個地方

### 1. 每個 app.yaml 加 `observability`

```yaml
observability:
  dept: D3120        # 必填
  system: mes        # 選填
  # group: it.imc    # 選填，只有要多送給別的 tenant 時才寫
```

### 2. ApplicationSet 的 templatePatch

把 `namespace-metadata.tmpl` 的內容併進去。**dept → group 的對照表就在這份 template 的 `$groups` 裡**，改組只改這裡。

⚠ **`syncOptions` 要合併，不能直接貼。** templatePatch 裡的 list 是整個取代，你們現有的 `syncOptions` 沒一起寫進來的話會被蓋掉，而且不會報錯。

⚠ 要確認 ApplicationSet 有開 `goTemplate: true`。template 用 `dig` 取值，就算設了 `missingkey=error` 也不會因為 app.yaml 少寫某個欄位而失敗。

### 3. CI 加一個 job

```bash
python scripts/validate_observability.py applications/*/app.yaml
```

**一定要一次把全部 app.yaml 傳進去**，「同一個 namespace 不能被兩個 app 宣告」要看全部才驗得出來。

驗證器直接讀 template 裡的 `$groups`，所以不需要另外告訴它有哪些部門。擋哪些寫法見 [../../docs/architecture.md](../../docs/architecture.md) 的 ②。

## 上線前要在你們環境確認的

這個 repo 沒有 ArgoCD，以下沒辦法在本機驗證：

1. **`managedNamespaceMetadata` 對已經存在的 namespace 會不會套用。** 你們的 namespace 如果是 `scripts/deploy.sh` 先建好的，要確認 ArgoCD 會把 label 補上去。
2. **label 是不是在 pod 起來之前就貼好。** 理論上 ArgoCD 會先處理 namespace，但沒實測。如果不是，app 第一次部署時最開始的幾筆資料會進 fallback。
3. **production 改組要手動 sync。** 你們的規則是 production 不可自動同步，所以改了 `$groups` 之後，production 的 namespace 要等有人手動 sync 才會換 label。

template 渲染的結果和 vmagent 的分流有在本機驗證過，見 [../../tests/](../../tests)。
