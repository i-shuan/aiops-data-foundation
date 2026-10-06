# commands — 這個專案真正有的指令

⚠️ **只用下面列的。不得臆測、不得順手補一個。**

## 沒有的東西（不要用）

| 指令 | 為什麼沒有 |
| --- | --- |
| `npm` 任何子指令 | 沒有 `package.json`，這個 repo 沒有 Node |
| `make` | 沒有 `Makefile` |
| `pytest` | `tests/` 底下是 bash 腳本，加一支由它呼叫的 `check.py` |
| `helm lint` / `kubeconform` | 0 處。chart 的檢查在 `tests/validate.sh` 裡 |
| `./tests/tenant-routing/run.sh`、`./tests/ap1105-sim/run.sh`、`./tests/routing/run.sh` | 已移除。它們測的是 VictoriaMetrics 的做法，在 branch `chore/ai-native-scaffold` |

## 從 repo 根目錄

```bash
./tests/validate.sh                        # 不需要 cluster，要 python（PyYAML）和 helm
./tests/obs/run.sh                         # 要 cluster obs-test。部署、查分流、弄慢看得到，約 5 分鐘
./tests/obs/run.sh outage                  # 停機補送，約 7 分鐘。要先跑過上面那個
./tests/obs/run.sh broken nospoof          # 改壞的設定一定要 FAIL。另有 retry0、nopvc，各約 8 分鐘
python tools/sync-rules.py                 # 紅線三份一致嗎；--fix 從 AGENTS.md 複製
python tools/check-md-style.py -v          # md 格式違規，逐條列位置
python .claude/hooks/test_guard_git.py -v  # 改了 guard-git.py 之後
```

## 本機的 k3d

`tests/obs/run.sh` 用 cluster `obs-test`，不建也不刪。先手動建，五行照順序；後兩行裝 trace 要的 Operator，測試環境不裝 cert-manager，憑證由 Helm 自己產生：

```bash
k3d cluster create obs-test --image rancher/k3s:v1.30.14-k3s2 --no-lb --k3s-arg "--disable=traefik@server:0" --k3s-arg "--disable=metrics-server@server:0" --kubeconfig-switch-context=false --timeout 240s
kubectl config set-cluster k3d-obs-test --server="https://127.0.0.1:$(docker port k3d-obs-test-server-0 6443/tcp | head -1 | sed 's/.*://')"
docker pull busybox:1.36 && k3d image import busybox:1.36 -c obs-test
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm --kube-context k3d-obs-test install opentelemetry-operator open-telemetry/opentelemetry-operator --version 0.124.1 -n otel-operator --create-namespace --set admissionWebhooks.certManager.enabled=false --set admissionWebhooks.autoGenerateCert.enabled=true --wait
```

| ⚠️ | 為什麼 |
| --- | --- |
| `kubectl`、`helm` 一律帶 `--context k3d-obs-test` / `--kube-context k3d-obs-test` | 紅線。目前的 context 不一定是它 |
| 建 cluster 要帶 `--image rancher/k3s:v1.30.14-k3s2` | 這台的 docker 是 cgroup v1，預設的 k3s v1.35 起不來，k3d 卡到逾時 |
| 建 cluster 要帶 `--kubeconfig-switch-context=false` | 預設會切換目前的 context。刪掉目前 context 指向的 cluster 時也會自動換 |

## 不要讀、不要遞迴搜尋

`.git/`、`__pycache__/`、`*.tar`（image tar，100 多 MB）。

## 技術棧

| 層 | 用什麼 | 版本寫在哪 |
| --- | --- | --- |
| 收集 | OpenTelemetry Collector contrib 0.161.0；trace 的自動埋點靠 Operator v0.160.0（chart 0.124.1） | `platform/otel/`、上面的安裝指令 |
| 儲存 | Elasticsearch 9.5.3，免費版。repo 裡只有測試用的 | `tests/obs/backends.yaml` |
| app | Python，各自一個 `Dockerfile` | `app/<app>/` |
| chart | 每個 app 自帶 templates | `app/<app>/k8s/` |
| 部署（未實測） | ArgoCD ApplicationSet | `platform/argocd/applicationset.yaml` |

## 這台開發機的坑（Windows + Git Bash）

| 坑 | 症狀 | 做法 |
| --- | --- | --- |
| bash heredoc 吃掉反斜線 | 寫進檔案的 regex、`\1`、`\r` 少一層或變成控制字元，**不報錯** | 內容含反斜線就用檔案寫，不要用 heredoc |
| console 是 cp950 | 印 `✅` 直接 `UnicodeEncodeError`，退出碼變 1 | 腳本開頭 `sys.stdout.reconfigure(encoding="utf-8")`，或 `PYTHONIOENCODING=utf-8` |
| Windows 的 python 印的是 CRLF | bash 的 `read` 進來的最後一欄多一個 CR | 接 `tr -d '\r'` |
| Git Bash 改寫 `/` 開頭的參數 | 容器路徑變成 `C:/Program Files/Git/...` | 指令前加 `MSYS_NO_PATHCONV=1` |
| `kubectl` / `helm` 是 Windows 程式 | 看不懂 Git Bash 的 `/tmp/...` | 用 `cygpath -m` 轉成 `C:/...` |
| k3d 的 image store 跟本機 docker 分開 | build 完 pod 還在跑舊的 | `k3d image import <image> -c <cluster>` |
| k3d 寫進 kubeconfig 的位址是 `host.docker.internal` | `kubectl` 連不上，一直逾時 | `kubectl config set-cluster <ctx> --server=https://127.0.0.1:<port>` |
