# metrics 分流的端到端測試

驗證一件事：**namespace 上貼什麼 label，資料就進哪些 tenant**——只進 IT、只進 IMC、兩個都進，或是寫錯的時候掉進兜底，不會送錯人。

```bash
./tests/routing/run.sh
```

全部對 exit 0，有一格不對 exit 1。跑完模擬器會一直留著產生資料，可以接著查。

## 測的是正式那份設定，不是另外寫的

測試用的 vmagent 是 [gen_vmagent.py](gen_vmagent.py) **從 `platform/cluster-addons/vmstack-values.yaml` 直接讀出來產生的**：`scrapeClasses` 和 `remoteWrite` 兩段原封不動，只把 URL 換成本機的 vmauth。

```
正式：vmagent → 中央 vmauth（依 token）→ tenant IT / IMC / 平台
測試：vmagent → 本機 vmauth（依 token）→ VMSingle it / imc / platform
```

三條 remoteWrite 的 URL 完全一樣，**唯一的差別是帶的 token**。所以三個 VMSingle 收到不同的資料，就代表「依 token 分 tenant」這個機制跟正式環境一樣在運作。

如果手寫一份測試用的設定，測過了也不代表正式那份是對的。

## 六個情境

| namespace | 對照表怎麼寫 | 標上的 group | 預期進哪 |
|---|---|---|---|
| `zk-sim-it-prod` | `dept: D3120` | `it` | 只有 IT |
| `zk-sim-imc-prod` | `dept: D1110` | `imc` | 只有 IMC |
| `zk-sim-both-prod` | `dept: D3120, group: it.imc` | `it.imc` | **IT 和 IMC 都有** |
| `zk-sim-none-prod` | 沒登記 | 無 | 只有兜底 |
| `zk-sim-typo-prod` | `group: it.imcc`（打錯字） | **拒標** | 只有兜底 |
| `zk-sim-plat-prod` | `group: it.platform` | **拒標** | 只有兜底 |

後兩個是在驗證「寫錯的時候會怎樣」：**打錯字不會讓資料送進錯的 tenant，而是被擋下來、掉進兜底、觸發「沒有歸屬」的告警**。

`zk-sim-both-prod` 故意不填 `system`，順便驗證對照表中間欄位空著時，後面的 `group` 不會錯位。

## 實測結果

```
namespace          IT     IMC    platform  結果
zk-sim-it-prod     ✓      ·      ·         OK
zk-sim-imc-prod    ·      ✓      ·         OK
zk-sim-both-prod   ✓      ✓      ·         OK
zk-sim-none-prod   ·      ·      ✓         OK
zk-sim-typo-prod   ·      ·      ✓         OK
zk-sim-plat-prod   ·      ·      ✓         OK

存進 tenant 的 group 值（應該等於 tenant 名稱）：
  OK，7 筆全部相符
```

另外確認了：

- **資料是活的。** 同一個感測器 12 秒內 31.05 → 32.39 → 33.11，收值速率每秒約 0.8 筆（4 個感測器 ÷ 5 秒），失敗率約 5%，都跟設定一致。
- **共用的資料是複製，不是重抓。** `zk-sim-both-prod` 在 IT 和 IMC 兩個 tenant 裡同一時間點的值完全一樣。

## 這個測試抓得到錯誤嗎

一次就過的測試要先懷疑它是不是空轉。所以故意把 IT 那條的 regex 改成最直覺的錯誤寫法 `'it'`（只認單一值），重跑：

```
zk-sim-it-prod     ✓      ·      ·         OK
zk-sim-both-prod   ·      ✓      ·         FAIL（預期 imc,it）
```

只有 `it.imc` 從 IT 消失，其他全部正確，exit 1。**這正是 `'it'` 這種寫法會漏掉的東西**，而且在正式環境裡它不會報任何錯。

要自己重做這個驗證，用 `VMSTACK_VALUES` 指到一份改壞的複本，不用動正式那份：

```bash
VMSTACK_VALUES=/path/to/broken.yaml python tests/routing/gen_vmagent.py | kubectl apply -f -
```

**看失敗的樣子，不要只看有沒有失敗。** 第一次做這個驗證時測試也喊了 FAIL，但是幾乎每一格都空——那是因為剛重啟後端、資料還沒回來，不是 regex 的錯。等資料穩定（`check.py --wait 120`）後才出現上面那個「只有一格錯」的正確失敗模式。

## 前提

```bash
# 1. 模擬器映像（Dockerfile 預設從 Nexus 拉，本機換成 Docker Hub 的）
docker build --build-arg BASE_IMAGE=python:3.12-slim -t metrics-sim:test services/metrics-sim
k3d image import metrics-sim:test -c zk

# 2. vmauth 映像
docker pull victoriametrics/vmauth:v1.148.0
k3d image import victoriametrics/vmauth:v1.148.0 -c zk
```

另外需要：VictoriaMetrics operator 已裝、ns-labeler 已經用 helm 裝在 `vm-monitoring`。

## 本機踩到的坑

- **VMSingle 被 OOMKilled。** 這台 k3d 是 cgroup v1，VictoriaMetrics 偵測到的可用記憶體不準，快取開得比容器上限還大。`backends.yaml` 裡用 `memory.allowedBytes` 明確給預算解掉。**只是本機環境的問題**，但它會讓測試結果不可信（某個 tenant 突然查不到東西），所以值得知道。
- **Git Bash 會改寫開頭是 `/` 的參數。** `kubectl get --raw /api/v1/...` 會被當成 Windows 路徑。腳本裡設了 `MSYS_NO_PATHCONV=1`。

## 清掉

```bash
kubectl delete ns route-test zk-sim-it-prod zk-sim-imc-prod zk-sim-both-prod zk-sim-none-prod zk-sim-typo-prod zk-sim-plat-prod
helm upgrade ns-labeler platform/cluster-addons/ns-labeler -n vm-monitoring --set image.registry=docker.io
```

第二行把 ns-labeler 的對照表還原成不含測試項目的預設值。
