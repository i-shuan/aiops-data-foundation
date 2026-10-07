# aiops-data-foundation

給 IT 維運用的 AIOps 資料地基：把程式與 server 的健康度資料收齊、貼上一致的標籤，
之後用來做異常偵測與找根因。

這條 branch（`explore/elastic-all-in`）換了方向。目前能跑的是第 1 步：範例程式與 server 的
健康度數值、每個容器的 log、程式之間呼叫的 trace，三種都經 OpenTelemetry Collector 進 Elasticsearch，
依部門分開。只在本機 k3d 驗過。

| 想知道 | 看哪裡 |
| --- | --- |
| 現在要做什麼、為什麼 | [`requirements/elastic-unified-observability/1-intent.md`](requirements/elastic-unified-observability/1-intent.md) |
| 第 1 步的規格與驗收結果 | [`requirements/health-metrics-to-elastic/`](requirements/health-metrics-to-elastic/4-acceptance.md) |
| 收集程式怎麼部署、資料怎麼分部門 | [`platform/README.md`](platform/README.md) |
| 給主管看的架構圖與資料量試算 | [`requirements/architecture.html`](requirements/architecture.html)，用瀏覽器直接開 |
| 舊的做法（VictoriaMetrics）在哪 | branch `chore/ai-native-scaffold` |

## 方向

```
程式與 server
    ↓  一套收集程式（OpenTelemetry Collector），讀 namespace 的門牌貼標籤
Elasticsearch（metrics、log、trace 都存這）
    ↓  自己訓練的 AIOps
異常偵測 → 關聯 → 找根因
```

| 項目 | 決定 |
| --- | --- |
| 監控的對象 | 程式健康度、server 健康度。不做機台收值 |
| 給誰用 | IT 維運人員。最花時間的是找根因 |
| 收集 | OpenTelemetry Collector，一套收三種資料 |
| 儲存 | Elasticsearch 9.5.3，免費版 |
| AIOps | 自己訓練，不依賴付費功能 |

## 現在做到哪

| 項目 | 狀態 |
| --- | --- |
| 方向與原則 | 已確認 |
| 範例程式（可以從外面弄壞的兩支服務） | 做完，在 `app/demo-svc/` |
| 收集設定：程式與 server 的數值進 Elasticsearch，依部門分開、擋冒充、停機補送 | 做完，在 `platform/otel/`，本機 k3d 實測 |
| log：每個容器印的 log 進 Elasticsearch，跟數值同一套標籤 | 做完，本機 k3d 實測 |
| trace：不改程式碼，靠自動埋點送出，跟數值、log 同一套標籤 | 做完，本機 k3d 實測 |
| 異常偵測、找根因 | 還沒做 |
| 誰能看哪個部門的資料、資料保留多久 | 還沒做 |

## 目錄

| 資料夾 | 放什麼 |
| --- | --- |
| `app/<app>/` | 一個 app 一個資料夾，程式碼和 `Dockerfile` 直接放在裡面 |
| `app/<app>/k8s/` | 這個 app 怎麼部署到 K8s（Helm chart） |
| `platform/` | 平台這一側的設定。收集程式在 `platform/otel/`，見 [`platform/README.md`](platform/README.md) |
| `tests/` | `validate.sh`：不用 cluster 的靜態檢查。`obs/`：在本機 k3d 上實測 |
| `requirements/` | 每個需求的文件，以及架構圖 |
| `tools/` | 檢查文件格式與規則同步的小工具 |
| `.agents/`、`.claude/`、`.github/` | 給 AI 助手讀的規則與防護 |

## 部門與標籤

每個 app 在 `app/<app>/k8s/values.yaml` 宣告自己是誰的，部署時變成 namespace 的 label：

```yaml
observability:
  dept: D3120       # 必填
  tenant: tsid      # 必填：tsid / bsid / icsd / imc / platform
  system: mes       # 選填
```

| `tenant` | 管什麼 |
| --- | --- |
| `icsd` | infra，server 底層。server 的資料由收集程式直接標成這個 |
| `tsid` | MES、RMS、EAP 相關的程式 |
| `bsid` | ERP 相關的程式 |
| `imc` | app 應用。這一輪先放著 |
| `platform` | 整套 AIOps 自己。沒掛門牌或門牌寫錯的也先歸這裡 |

沒寫 `system` 就不會有 `system` 這個 label，不會變成 `system=""`。

## 在自己的電腦上一鍵部署

裝好 Rancher Desktop（容器引擎選 containerd、啟用 Kubernetes、PATH 設成 Automatic）之後，
在 repo 的資料夾開終端機。

PowerShell（提示符號開頭是 `PS`）：

```powershell
.\deploy.cmd
```

cmd（命令提示字元）：

```
deploy.cmd
```

它會先印出要部署到哪個 cluster，等你輸入 `y` 才動手。裝的是 Operator、兩份收集程式、
一座測試用的 Elasticsearch、兩組範例程式，跑完會印出 Elasticsearch 裡三種資料各有幾筆。
2026-10-07 在 Rancher Desktop（K8s 1.30.14、containerd）上實際跑過，約 5 分鐘。

