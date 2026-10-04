# commands — 這個專案真正有的指令

⚠️ **只用下面列的。不得臆測、不得順手補一個。**

## 沒有的東西（不要用）

| 指令 | 為什麼沒有 |
| --- | --- |
| `npm` 任何子指令 | 沒有 `package.json`，這個 repo 沒有 Node |
| `make` | 沒有 `Makefile` |
| `pytest` | `tests/` 底下是 bash 腳本加 `check.py`，不是 pytest |
| `helm lint` / `kubeconform` | 0 處。chart 的檢查在 `tests/validate.sh` 裡 |

## 從 repo 根目錄

```bash
./tests/validate.sh                        # 不需要 cluster，要 python（PyYAML）和 helm
python tools/sync-rules.py                 # 紅線三份一致嗎；--fix 從 AGENTS.md 複製
python tools/check-md-style.py -v          # md 格式違規，逐條列位置
python tools/check-md-style.py AGENTS.md   # 只看指定檔案
python .claude/hooks/test_guard_git.py -v  # 改了 guard-git.py 之後
```

⚠️ `check-md-style.py` 不帶檔名會掃全 repo，而既有的三份 `README.md` 寫在這套
規範之前，有違規。新寫或改過的檔要指名跑，要是 0。

## 要本機 k3d 的（跑很久、會動 cluster）

```bash
./tests/routing/run.sh          # E2E，真的經過 ArgoCD。前提見 README.md 的 Tests
./tests/ap1105-sim/run.sh       # 會先刪掉 k3d cluster ap1105 重建
./tests/ap1105-sim/run.sh routed   # 只跑某一步之後的步驟
```

⚠️ 這兩支不要順手跑。`ap1105-sim/run.sh` 不帶參數會**刪掉重建** cluster。

## 不要讀、不要遞迴搜尋

`.git/`、`__pycache__/`、`*.tar`（image tar，100 多 MB）、
`tests/ap1105-sim/.work/`（`run.sh` 的工作檔，含 `helm get values` 的輸出）。

## 技術棧

| 層 | 用什麼 | 版本寫在哪 |
| --- | --- | --- |
| 部署 | ArgoCD ApplicationSet（E2E 用 v3.5.3 core） | `platform/applicationset.yaml` |
| metrics | victoria-metrics-k8s-stack（vmagent → vmauth） | `platform/vmstack-values.yaml` |
| log | Fluentd → Elasticsearch | `platform/fluent.conf` |
| app | Python，各自一個 `Dockerfile` | `services/<app>/` |
| chart | 每個 app 自帶 templates | `applications/<app>/helm/` |
| 執行環境 | k8s；本機 k3d | `tests/routing/run.sh`、`tests/ap1105-sim/run.sh` |

## 這台開發機的坑（Windows + Git Bash）

| 坑 | 症狀 | 做法 |
| --- | --- | --- |
| bash heredoc 吃掉反斜線 | 寫進檔案的 regex、`\1` 少一層，**不報錯** | 內容含反斜線就用檔案寫，不要用 heredoc |
| console 是 cp950 | 印 `✅` 直接 `UnicodeEncodeError`，退出碼變 1 | 腳本開頭 `sys.stdout.reconfigure(encoding="utf-8")`，或 `PYTHONIOENCODING=utf-8` |
| Git Bash 改寫 `/` 開頭的參數 | 容器路徑變成 `C:/Program Files/Git/...` | 指令前加 `MSYS_NO_PATHCONV=1` |
| `kubectl` / `helm` 是 Windows 程式 | 看不懂 Git Bash 的 `/tmp/...` | 用 `cygpath -m` 轉成 `C:/...` |
| 檔名不分大小寫 | `git rm` 會把只差大小寫的另一個檔一起刪掉 | 新舊檔名只差大小寫時先確認 |
| k3d 的 image store 跟本機 docker 分開 | build 完 pod 還在跑舊的 | `k3d image import <image> -c <cluster>` |
