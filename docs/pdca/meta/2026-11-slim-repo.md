# 精簡 repo：移除 GitLab CI yaml，盤點 yaml 與不必要的檔（2026-11）

**狀態**：進行中（已做：移除 CI yaml、`elasticsearch/` 收成一份 Kibana 檔；其餘只盤點，不動）　　**日期**：2026-11-05　　**相關**：分支 `claude/slim-repo`（從 `claude/api-beat-worker` 的已提交內容開出，用獨立 worktree，不碰那條分支沒 commit 的改動）
**模組**：③ 存、④～⑨、⑧ 出口

## Plan：這一次要達到什麼

- **需求**：repo 的 yaml 與資料夾愈長愈多；部署改由公司自己的流程做，repo 不需要 GitLab CI。
- **目的**：移除 4 份 `.gitlab-ci.yml`，文件不再叫人去按 CI 的 job；盤點還能精簡的 yaml 與檔案，列出來給使用者決定。
- **範圍**：做 — 刪 CI yaml、改掉文字參照、盤點。不做 — 其他任何刪除（盤點結果經使用者確認才動）；`elasticsearch-setup.sh` 的 `KEYS_OUT_DIR` 等 CI 用途的參數先留。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `.gitlab-ci.yml`、`aiops-engine/.gitlab-ci.yml`、`elasticsearch/.gitlab-ci.yml`、`grafana/.gitlab-ci.yml` | 刪除 | 部署不走 repo 的 CI |
| `elasticsearch/README.md` | 「用 GitLab CI 跑」改成「在 K8s 裡用 Job 跑 setup」：同樣的 helm 指令與三種情況表，不再說按哪個 job | CI 沒了，使用者要有手動做法；`setup-helmchart/` 本身沒改 |
| `docs/deploy-how-to.md`、`docs/architecture.md`、`aiops-engine/helmchart/values.yaml`、`elasticsearch/setup-helmchart/values.yaml`、`grafana/helmchart/templates/secret.yaml`、`grafana/code/build-official-values.py` | 刪掉或改寫提到 CI job 的註解與句子（含「`deploy-aiops-engine` 沒帶 `source=apm`」那條，CI 沒了就不成立） | 不留指向不存在檔案的說明 |
| `grafana/values-official.yaml` | 由 `build-official-values.py` 重新產生，只差一行註解 | 檔頭註解是產生的，不手改 |
| `elasticsearch/` | 刪 `README.md`、`code/`（setup 腳本、`retention.conf`、兩個 JSON）、`setup-helmchart/`（5 個檔）；新增 `kibana-dev-tools.txt`（ILM、template、engine key、Grafana key、選用 snapshot，整份貼進 Kibana Dev Tools） | 使用者的 ES 與 Grafana 本來就有，部署用手動貼；Job、RBAC、腳本、CI 用的 key 輸出都不需要。原本單獨的 role 一段拿掉，因為 key 本身就帶權限 |
| 引用處（`CLAUDE.md`、`README.md`、`docs/`、`aiops-engine/` 的 README 與註解、`sonar-project.properties`） | 指向 `kibana-dev-tools.txt` 的段落；`apm/README.md` 的 ES 兩節合成一節；`deploy-how-to.md` 第 1 節改成段落表 | 不留指向已刪檔的說明 |
| 沒動 | `tests/*_e2e.py` 的 docstring 還寫「先跑 elasticsearch-setup.sh」；`baseline/verification.md` 的歷史實測句 | e2e 不在這次範圍；實測紀錄是當時的措辭 |
| 沒動 | `docs/pdca/phase1-collect/2026-11-gitlab-ci.md` 等 PDCA 紀錄；`values.yaml` 的 `esApiKey`、`secretData`（沒有 CI 也能用 `--set` 帶 key） | PDCA 是當時的紀錄；那幾個參數要不要刪在下面「待決定」 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 刪掉 CI 後沒有文件指向不存在的檔、舊說法沒回來 | `tools/check-docs.py`；`git grep gitlab-ci` | 四項 0 個問題；剩下的 GitLab 字樣只有 rca README 的「貼到 GitHub、GitLab 或 Kibana 的 Markdown」，與 CI 無關 |
| chart 沒被註解修改弄壞 | `helm template` 跑 `es-setup`（`esSetup.enabled=true`）與 `grafana/helmchart` | 分別渲染出 10、7 個資源 |
| `values-official.yaml` 與 `helmchart/files/` 仍一致 | `build-official-values.py` 重新產生後 `--check` | 一致；與原檔差 1 行 |

