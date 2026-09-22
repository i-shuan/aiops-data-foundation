# 歸屬寫在 app.yaml

一隻 app 一個 namespace，所以歸屬直接寫在 devops repo 的 `applications/<app>/app.yaml`，由 ArgoCD 貼到 namespace 上。vmagent 和 Fluentd 讀的本來就是 namespace 的 label，**收集端設定一行都不用改**。

```
applications/<app>/app.yaml       observability: {dept, system, group?}
        ↓ ApplicationSet 的 templatePatch（namespace-metadata.tmpl）
        ↓ CreateNamespace + managedNamespaceMetadata
namespace 上的 label             dept / group / system
        ↓
vmagent（依 group 分流）、Fluentd（依 dept 分流）
```

## 要改 devops repo 的三個地方

### 1. `app.yaml` 加 `observability` 區塊

```yaml
observability:
  dept: D3120        # 必填，只有一個值
  system: mes        # 選填
  # group: it.imc    # 選填，只有「一份資料要給多個 tenant」時才寫
```

完整範例見 [app.yaml.example](app.yaml.example)。`group` 平常不寫，由 template 依 `dept` 查表推出來。

### 2. ApplicationSet 的 templatePatch

把 [namespace-metadata.tmpl](namespace-metadata.tmpl) 的內容併進 `applicationsets/devops/applicationset.yaml` 的 `templatePatch`。**dept → group 的對照表就在這份 template 裡**，改組要改這裡。

⚠ **`syncOptions` 要合併，不能直接貼。** templatePatch 裡的 list 是整個取代，你們現有的 `syncOptions` 如果沒一起寫進來，會被蓋掉而且不會報錯。

### 3. CI 加一個驗證 job

```bash
python validate_observability.py \
  --ns-labeler-values cluster-addons/ns-labeler/values.yaml \
  applications/*/app.yaml
```

可以併進你們的 `scripts/validate_application.py`，或在 `.gitlab-ci.yml` 另外開一個 job。**`--ns-labeler-values` 不能省**，理由見下面「兩份對照表」。

## CI 擋什麼

這些值最後決定資料進哪個 tenant，**寫錯的話執行時不會有任何錯誤**，只是送進錯的地方或全掉進兜底。所以要在 merge 前擋：

| 寫法 | 結果 | 不擋會怎樣 |
|---|---|---|
| 沒有 `observability` | 擋 | 資料全進兜底 |
| `dept: D9999`（不在對照表） | 擋 | group 查不到是空的，全進兜底 |
| `group: it.imcc`（打錯字） | 擋 | 只進 IT，IMC 看不到，沒人發現 |
| `group: it.platform` | 擋 | 平台其實收不到，因為兜底是用「不屬於任何 tenant」認的 |
| `system: MES System`（有空白） | 擋 | 不是合法的 label 值，ArgoCD sync 會失敗 |
| `grop: it.imc`（欄位名打錯） | 擋 | **被默默忽略**，看起來有設其實沒有 |
| 同一個 namespace 也在 ns-labeler 對照表 | 擋 | 兩邊互相覆蓋，label 一直翻 |
| 兩份 dept → group 對照不一致 | 擋 | 同一個部門的 app 依「誰標的」分流到不同 tenant |

測試案例在 [../../tests/app-yaml/cases/](../../tests/app-yaml/cases)，每個檔案第一行寫著預期結果。

## 兩份對照表：ArgoCD 一份、ns-labeler 一份

不是所有 namespace 都是 ArgoCD 部署的：`kube-system`、`vm-monitoring` 這些平台自己的，還有沒進 ArgoCD 的舊 app。這些還是由 [ns-labeler](../cluster-addons/ns-labeler) 標，它也需要 dept → group 的對照。

所以對照表有兩份：

| | 位置 | 管哪些 namespace |
|---|---|---|
| ArgoCD | `namespace-metadata.tmpl` 裡的 `$groups` | 有 app.yaml 的 |
| ns-labeler | `cluster-addons/ns-labeler/values.yaml` 的 `registry.groups` | 平台的、沒進 ArgoCD 的 |

