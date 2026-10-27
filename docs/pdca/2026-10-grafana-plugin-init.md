# Grafana 在 K8s 上用 initContainer 裝回饋表單外掛（2026-10）

**狀態**：腳本與 manifest 完成，docker 模擬驗過；**K8s／k3d 上還沒跑**（執行 k3d 的權限被擋）　　**日期**：2026-10-26　　**相關**：分支 `claude/grafana-initcontainer`；前置 [grafana-form](2026-10-grafana-form.md) 的 Act「K8s 上放外掛的三種做法還沒實測」，選 B
**模組**：⑧ 出口、⑨ feedback

## Plan：這一次要達到什麼

- **需求**：回饋表單要 Grafana 裝 Business Forms 外掛 6.3.1。Grafana 跑在 K8s 時，Pod 重建外掛就沒了；正式環境的 Pod 通常不能連 grafana.com。要一個**不靠外網、Pod 重建也自動裝回去、版本固定**的做法。
- **做法（B）**：Grafana 的 Pod 加一個 initContainer，在 Grafana 啟動前：從公司內部的檔案庫（Nexus）下載外掛 zip → **核對 SHA-256**（不符就不啟動）→ 解開到一個共用的 volume；Grafana 主容器把這個 volume 掛成外掛資料夾。initContainer 用**同一個 Grafana image**（裡面有 curl、unzip、sha256sum），不用多拉 image。
- **目的**：
  1. `engine/deploy/grafana/`：給管 Grafana 的人的 initContainer 片段（加進他們現有的 Deployment），和一份能直接跑的完整範例（Deployment＋Service＋儀表板 ConfigMap）。
  2. 實際跑一次：外掛載入、簽章 valid、儀表板出現；checksum 錯時起不來；重建後外掛自動回來。
  3. `INSTALL-form-plugin.md` 改成以這個做法為主。
- **範圍**：不做 Helm chart 的寫法（公司 Grafana 怎麼部署還不知道）；不做 Ingress。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `engine/deploy/grafana/install-plugin.sh`（新） | initContainer 跑的腳本：`curl` 下載（重試 3 次、逾時 120 秒、`CURL_OPTS` 可給內部 CA）→ `sha256sum` 核對，不符就 exit 1 → `unzip` → 確認 zip 裡有 `<PLUGIN_ID>/plugin.json` → 放進外掛資料夾。記版本的小檔 `.<id>.sha256` 放在**外掛資料夾外面**；同一個 volume 已經是同版本就跳過 | 不靠外網、版本固定、檔案被換掉或下載不完整就不啟動；記版本的檔放裡面 Grafana 驗簽章會說被改過 |
| `engine/deploy/grafana/plugin-init-patch.yaml`（新） | strategic merge patch：只加 initContainer（用 Grafana 同一個 image）、emptyDir 的外掛資料夾、腳本的 ConfigMap、主容器一個掛載點 | 公司已經有 Grafana：只加東西、不動他們原本的設定 |
| `engine/deploy/grafana/grafana-example.yaml`（新） | 完整範例：Deployment（同樣的 initContainer、`GF_PLUGINS_PREINSTALL_DISABLED`、儀表板 ConfigMap）＋ Service | 沒有現成 Grafana、或本機 k3d 測 |
| `engine/deploy/grafana/README.md`（新）、`engine/configs/grafana/INSTALL-form-plugin.md`（改寫） | 安裝說明改成 K8s 用 initContainer 為主（放 Nexus、改 patch、套用、確認、出問題怎麼看、Helm 不要直接 patch）；一般主機用解開放資料夾 | 給管 Grafana 的人照做 |
| `engine/tests/test_deploy.py` | 腳本用 `file://` 當 Nexus：正常裝、再跑跳過、版本檔不在外掛資料夾裡、SHA-256 不符什麼都不裝、外掛 id 錯、檔案不存在；兩份 manifest 釘的 id、版本、SHA-256 跟安裝說明一致、外掛資料夾是 emptyDir | 以後改壞會被抓到 |

## Check：測試的目的與結果

**K8s／k3d 沒有跑**：下載並執行 k3d、kubectl 這一步被這個環境的權限擋下。改用 docker 模擬 initContainer 做的事（同一個 Grafana 12.2.0 image、同一份腳本、本機 HTTP 服務當 Nexus），再讓 Grafana 掛同一個 volume 啟動。

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 正常裝 | 用 Grafana image 跑腳本，裝進一個空的 volume | 「已裝 volkovlabs-form-panel」，exit 0 |
| Grafana 認得、簽章沒壞 | Grafana 掛那個 volume 啟動，查外掛 | `signature: valid`、版本 6.3.1；儀表板「AIOps / AIOps 回饋」載入 |
| 已經裝過不重裝（PVC 的情況） | 同一個 volume 再跑一次 | 「已經是這個版本，不用重裝」，exit 0 |
| 檔案被換掉不裝 | SHA-256 給錯 | 「SHA-256 不符：預期…下載到的是…」，exit 1，volume 是空的 |
| 網址錯 | 404 | curl exit 22，失敗 |
| 單元測試 | pytest（`file://`）、ruff | 過 |

## Act：Check 後沒改善的、還沒解決的

- **K8s 上的行為沒驗**（在什麼情況下發現：要下載 k3d 時被擋）：initContainer 失敗 Pod 停在 `Init:Error`、emptyDir 在 Pod 重建時清空重裝、ConfigMap 掛腳本、`fsGroup` 讓 uid 472 寫得進 emptyDir，這些是 K8s 的標準行為，但沒有實際看過。要在有 k3d 或測試 cluster 的地方跑 `grafana-example.yaml` 一次。
- **Helm chart 的寫法沒給**：公司 Grafana 如果用 Helm 裝，直接 patch 會在下次升級被蓋掉，要寫進 values；chart 版本不同欄位名稱不同，等知道他們用哪一版再補。
- **Nexus 的網址、內部 CA 是假的**：`PLUGIN_URL` 要換；Nexus 用內部憑證的話要給 `CURL_OPTS=--cacert …` 並掛 CA，這段沒測。
- 每次 Pod 重建都會下載一次（約 1.4 MB），Nexus 掛掉時 Grafana 起不來。要避免可以把外掛資料夾改掛 PVC（腳本已經會跳過已裝的同版本），代價是多一個 PVC。
