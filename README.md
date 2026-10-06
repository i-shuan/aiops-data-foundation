# aiops-data-foundation

給 IT 維運用的 AIOps 資料地基：把程式與 server 的健康度資料收齊、貼上一致的標籤，
之後用來做異常偵測與找根因。

這條 branch（`explore/elastic-all-in`）換了方向。目前能跑的是第 1 步：範例程式與 server 的
健康度數值，以及每個容器的 log，經 OpenTelemetry Collector 進 Elasticsearch，依部門分開。
只在本機 k3d 驗過。

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
| trace、異常偵測、找根因 | 還沒做 |
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

- **trace 還沒收。** 範例程式有傳編號、log 裡查得到，但沒有真的送出 trace。
- **只在本機 k3d 驗過。** 一個節點、Elasticsearch 沒開 TLS。正式環境沒有碰過。
- **`platform/otel/site-ctsp.yaml` 裡的位址是佔位。** 上線前要換，`tests/validate.sh` 會提醒。
- **跟機台有關的範例程式都移除了**：`eqp-collector`、`metrics-sim`（模擬機台收值）、`yms-sim`
  （模擬良率）。方向是不做機台收值。
- **`mes-dispatch` 是 ArgoCD 的寫法**，chart 裡沒有 `namespace.yaml`，pod 上也沒有
  「我要被抓」的標註。用 `helm` 直接裝的話 namespace 不會有 label，收集程式也不會抓它。
- **ArgoCD 那條路沒有實測。** `platform/argocd/applicationset.yaml` 留著，`tests/validate.sh` 只檢查
  它貼 label 的那段樣板。
