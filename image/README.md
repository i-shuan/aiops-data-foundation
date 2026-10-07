# image — 離線部署用的檔案

這個資料夾放部署要用的 image 與 Helm chart，給拉不到外面 image 的環境用。
裡面的檔案一共 1 GB 多，不進 git，只有這份說明在 repo 裡。

## 怎麼產生

在拉得到 image 的電腦上先部署一次，再把它用到的東西存下來：

```powershell
.\deploy.cmd
```

```powershell
.\deploy.cmd -SaveImages
```

## 怎麼用

把整個 repo 連同這個資料夾複製到另一台電腦，在那一台照常部署：

```powershell
.\deploy.cmd
```

這個資料夾裡有 `.tar` 的話，腳本會先把它們載入，後面就不上網拉，也不用重建範例程式。

⚠️ `git clone` 下來的 repo 裡這個資料夾是空的。檔案要用隨身碟、共用資料夾等方式另外複製。

## 裡面應該有什麼

| 檔案 | 是什麼 |
| --- | --- |
| `elasticsearch.tar` | 測試用的 Elasticsearch 9.5.3，最大的一個 |
| `otel-collector.tar` | OpenTelemetry Collector contrib 0.161.0 |
| `otel-operator.tar` | OpenTelemetry Operator 0.160.0 |
| `otel-autoinst-python.tar` | Python 的自動埋點，Operator 塞進 pod 用的 |
| `busybox.tar` | 測試用的假程式 |
| `demo-svc.tar` | 範例程式，已經建好的 |
| `opentelemetry-operator-0.124.1.tgz` | Operator 的 Helm chart |

版本寫在 `deploy.ps1` 開頭的清單裡。改了 `platform/otel/` 或 `tests/obs/backends.yaml` 用的版本，
那份清單要跟著改，再重新存一次。

⚠️ 只能用在同樣是 x86-64 的電腦上。