沒測的：沒改任何 Python 邏輯，沒跑 pytest；CI 從來沒在 GitLab 上跑過，所以也沒有「刪掉影響既有 pipeline」的驗證。

## Act：沒解決的

**前提（使用者）：只有一種部署法，helm chart 格式 + Rancher Desktop + k3d。**下面是盤點，**沒有任何一項已動**，等使用者決定。盤點只看檔案與參照，沒有逐一驗證有沒有被用到。

| 類別 | 項目 | 為什麼可能不需要 |
|---|---|---|
| yaml | `grafana/values-official.yaml`（2,289 行，全部由 `helmchart/files/` 產生）、`grafana/code/build-official-values.py`、`plugin-init-patch.yaml`（套在既有 Grafana Deployment 上的 patch）、`INSTALL-form-plugin.md` | 使用者只有一種部署法：helm chart 格式、Rancher Desktop、k3d。官方 chart 的 values 與「改既有 Deployment」都不屬於這條路；`test_deploy.py` 有一條測試讀 `plugin-init-patch.yaml`，刪的時候要一起改 |
| yaml | `configs/baseline/` 的 OTel 版：`series`、`rules`（569 行）、`quality`（219）、`peer`、`trace`、`chronic`、`calendar` | 讀的是 `k8s-*`、`metrics-*.otel-*`，自建 Collector 已刪；現行資料是 APM。`baseline/prune.yaml` 與 `apm/schedule.yaml` 還在用，要留 |
| yaml | 四份排程表：`schedule/`、`apm/`、`factory/`、`metrics_only/`（各約 80～105 行） | 指令幾乎相同，只差設定檔路徑 |
| yaml | `source: otel｜apm` 開關與 chart 裡的三元運算 | 只剩 APM 時可拿掉 |
| 檔案 | Grafana 外掛安裝有四種做法：`grafana/plugins/*.zip`（1.4 MB 二進位）、`helmchart/load-plugin.sh`、`helmchart/files/install-plugin.sh`、`code/plugin-init-patch.yaml` ＋ `INSTALL-form-plugin.md` | 留一種 |
| 檔案 | `aiops-engine/.cache/calendar/*.json`（已被追蹤） | 是抓行事曆的快取，不該進 git |
| 檔案 | `sonar-project.properties`、`docs/pdca/meta/2026-10-sonar.md` | 公司有沒有用 SonarQube 沒確認 |
| 檔案 | `aiops-engine/fake-data/`、`interfaces/` | 假資料與接口範例 |
| 檔案 | engine／grafana chart 的 `esApiKey`、`secretData`、`secret.yaml` | 原本為 CI 把 key 組成 values；沒有 CI 與腳本後只剩手動 `--set` |
| 工作目錄 | 根目錄 6 個檔名是 Windows 路徑的 `.tar`（`C：\Project\…\image\*.tar`，未追蹤）、`e2e/`（未追蹤，`local-k3d/.env` 被忽略） | 不在 git 裡，是本機的殘留 |
| 工作目錄 | `.claude/worktrees/` 有 20 多個 worktree，多數對應已合或已放棄的分支 | 佔空間，也容易跑錯地方 |
| 不要動 | `api/`（原 `feedback_web/`）、`scheduler/`、`configs/*/schedule.yaml` | 另一條分支 `claude/api-beat-worker` 正在改成 api／beat／worker |

- **決定**：先只刪 CI yaml，其餘等使用者逐項確認。
- **下一輪**：使用者選定要刪／合併的項目後，各自一個 commit，刪之前先 `git grep` 確認沒有參照。
