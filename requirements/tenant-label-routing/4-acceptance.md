# Acceptance — 依 `tenant` label 寫進對應的 AccountID

> 每一項都要是**可執行的指令加預期輸出**。分批驗收，前一批全過才做下一批。

## 第零批：先確認地基（已做）

在獨立的 k3d cluster `tenant-test` 上用最小設定實測，版本跟 AP1105 一樣：
operator v0.74.0、vmagent / vminsert / vmselect / vmstorage v1.148.0。

| # | 要確認的 | 實際結果 | 結果 |
| --- | --- | --- | --- |
| 0-1 | operator 認得全域 relabel 與 `scrapeClasses`，不會默默裁掉 | server-side dry-run 回來的 spec：全域 7 條、抓取 2 條都在，`regex: "^$"` 沒被吃掉 | ☑ |
| 0-2 | vminsert 依 `vm_account_id` 分租戶 | `tenant: icsd` 的樣本只在 1003；0、1001、1002、1004 都是 0 筆 | ☑ |
| 0-3 | 沒貼、打錯的進兜底 | `t-none`、`t-typo` 只在 1000，樣本上是 `tenant="platform"` | ☑ |
| 0-4 | 偽造被擋下 | 自帶 `tenant="tsid"`、`vm_account_id="1001"` 的 pod 只在 1000；自帶的 `tenant` 變成 `exported_tenant` | ☑ |
| 0-5 | 洞是真的 | 拿掉「清掉 `vm_account_id`」那條規則後，同一個 pod 的樣本出現在 1001。已還原 | ☑ |

⚠️ 「對不到就兜底」的規則實測用的是 `regex: "^$"`。空字串沒有測：API 會收下，
但 operator 產生設定時若把它當成沒寫，預設值是「全部符合」，每個租戶都會被改成
`platform` 而且不報錯。所以一律寫 `^$`，`tests/validate.sh` 會擋空字串。

## 第一批：改名

先跑這段，#1-3 ~ #1-5 用暫存複本，不動 repo 裡的檔：

```bash
mk() { T=$(mktemp -d); mkdir -p "$T/tests"; cp -r app platform "$T/"; cp tests/validate.sh "$T/tests/"; E="$T/app/eqp-collector/k8s"; }
run() { "$T/tests/validate.sh" 2>&1 | grep -A1 -E "$1|^(PASS|FAIL)|Traceback"; echo "exit=${PIPESTATUS[0]}"; }
```

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 1-1 | `./tests/validate.sh; echo "exit=$?"` | `PASS`、`exit=0`；第 3 段四個 app 都列出 `tenant=…` | ☑ |
| 1-2 | `grep -n -E "^\s+group:" app/*/k8s/values.yaml` | 沒有輸出 | ☑ |
| 1-3 | `mk; sed -i 's/tenant: imc/group: imc/' "$E/values.yaml"; run eqp-collector` | 第 1 段 `FAIL`，訊息有「改名」；`exit=1` | ☑ |
| 1-4 | `mk; sed -i 's/tenant: imc/tenant: it.imc/' "$E/values.yaml"; run eqp-collector` | 第 1 段 `FAIL`，訊息有「不是允許的值」；`exit=1` | ☑ |
| 1-5 | `mk; sed -i 's/tenant: imc/tenant: tsid/' "$E/values.yaml"; run eqp-collector` | 第 1 段 `OK`；第 3 段 `tenant=tsid`；`PASS`、`exit=0` | ☑ |
| 1-6 | `./tests/validate.sh \| grep -E "自我檢查\|→" \| grep -c FAIL` | `0` | ☑ |

