# 各元件的 GitLab CI：ES 結構設定、collector、aiops-engine、grafana 分開部署（2026-11）

**狀態**：已完成（GitLab 上沒跑過）　　**日期**：2026-11-01　　**相關**：分支 `claude/gitlab-ci-es-grafana`；[elasticsearch/README.md](../../../elasticsearch/README.md)
**模組**：① 收值、③ 存、④～⑨ AIOps、⑧ 出口

## Plan：這一次要達到什麼

- **需求**：公司的 ES、Grafana 都已經有（或另有 cluster），部署走 GitLab CI；collector、aiops-engine、grafana、ES 設定這幾隻分開部署、各自一份 CI yaml。原本 `elasticsearch-setup.sh` 是人手動跑、API key 印在畫面上，三個 chart 的 Secret 也都要手動 `kubectl create secret`。
- **目的**：
  - 每個元件一份 `.gitlab-ci.yml`（`elasticsearch/`、`collector/`、`aiops-engine/`、`grafana/`），根目錄的只負責 `include`；job 都是手動觸發，只有該資料夾有改動才出現。
  - `elasticsearch-setup.sh` 在 CI 跑時 key 不印進 log，寫成 values 檔放 artifact；三個 chart 能從 values 建 Secret（使用者要求 key 寫在 chart 的 yaml）。
- **範圍**：
  - 做：四份 CI yaml；setup 腳本的 `KEYS_OUT_DIR`；三個 chart 的 `secret.yaml`（collector、aiops-engine、grafana）；對測試 ES 跑 setup、chart 渲染。
  - 不做：GitLab 上實跑（沒有 GitLab）；測試／文件檢查的 job；collector 多 cluster 自動展開（一個 job 一個 cluster，要幾個複製幾個）；健康檢查 key 自動放進 LB。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `.gitlab-ci.yml`（根） | `stages`（es-setup、build、deploy）＋ `include` 四個資料夾的檔 | 各元件分開，這裡只串起來 |
| `elasticsearch/.gitlab-ci.yml` | `es-setup`：alpine 裝 bash、curl、jq，跑 setup（`KEYS_OUT_DIR=keys`），`keys/` 當 artifact（`expire_in: 1 hour`、`access: developer`） | key 不進 log；artifact 短命、限權限 |
| `grafana/.gitlab-ci.yml`（改版） | 改用 Grafana **官方 chart**（`grafana/grafana`＋[`values-official.yaml`](../../../grafana/values-official.yaml)）：job 用 `kubectl` 建 Secret `grafana-env`（有 `es-setup` 的 `keys/grafana` 才建，server-side apply、不存 annotation），再 `helm upgrade`；新增 `check-grafana-values`（`test` stage，`build-official-values.py --check`，`code/` 改了沒重新產生就擋） | 公司用官方 chart；官方 chart 不建 Secret，key 不進 helm 的 values（我們自己的 chart 的 `secretData` 仍在，CI 的 Grafana job 不再用它） |
| `collector/`、`aiops-engine/` 的 `.gitlab-ci.yml` | 各自的部署 job：`needs` 接 `es-setup` 的 artifact（`optional: true`，沒跑 setup 也能部署）；有 key 檔才 `-f`；`rules` 是預設分支、該資料夾有改、手動，或在畫面 Run pipeline。aiops-engine 另有 `build-aiops-engine`（kaniko 推 Nexus，tag 用 commit 短碼）與 LLM 設定（變數寫成暫時的 values 檔）。Grafana 的 job 把 ES 網址、webhook、admin 密碼加上 key 組成 `secretData` | 分開部署；變數放 GitLab、不進 git |
| `elasticsearch/code/scripts/elasticsearch-setup.sh` | `issue_key`：沒設 `KEYS_OUT_DIR` 照舊印出來；有設就只寫 `writer`、`reader`、`engine`、`grafana`、`probe` 五個檔（失敗時說原因並停），結尾組 `collector-values.yaml`、`engine-values.yaml`、`grafana-values.yaml`（`umask 077`） | 人手動跑的行為不變 |
| 三個 chart 的 `templates/secret.yaml`、`values.yaml` | `esWriterApiKey`（collector）、`esApiKey`（aiops-engine）、`secretData`（grafana）有給才建 Secret（名字用原本的 `esWriterSecret`、`secret`、`envSecret`），加 `helm.sh/resource-policy: keep` | 空的就不建，沿用手動建的；`keep` 是之後沒帶 key 的部署不會被 helm 當成「不要了」把 Secret 刪掉 |

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| Grafana job 建 Secret 的方式 | 測試 cluster 另開 namespace：`kubectl create secret … --dry-run=client -o yaml \| kubectl apply --server-side`，再更新一次 | 建出 `ES_URL`、`GRAFANA_ES_API_KEY` 兩個欄位；annotation 是空的（沒把內容存進去）；第二次更新成功；測完刪 namespace |
| 五份 CI yaml 能解析、各 job 的資源名對得上 chart | `yaml.safe_load`；`helm template` 看 DaemonSet／Deployment 名 | 都能解析；collector 的是 `otel-node-collector`、`otel-cluster-collector`（第一版寫成 `collector-node`，改正）；aiops-engine、grafana 是 `aiops-engine`、`grafana` |
| 三個 chart 沒給 key 時不建 Secret、給了才建 | `helm template` 有無 `--set`／`-f` | collector、grafana 沒給 0 個 Secret，給了建對的名字與欄位（`ES_WRITER_API_KEY`、`GRAFANA_ES_API_KEY`）；aiops-engine 本來就有 `aiops-llm`，給 `esApiKey` 多一個 `aiops-secrets`（`ES_API_KEY`） |
| setup 的 CI 模式 key 不進 log、檔案內容對 | 對測試 ES 用 `KEYS_OUT_DIR` 跑 | log 裡沒有 `encoded`、沒有那把 key（0 次）；5 個 key 檔、3 個 values 檔格式對；實測 writer key 打 `GET /` 是 403（只能寫）、grafana 與 probe 是 200 |

