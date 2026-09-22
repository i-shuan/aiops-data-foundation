# C. namespace label → tenant 分流的 E2E

```bash
./tests/routing/run.sh
```

在本機 k3d 上跑。全部對 exit 0，任何一項不對 exit 1。跑完模擬器會一直留著產生資料。

## 測的是正式那份設定

```
正式    vmagent → 中央 vmauth（依 token）→ IT / IMC / platform tenant
本機    vmagent → 本機 vmauth（依 token）→ VMSingle it / imc / platform
```

測試用的 vmagent 由 [gen_vmagent.py](gen_vmagent.py) **從 `platform/cluster-addons/vmstack-values.yaml` 直接產生**：`scrapeClasses` 和 `remoteWrite` 兩段原封不動，只把 URL 換成本機的 vmauth。

三條 remoteWrite 打同一個 URL，唯一的差別是 token。三個 VMSingle 收到不同的資料，就代表「依 token 分 tenant」跟正式環境同樣在運作。

## 五個情境

| namespace | label 從哪來 | 預期 |
|---|---|---|
| `zk-sim-it-prod` | `tests/app-yaml/cases/ok-it.yaml` 渲染 | 只有 IT |
| `zk-sim-imc-prod` | `ok-imc.yaml` 渲染 | 只有 IMC |
| `zk-sim-shared-prod` | `ok-shared.yaml` 渲染（`group=it.imc`） | **IT 和 IMC** |
| `zk-sim-none-prod` | 沒有 label | 只有 platform |
| `zk-sim-bad-prod` | 直接貼 `group=nope` | 只有 platform |

前三個的 label 走主線：app.yaml → CI 驗證 → 用 [../app-yaml/render.sh](../app-yaml/render.sh) 渲染 template → 貼到 namespace。最後一步正式環境是 ArgoCD 做的，這裡用 `kubectl label` 代替。

`zk-sim-bad-prod` 正式環境會被 CI 擋下，這裡直接貼上去，驗證**就算 CI 漏掉，vmagent 也不會把它送錯 tenant**。

## 檢查什麼

1. **每個 namespace 出現在哪些 tenant，必須跟預期完全一樣**。多一個、少一個都算錯
2. **存進 tenant 的 `group` 等於那個 tenant 的名字**
3. **fan-out**：匯出 `zk-sim-shared-prod` 在 IT 和 IMC 裡的原始樣本，**每一筆的 timestamp 和 value 都要一樣**

第 3 項比對的是原始樣本，timestamp 精確到毫秒。被抓兩次的話 timestamp 會不同，只有「同一次抓取複製成兩份」才會完全相同。

## 實測結果

```
namespace            IT   IMC  platform  結果
zk-sim-it-prod       ✓    ·    ·         OK
zk-sim-imc-prod      ·    ✓    ·         OK
zk-sim-shared-prod   ✓    ✓    ·         OK
zk-sim-none-prod     ·    ·    ✓         OK
zk-sim-bad-prod      ·    ·    ✓         OK

存進 tenant 的 group 值：OK，6 筆全部相符
fan-out：OK，4 個感測器、36 筆原始樣本，timestamp 和 value 完全相同

IT   1790047043613→38.06  1790047053613→36.82  1790047063613→32.08
IMC  1790047043613→38.06  1790047053613→36.82  1790047063613→32.08
```

## 確認測試會抓到錯

把 IT 那條的 regex 改成只認單一值的 `'it'`：

```bash
VMSTACK_VALUES=/path/to/broken.yaml ./tests/routing/run.sh
```

```
zk-sim-shared-prod   ·    ✓    ·         FAIL（預期 imc,it）
fan-out：FAIL  IT 裡沒有樣本，無法比對
exit 1
```

只有共用的那個從 IT 消失，其他全對。這正是 `'it'` 這種寫法在正式環境會安靜漏掉的東西。

### 這個測試曾經假裝通過

第一次做上面這個驗證時，regex 明明改壞了，測試卻 PASS。

原因是改了 VMAgent 之後，operator 更新設定用的 Secret，vmagent 要等 kubelet 同步（最多一分鐘左右）才重新載入。那段期間它跑的還是**舊的、正確的**設定，資料照舊正確送出，測試一看全部符合就結束了。

所以 run.sh 現在會先用 [await_config.py](await_config.py) 輪詢 vmagent 的 `/remotewrite-url-relabel-config`，**確認實際載入的規則就是要測的那份**，接著清空三個 tenant，才開始檢查。

## 前提

```bash
# 模擬器映像（Dockerfile 預設從 Nexus 拉，本機換成 Docker Hub 的）
docker build --build-arg BASE_IMAGE=python:3.12-slim -t metrics-sim:test services/metrics-sim
k3d image import metrics-sim:test -c zk

# vmauth 映像
docker pull victoriametrics/vmauth:v1.148.0
k3d image import victoriametrics/vmauth:v1.148.0 -c zk
```

另外需要 VictoriaMetrics operator 已經裝好。

## 本機踩到的坑

- **VMSingle 被 OOMKilled。** 這台 k3d 是 cgroup v1，VictoriaMetrics 偵測到的可用記憶體不準。`backends.yaml` 裡用 `memory.allowedBytes` 明確給預算解掉。只是本機環境的問題，但它會讓某個 tenant 突然查不到東西，讓測試結果不可信。
- **Git Bash 會改寫開頭是 `/` 的參數**，`kubectl get --raw /api/v1/...` 會被當成 Windows 路徑。腳本裡設了 `MSYS_NO_PATHCONV=1`。
- **設了 `MSYS_NO_PATHCONV=1` 之後，kubectl 和 helm 反而看不懂 `/tmp/...`**，要自己用 `cygpath -m` 轉。

## 清掉

```bash
kubectl delete ns route-test zk-sim-it-prod zk-sim-imc-prod zk-sim-shared-prod zk-sim-none-prod zk-sim-bad-prod
```