**兩份不一致的後果是靜默的**：改組時只改了一份，同一個部門的 app 就會依「是誰標的」分流到不同 tenant。本機實測過這個情況：D3120 在 ns-labeler 改成 imc，ns-labeler 管的 `zk-mes-prod` 跟著變了，ArgoCD 管的 `zk-sim-it-prod` 沒變，兩個 D3120 的 app 進了不同 tenant。

所以 CI 的 `--ns-labeler-values` 會比對這兩份，**一個不一致就不讓 merge**。改組時兩份要在同一個 PR 裡改。

## ns-labeler 怎麼不跟 ArgoCD 搶

ns-labeler 標過的 namespace 會帶 `observability.spil.com.tw/labeled-by: ns-labeler` 這個 annotation，靠它分辨哪些是自己的：

| 情況 | ns-labeler 的反應 |
|---|---|
| 對照表沒有、namespace 有 label、不是它標的 | 算「由別處管理」，**不報成沒歸屬** |
| 對照表沒有、但以前是它標的（搬去 app.yaml 了） | **釋放**：拿掉 annotation，label 保留 |
| 對照表有、namespace 的 label 是別人標的、值不一樣 | **報錯、不蓋**。硬蓋的話 ArgoCD selfHeal 會蓋回去，一直翻 |
| 對照表有、是它自己標的、改組了 | 照常更新 |
| 舊版標的（沒有 annotation）、值正確 | 認領，補上 annotation |

這幾種都在本機實測過。**但 CI 的「同一個 namespace 不能兩邊管」才是根本的防線**，ns-labeler 這邊只是萬一漏網時不要把情況弄得更糟。

**搬家的步驟**：在同一個 PR 裡，app.yaml 加上 `observability`、ns-labeler 對照表刪掉那一行。CI 會檢查兩邊沒有重疊。合進去之後 ns-labeler 下一輪會釋放那個 namespace，ArgoCD 接手。

## 這條路比 ns-labeler 好的地方

**「先登記才能部署」的問題不見了。** 用 ns-labeler 時，app 如果比對照表先上線，那段時間的資料會進兜底、而且事後搬不回來，所以才需要部署前檢查。

用 app.yaml 的話，歸屬跟 app 在同一個檔案、同一次 sync。沒寫 `observability` 在 CI 就被擋下，**不可能部署一個沒有歸屬的 app**。

## 代價：production 改組要等手動 sync

你們的規則是 `environment: production` 不可自動同步。所以改組（改 `$groups`）之後，**production 的 namespace 要等有人手動 sync 才會換 label**，這段期間資料照舊進原本的 tenant。

ns-labeler 是每 10 分鐘自動對帳，沒有這個延遲。改組時要記得排一次 production 的 sync。

## 沒有驗證的部分

我這邊沒有 ArgoCD，以下是**推論**，上線前要在你們的環境確認：

1. **template 本身**是用 helm 的模板引擎模擬的。兩者都是 Go template + sprig，`dict`、`dig`、`get`、`default` 行為一樣，但這不是 ArgoCD 本身。
2. **`managedNamespaceMetadata` 對「已經存在」的 namespace 會不會套用。** 你們的 namespace 如果是 `scripts/deploy.sh` 先建好的，要確認 ArgoCD 會不會把 label 補上去。
3. **namespace 的 label 是不是在 app 的 pod 起來之前就貼好。** 理論上 ArgoCD 會先處理 namespace，但沒實測。如果不是，app 第一次部署時最開始的幾筆資料會進兜底。
4. **你們 ApplicationSet 的 `goTemplateOptions`。** template 用 `dig` 取值，就算設了 `missingkey=error` 也不會因為 app.yaml 沒寫某個欄位而失敗，但要確認你們有開 `goTemplate: true`。

vmagent 那一段有實機驗證：label 照 template 渲染出來的結果貼上 namespace 之後，只送 IT、只送 IMC、兩個都送、寫錯掉進兜底，六個情境全部正確。見 [../../tests/app-yaml/](../../tests/app-yaml)。
