# 給管 Grafana 的人：裝回饋表單外掛

AIOps 的告警通知裡有一個「回饋」連結，點進來是 Grafana 裡的一個表單，值班和老師傅在那裡回報「真的／誤報、根因、怎麼看出來的」。表單要一個 Grafana 外掛。**不用換 Grafana image、不用改安全設定。**

| 項目 | 值 |
|---|---|
| 外掛 | Business Forms（`volkovlabs-form-panel`），Volkov Labs 出的，Apache 2.0，有 Grafana 官方簽章 |
| 版本 | **6.3.1**（Grafana 12.2、13.0.7 都實測過；6.3.2 以後要 Grafana 12.3 以上，本專案只驗過 6.3.1） |
| 下載 | `https://grafana.com/api/plugins/volkovlabs-form-panel/versions/6.3.1/download`（zip，約 1.4 MB；解開約 4.6 MB） |
| SHA-256 | `1eece14d67b2ffac4bbba18f5c69cde3f0f7aecef2276b0f1b33bc3cfb9998d0` |

## 做法：Grafana 跑在 K8s 上（用 initContainer）

Grafana 的 Pod 多一個 initContainer：Grafana 啟動前，從公司的 Nexus 下載外掛 zip、**核對 SHA-256**、解開到外掛資料夾。外掛資料夾用 emptyDir，Pod 每次重建都重裝一次，版本永遠是設定裡寫的那個。initContainer 用跟 Grafana **同一個 image**（裡面有 curl、unzip、sha256sum），不用多拉 image；Pod 不用連外網。

檔案在 [engine/deploy/grafana/](../../deploy/grafana/)：

| 檔案 | 用途 |
|---|---|
| `install-plugin.sh` | initContainer 跑的腳本：下載 → 核對 SHA-256（不符就失敗，Grafana 不啟動）→ 解開 |
| `plugin-init-patch.yaml` | **加進你們現有的 Grafana Deployment**：一個 initContainer、一個 volume、主容器一個掛載點，其他不動 |
| `grafana-example.yaml` | 沒有現成 Grafana 時的完整範例（Deployment＋Service＋回饋表單儀表板） |

**步驟**

1. **zip 放進 Nexus**（在連得到外網的機器上下載，確認 SHA-256 後上傳）：

   ```bash
   curl -L -o volkovlabs-form-panel-6.3.1.zip \
     https://grafana.com/api/plugins/volkovlabs-form-panel/versions/6.3.1/download
   sha256sum volkovlabs-form-panel-6.3.1.zip    # 要跟上表的 SHA-256 一樣
   # 上傳到 Nexus 的 raw repository，例如 .../repository/raw/grafana-plugins/volkovlabs-form-panel-6.3.1.zip
   ```

2. **改 `plugin-init-patch.yaml`**：`metadata.name` 改成你們的 Grafana Deployment 名字、主容器的 `name`（這裡假設 `grafana`）、`image` 跟你們主容器一樣、`PLUGIN_URL` 改成 Nexus 的網址。Nexus 用內部 CA 的話，`CURL_OPTS` 給 `--cacert <CA 檔>` 並把 CA 掛進來。

3. **套用**：

   ```bash
   kubectl -n <ns> create configmap grafana-plugin-installer --from-file=engine/deploy/grafana/install-plugin.sh
   kubectl -n <ns> patch deployment <grafana> --patch-file engine/deploy/grafana/plugin-init-patch.yaml
   ```

   Grafana 用 Helm chart 裝的話，不要直接 patch（下次 helm upgrade 會蓋掉）：把同樣的 initContainer、volume、volumeMount 寫進 chart 的 values（Grafana 官方 chart 有放額外 initContainer、volume 的欄位）。

4. **放回饋表單的儀表板**：[dashboards.yaml](dashboards.yaml) 和 [dashboards/aiops-feedback.json](dashboards/aiops-feedback.json) 放進 Grafana 的 `/etc/grafana/provisioning/dashboards/`（K8s 上用 ConfigMap 掛，`grafana-example.yaml` 有寫法）。放之前把 `aiops-feedback.json` 裡兩處 `http://localhost:18080` 改成 AIOps 回饋服務的網址（AIOps 那邊會給，要是使用者瀏覽器連得到的）。

5. **確認**：

   ```bash
   kubectl -n <ns> logs deploy/<grafana> -c install-plugins     # 要看到「已裝 volkovlabs-form-panel」
   curl -s -u <admin>:<password> https://<grafana>/api/plugins/volkovlabs-form-panel/settings | grep -o '"signature":"[a-z]*"'
   # 要看到 "signature":"valid"
   ```

   Grafana 的「Dashboards」裡出現 AIOps 資料夾的「AIOps 回饋」，網址加 `?var-alert=<任一個告警 id>` 打開，表單上半部會自動填好那件告警。

**出問題時**：initContainer 失敗 Pod 會停在 `Init:Error`，Grafana 不會起來（不會用一個不對的外掛跑起來）。`kubectl logs ... -c install-plugins` 會寫原因：SHA-256 不符、下載失敗（網址、憑證）、zip 裡沒有這個外掛。要先讓 Grafana 起來，把 patch 拿掉就回到原本的樣子，**告警本身不受影響**；回饋暫時改用 AIOps 回饋服務自己的網頁。

## 做法：Grafana 裝在一般主機上（不是 K8s）

把 zip 解開放進外掛資料夾（`grafana.ini` 的 `[paths] plugins`，預設 `/var/lib/grafana/plugins`）、重啟：

```bash
unzip volkovlabs-form-panel-6.3.1.zip -d /var/lib/grafana/plugins/
```

儀表板一樣放進 `/etc/grafana/provisioning/dashboards/`（第 4 步）。

## 要回給 AIOps 那邊的

- Grafana 的網址（例如 `https://grafana.example.internal`）：AIOps 的回饋服務要允許這個網址（`FEEDBACK_CORS_ORIGIN`），通知裡的連結也用它。

## 以後升級 Grafana

外掛不會跟著 Grafana 自動升級。升到 12.3 以後可以換新版：新的 zip 放 Nexus、改 `PLUGIN_URL` 和 `PLUGIN_SHA256`，表單設定不用改；不換也能用。