## 第二批：對照表、防偽造、單條 `remoteWrite`

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 2-1 | `./tests/validate.sh; echo "exit=$?"` | `PASS`、`exit=0`；第 2 段印出對照：`platform=1000 tsid=1001 bsid=1002 icsd=1003 imc=1004` | ☑ |
| 2-2 | `./tests/validate.sh \| grep "自我檢查" \| grep -E "清掉\|重複\|兜底\|兩條\|空字串"` | 五條都在、都是 `OK`：拿掉清 `vm_account_id` 那條、AccountID 重複、兜底不在最後、`remoteWrite` 兩條、兜底的 `regex` 是空字串，全部被擋下 | ☑ |
| 2-3 | `python -c "import yaml,io;print(len(yaml.safe_load(io.open('platform/vmstack-values.yaml',encoding='utf-8'))['vmagent']['spec']['remoteWrite']))"` | `1` | ☑ |
| 2-4 | `grep -n -w group README.md app/eqp-collector/README.md` | 剩下的每一處都是在講歷史或 Fluentd 的欄位，逐條列在下面「人工確認」 | ☑ |
| 2-5 | `python tools/check-md-style.py README.md app/eqp-collector/README.md` | 違規數不比改之前多（5 和 4） | ☑ |

## 第三批：k3d 實測

| # | 指令 | 預期輸出 | 結果 |
| --- | --- | --- | --- |
| 3-1 | `./tests/tenant-routing/run.sh; echo "exit=$?"` | 四個租戶各自只在自己的 AccountID；沒貼、打錯、偽造只在 1000；`PASS`、`exit=0` | ☑ |
| 3-2 | `VMSTACK_VALUES=<拿掉清 vm_account_id 那條的複本> ./tests/tenant-routing/run.sh; echo "exit=$?"` | 偽造那一項 `FAIL`；`exit=1` | ☑ |
| 3-3 | `VMSTACK_VALUES=<icsd 對到 1004 的複本> ./tests/tenant-routing/run.sh; echo "exit=$?"` | `icsd` 那一項 `FAIL`；`exit=1` | ☑ |
| 3-4 | 跑 3-1 前後各一次 `kubectl config current-context` | 一樣，沒有被切走 | ☑ |

## 實作時發現、跟 plan 不一樣的地方

| 事項 | 說明 |
| --- | --- |
| 測試要等 vmagent 真的載入新規則 | operator 更新設定後 vmagent 不會馬上重新載入。`check.py loaded` 會等它實際載入的規則跟要測的一樣 |
| 檢查前要等 90 秒 | 等太短時，剛跑完改壞的複本再跑正常的，會把上一輪的資料算進來而誤判 FAIL。實際遇到過，改成 90 秒後連跑三輪（壞、壞、好）結果正確 |
| 這台機器的 k3s 要釘 v1.30 | docker 是 cgroup v1，k3d 預設的 k3s v1.35 起不來。`run.sh` 已寫死，`.agents/project/commands.md` 有記 |
| 根目錄 `README.md` 的 overlay 步驟縮短了 | 原本的逐步指令是三把 token 的版本。新 overlay 在 AP1105 模擬環境的完整流程留給需求 4 |

## 人工確認

| # | 怎麼做 | 看到什麼算過 | 結果 |
| --- | --- | --- | --- |
| 1 | 讀 `README.md` 開頭到「新增一個 App」 | 說的是 `tenant`、五個值、一條 `remoteWrite`，沒有殘留三把 token 的說法 | ☐ |
| 2 | 看 `.agents/project/commands.md` | `tests/routing`、`tests/ap1105-sim` 標明測的是舊分流、目前會失敗 | ☐ |

## 已知未涵蓋

| 沒證明 | 為什麼 |
| --- | --- |
| 中央真正的 vmauth 接受這個寫入路徑 | 路徑是佔位，待 Infra 確認。本機的 vmauth 是自己起的 |
| 正式 cluster 的 operator 版本也認得這些欄位 | 只測了 v0.74.0。其他版本要在該 cluster 用 server-side dry-run 確認 |
| ArgoCD 那條路 | `tests/routing` 這次不修 |
| 查詢端的隔離 | 總綱已列為不做 |

## 寫回文件

| 改了什麼 | 動到哪份文件 | 狀態 |
| --- | --- | --- |
| `group` 改 `tenant`、值與分流方式 | `README.md`、`app/eqp-collector/README.md` | ☑ |
| 兩支舊測試過期、新增一支 | `.agents/project/commands.md`、`README.md` 的「Tests」 | ☑ |
