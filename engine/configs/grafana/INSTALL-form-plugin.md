# 給管 Grafana 的人：裝回饋表單外掛

AIOps 的告警通知裡有一個「回饋」連結，點進來是 Grafana 裡的一個表單，值班和老師傅在那裡回報「真的／誤報、根因、怎麼看出來的」。表單要一個 Grafana 外掛。**不用換 Grafana image、不用改安全設定**，放一個資料夾、重啟就好。

| 項目 | 值 |
|---|---|
| 外掛 | Business Forms（`volkovlabs-form-panel`），Volkov Labs 出的，Apache 2.0，有 Grafana 官方簽章 |
| 版本 | **6.3.1**（Grafana 12.2 只能用這版；6.3.2 以後要 Grafana 12.3） |
| 下載 | `https://grafana.com/api/plugins/volkovlabs-form-panel/versions/6.3.1/download`（zip，約 1.4 MB；解開約 4.6 MB） |
| SHA-256 | `1eece14d67b2ffac4bbba18f5c69cde3f0f7aecef2276b0f1b33bc3cfb9998d0` |

## 步驟

**1. 下載、確認檔案沒被改過**（在連得到外網的機器上）

```bash
curl -L -o volkovlabs-form-panel-6.3.1.zip \
  https://grafana.com/api/plugins/volkovlabs-form-panel/versions/6.3.1/download
sha256sum volkovlabs-form-panel-6.3.1.zip    # 要跟上表的 SHA-256 一樣
```

**2. 放進 Grafana 的外掛資料夾**

Grafana 啟動時會載入外掛資料夾（`grafana.ini` 的 `[paths] plugins`，預設 `/var/lib/grafana/plugins`）裡的每個外掛。

```bash
unzip volkovlabs-form-panel-6.3.1.zip -d /var/lib/grafana/plugins/
ls /var/lib/grafana/plugins/volkovlabs-form-panel/plugin.json    # 有這個檔就對了
```

Grafana 跑在 K8s：外掛資料夾通常在掛給 Grafana 的持久磁碟上，把資料夾放進那個磁碟（`kubectl cp` 或掛 initContainer）。

**3. 放回饋表單的儀表板**

把這個資料夾的 `dashboards.yaml` 放成 `/etc/grafana/provisioning/dashboards/aiops.yaml`，`dashboards/aiops-feedback.json` 放進 `/etc/grafana/provisioning/dashboards/aiops/`。放之前把 `aiops-feedback.json` 裡兩處 `http://localhost:18080` 改成 AIOps 回饋服務的網址（AIOps 那邊會給，要是使用者瀏覽器連得到的）。

**4. 重啟 Grafana**

**5. 確認**

```bash
curl -s -u <admin>:<password> https://<grafana>/api/plugins/volkovlabs-form-panel/settings | grep -o '"signature":"[a-z]*"'
# 要看到 "signature":"valid"
```

Grafana 的「Dashboards」裡出現 AIOps 資料夾的「AIOps 回饋」，網址加 `?var-alert=<任一個告警 id>` 打開，表單上半部會自動填好那件告警。

## 要回給 AIOps 那邊的

- Grafana 的網址（例如 `https://grafana.example.internal`）：AIOps 的回饋服務要允許這個網址（`FEEDBACK_CORS_ORIGIN`），通知裡的連結也用它。

## 以後升級 Grafana

外掛不會跟著 Grafana 自動升級。升到 12.3 以後可以照同樣步驟換成新版（表單設定不用改），不換也能用。萬一升級後外掛不能用，表單打不開，**告警本身不受影響**；回饋暫時改用 AIOps 回饋服務自己的網頁。