**沒測到的**：官方 chart 沒下載實裝（`helm repo add` 能不能連到公司的 helm proxy、欄位名對不對實際版本）；GitLab 上實跑（`rules: changes`、`needs: optional`、`artifacts:access`、kaniko、`dtzar/helm-kubectl` image 的版本都沒驗）；檔案權限 600 只在 Linux 的 CI 有效，在 Windows 上驗時是 644；Grafana 帶 `secretData` 真的 `helm upgrade` 到 cluster 沒做。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| **API key 出現在 artifact 與 Helm release** | 設計：key 寫在 chart values | artifact 有 key（1 小時後刪、限 Developer 以上）；`helm upgrade` 把 values 存在 cluster 的 Helm release Secret 裡，能讀該 namespace Secret 的人看得到 key | 跟 K8s Secret 本身同一個讀取權限範圍；要更保險改存公司的密碼管理 |
| setup 每次跑都發新 key、舊的不會作廢 | 腳本本來的行為（`rotate-writer-key.sh` 才是換 key 用的） | 重跑 es-setup 部署會累積舊 key | 只在要換 key 時跑 es-setup；舊 key 到 ES 手動作廢 |
| 健康檢查 key（`keys/probe`）要人從 artifact 手動取去放 LB | 範圍外 | artifact 1 小時就刪 | 放 LB 的流程另外決定 |
| `elasticsearch-setup.sh` 對這個測試 ES 本來就失敗（`curl: (3) URL rejected`，exit 3） | 跑 CI 模式時；master 原版同樣失敗 | 在「已存在的 metrics、trace 套用新的保留天數」那段 rollover 迴圈，測試 ES 當下有怪的資料流（`metrics-bedge.otel-it` 之類，e2e 留下的）；沒查根因 | 驗證時用關掉 `set -e` 的暫時複本繞過；乾淨的 ES 沒遇過 |
| collector 一個 job 一個 cluster | 設計 | 多個應用 cluster 要複製 job | 有幾個 cluster、kubeconfig 怎麼管，由公司決定 |