| 情況 | 先下這一行再跑 |
| --- | --- |
| 不在公司的網路（連不到公司的 Nexus） | PowerShell：`$env:BASE_IMAGE = "python:3.12-slim"`；cmd：`set BASE_IMAGE=python:3.12-slim` |
| 不想重建範例程式的 image | PowerShell：`$env:SKIP_BUILD = "1"`；cmd：`set SKIP_BUILD=1` |
| 不要問、直接部署 | 指令後面加 `-Yes` |
| 對別的本機 cluster | 指令後面加 `-Context 名稱` |

⚠️ 只給本機用。Elasticsearch 是測試用的單節點、沒有開 TLS。

⚠️ image 與 Helm chart 都從外面拉，加起來超過 1 GB。公司的網路拉不到的話會停在那一步並說是哪一個。

⚠️ PowerShell 裡一定要打 `.\deploy.cmd`，少了 `.\` 它找不到；`set 名稱=值` 在 PowerShell 裡沒有作用，
不會報錯但環境變數沒有設進去。

⚠️ Rancher Desktop 第一次啟動要從 GitHub 下載 K8s（約 220 MB），斷線會整個重來。網路慢的話可以自己
下載 `k3s`、`k3s-airgap-images-amd64.tar.zst`、`sha256sum-amd64.txt` 三個檔，放進
`%LOCALAPPDATA%\rancher-desktop\cache\k3s\<版本>\`（例如 `v1.30.14+k3s2`），再重開 Rancher Desktop。

### 拉不到 image 的環境（離線部署）

在拉得到的電腦上先部署一次，再把用到的 image 與 Helm chart 存進 `image\` 資料夾：

```powershell
.\deploy.cmd -SaveImages
```

`image\` 裡的檔案走 Git LFS。另一台裝了 Git LFS 再 clone，就能照常跑 `.\deploy.cmd`。資料夾裡有檔案的話，
腳本會先把它們載入，後面不上網拉 image、不抓 Helm chart，也不重建範例程式。

⚠️ `image\` 裡的檔案一共 1 GB 多。沒裝 Git LFS 就 clone 的話拿到的只是指標檔，要補 `git lfs install` 與 `git lfs pull`。
細節在 [`image/README.md`](image/README.md)。

⚠️ 「從資料夾載入」這條路在本機實際跑過。但那台電腦上本來就有這些 image，所以沒有證明
「完全沒有網路也裝得起來」。第一次在沒有網路的電腦上跑時要留意。

`deploy.cmd` 只是一行啟動檔，真正的內容與說明在 [`deploy.ps1`](deploy.ps1)。

## Tests

```bash
./tests/validate.sh       # 不需要 cluster，要 python（PyYAML）和 helm
```

檢查每個 `values.yaml` 的必填欄位、`tenant` 是否合法、namespace label 渲染結果跟
`values.yaml` 一致；以及 `platform/otel/` 裡改錯了不會報錯的那幾個地方。
內建的錯誤寫法一定要被擋下，否則腳本本身失敗。

允許的 `tenant` 清單只有一份，在 `platform/otel/common.yaml`。

```bash
./tests/obs/run.sh        # 要本機的 k3d cluster obs-test，約 5 分鐘
```

把 `platform/otel/` 套到本機的 k3d，部署範例程式與三個假程式，進 Elasticsearch 查每個情境
進了哪一份。另有 `outage`（停機補送）與 `broken`（改壞的設定一定要 FAIL）。
cluster 的建法見 [`.agents/project/commands.md`](.agents/project/commands.md)。

## 已知的狀況

- **trace 只驗過 Python 的範例程式。** 自動埋點只認得常見的框架；真的系統用什麼寫的還沒盤點。
- **測試 cluster 要先裝 OpenTelemetry Operator。** 指令在 `.agents/project/commands.md`。
- **Elasticsearch 連線逾時的時候會掉資料。** 寫入程式只重試「連線被拒絕」，不重試「逾時」。本機的停機測試
  測的是前者；正式環境的網路中斷比較常是後者。細節在 [`platform/README.md`](platform/README.md)，上線前要先解決。
- **只在本機 k3d 驗過。** 一個節點、Elasticsearch 沒開 TLS。正式環境沒有碰過。
- **`platform/otel/site-ctsp.yaml` 裡的位址是佔位。** 上線前要換，`tests/validate.sh` 會提醒。
- **跟機台有關的範例程式都移除了**：`eqp-collector`、`metrics-sim`（模擬機台收值）、`yms-sim`
  （模擬良率）。方向是不做機台收值。
- **`mes-dispatch` 是 ArgoCD 的寫法**，chart 裡沒有 `namespace.yaml`，pod 上也沒有
  「我要被抓」的標註。用 `helm` 直接裝的話 namespace 不會有 label，收集程式也不會抓它。
- **ArgoCD 那條路沒有實測。** `platform/argocd/applicationset.yaml` 留著，`tests/validate.sh` 只檢查
  它貼 label 的那段樣板。
