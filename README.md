# aiops-data-foundation

給 IT 維運用的 AIOps 資料地基：把程式與 server 的健康度資料收齊、貼上一致的標籤，
之後用來做異常偵測與找根因。

⚠️ 這條 branch（`explore/elastic-all-in`）正在換方向，目前 repo 裡**沒有一條能完整
跑起來的路**。舊的做法已移除，新的做法只有需求文件和試驗用的設定。

| 想知道 | 看哪裡 |
| --- | --- |
| 現在要做什麼、為什麼 | [`requirements/elastic-unified-observability/1-intent.md`](requirements/elastic-unified-observability/1-intent.md) |
| 第 1 步的規格與已經實測的結果 | [`requirements/health-metrics-to-elastic/2-spec.md`](requirements/health-metrics-to-elastic/2-spec.md) |
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
| 地基的四件事（讀門牌、依部門分開存、擋冒充、停機補送） | 已在本機實測，設定在 [`requirements/health-metrics-to-elastic/spike/`](requirements/health-metrics-to-elastic/spike/README.md) |
| 範例程式、正式的收集設定、測試腳本 | 還沒做 |
| log、trace、異常偵測、找根因 | 還沒做 |

## 目錄

| 資料夾 | 放什麼 |
| --- | --- |
| `app/<app>/` | 一個 app 一個資料夾，程式碼和 `Dockerfile` 直接放在裡面 |
| `app/<app>/k8s/` | 這個 app 怎麼部署到 K8s（Helm chart） |
| `platform/` | 平台這一側的設定。目前只剩 ArgoCD 與 log 的舊設定，見 [`platform/README.md`](platform/README.md) |
| `tests/` | 只剩 `validate.sh`：檢查 app 的 `values.yaml` |
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

檢查每個 `values.yaml` 的必填欄位、`tenant` 是否合法、namespace 不能被兩個 app 共用、
chart 渲染得出來、namespace label 渲染結果跟 `values.yaml` 一致。內建的錯誤寫法一定要
被擋下，否則腳本本身失敗。

⚠ 允許的 `tenant` 清單目前寫在 `tests/validate.sh` 裡。收集程式的設定進 repo 之後會改成
從那裡讀。

## 已知的狀況

- **沒有能跑的整條路。** 舊的 vmagent 設定、三支 k3d 測試、範例程式 `eqp-collector` 都已移除。
- **剩下的三個 app 都是 ArgoCD 的寫法**（`mes-dispatch`、`yms-sim`、`metrics-sim`），chart 裡沒有
  `namespace.yaml`。用 `helm` 直接裝的話 namespace 不會有 label。
- **`metrics-sim` 和 `yms-sim` 也是模擬機台與良率的程式**，跟「不做機台收值」的方向不一致，
  還沒決定怎麼處理。
- **沒有東西告訴收集程式去抓這三個 app。** 原本靠 VictoriaMetrics 專用的登記檔，已經拿掉；
  新的做法在第 1 步決定。
- **log 的設定還是舊的。** `platform/logging/fluent.conf` 照 `dept` 分，之後由收集程式取代。
- **ArgoCD 那條路沒有實測。** `platform/argocd/applicationset.yaml` 留著，`tests/validate.sh` 只檢查
  它貼 label 的那段樣板。
