#!/usr/bin/env bash
# 檢查每個 app/<app>/k8s/values.yaml，以及它們會變成什麼 namespace label。
#
#   ./tests/validate.sh
#
# 不需要 cluster，只要 python（PyYAML）和 helm。全部通過 exit 0，任何一項不過 exit 1。
#
#   1. values.yaml 合法：argocd 與 observability 必填欄位、production 不可自動同步、
#      namespace 不能被兩個 app 共用、chart 渲染得出來
#   2. tenant 只能是允許的值，一個 namespace 一個。允許什麼直接從 platform/victoriametrics/vmstack-values.yaml
#      的租戶對照（vmagent.spec.inlineRelabelConfig）讀，不另外維護清單。
#      對照本身也檢查：先清掉進來的 vm_account_id、AccountID 不重複、兜底在最後。
#      雙寫與暫存：remoteWrite 剛好兩條、網址和 token 不一樣、開 statefulMode、有暫存上限、PVC 裝得下。
#      platform/victoriametrics/site-<廠區>.yaml 只能有 site，值從 remoteWrite 的 token key 推出，不另外維護清單。
#      platform/victoriametrics/vmagent-routing-overlay.yaml 的那幾段要跟 vmstack-values.yaml 一模一樣
#      （只有 url 可以不同），而且不能帶那幾段以外的設定
#   3. platform/argocd/applicationset.yaml 的 templatePatch 渲染出來的 namespace label，
#      要跟 values.yaml 寫的一模一樣；沒寫的欄位不能變成空字串的 label
#
# app 有兩種，看 chart 自己的檔案分辨，不另外維護清單：
#   - values.yaml 有 argocd 區塊：走 ArgoCD，照上面檢查
#   - 沒有 argocd 區塊、有 templates/namespace.yaml：手動 Helm，label 是 chart 自己貼的。
#     不查 argocd 欄位，observability 照查，第 3 項改成渲染 chart、取出 Namespace 的 label 來比
#   - 兩個都有或都沒有：擋下
#   4. 自我檢查：內建的錯誤寫法一定要被擋下，渲染一定要照原值，否則這支腳本本身就是壞的
#
# 渲染用的是 helm。helm 和 ArgoCD 都是 Go template + sprig，但這不是 ArgoCD 本身；
# 真的經過 ArgoCD 的驗證在 tests/routing。
set -euo pipefail
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."

python - <<'PY'
import copy, glob, io, os, re, subprocess, sys, tempfile
import yaml

APPSET = "platform/argocd/applicationset.yaml"
VMSTACK = "platform/victoriametrics/vmstack-values.yaml"
OVERLAY = "platform/victoriametrics/vmagent-routing-overlay.yaml"
ARGOCD_FIELDS = ["namespace", "project", "environment", "owner", "criticality", "autoSync"]
OBS_FIELDS = ["dept", "tenant", "system"]
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
failed = 0


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


TENANT_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
KEEP = "inlineRelabelConfig 的寫法只能是：清掉 vm_account_id 的 labeldrop、tenant → vm_account_id 的對照、最後兩條兜底"


def routing(spec):
    """vmagent.spec 的租戶分流。回傳 (對照表 {tenant: AccountID}, 錯誤清單)。

    租戶由 vmagent 自己填的 vm_account_id 決定，vmauth 不檢查，所以這裡每一項寫錯
    都不會報錯，資料只是安靜地進到別的租戶。
    """
    e, table = [], {}

    def add(name, acct):
        if name in table:
            e.append("tenant %s 在對照裡出現兩次" % name)
        if acct in table.values():
            e.append("AccountID %s 重複，%s 的資料會跟別的租戶混在一起" % (acct, name))
        table[name] = acct

    rules = spec.get("inlineRelabelConfig") or []
    first = rules[0] if rules else {}
    if first.get("action") != "labeldrop" or \
            not {"vm_account_id", "vm_project_id"} <= set(str(first.get("regex", "")).split("|")):
        e.append("inlineRelabelConfig 第一條要是清掉 vm_account_id|vm_project_id 的 labeldrop，"
                 "不然 app 自己吐這個 label 就能寫進別人的租戶")
    for r in rules[1:-2]:
        name, acct = str(r.get("regex", "")), str(r.get("replacement", ""))
        if (r.get("source_labels") != ["tenant"] or r.get("target_label") != "vm_account_id"
                or r.get("action", "replace") != "replace"
                or not TENANT_NAME.match(name) or not acct.isdigit()):
            e.append("%s。看不懂這條：%s" % (KEEP, r))
        else:
            add(name, acct)
    tail = rules[-2:]
    shape = [(r.get("source_labels"), r.get("regex"), r.get("target_label")) for r in tail]
    fallback = None
    if shape != [(["vm_account_id"], "^$", "tenant"), (["vm_account_id"], "^$", "vm_account_id")]:
        e.append("inlineRelabelConfig 最後兩條要是兜底：vm_account_id 是空的就先把 tenant、再把 vm_account_id "
                 "改成兜底租戶。regex 寫 ^$，寫成空字串可能被當成沒寫而變成全部符合")
    else:
        fallback = str(tail[0].get("replacement", ""))
        add(fallback, str(tail[1].get("replacement", "")))

    sc = (spec.get("scrapeClasses") or [{}])[0]
    if not sc.get("default") or not (sc.get("attachMetadata") or {}).get("namespace"):
        e.append("scrapeClasses 第一個要是 default: true 而且 attachMetadata.namespace: true，"
                 "不然讀不到 namespace 的 label")
    pair = [{"source_labels": ["__meta_kubernetes_namespace_label_tenant"], "target_label": "tenant"},
            {"source_labels": ["tenant"], "regex": "^$", "target_label": "tenant", "replacement": fallback}]
    rc = sc.get("relabelConfigs") or []
    if not any(rc[i:i + 2] == pair for i in range(len(rc))):
        e.append("scrapeClasses 要先貼 namespace 的 tenant，緊接著把沒貼的補成 %s（regex 寫 ^$）。"
                 "少了第二條，沒貼 tenant 的 namespace 裡的 app 自己吐 tenant 就能冒充別人" % fallback)

    return table, e


UNITS = {"": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4,
         "KI": 1024, "MI": 1024 ** 2, "GI": 1024 ** 3, "TI": 1024 ** 4}


def size(text):
    """50Gi（Kubernetes 的寫法）或 20GiB（VictoriaMetrics 的寫法）換成 bytes。看不懂回 None。"""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]i?)?B?$", str(text), re.I)
    return float(m.group(1)) * UNITS[(m.group(2) or "").upper()] if m else None


def writes(spec):
    """vmagent.spec 的雙寫與暫存。回傳 (目的地的廠區清單, 錯誤清單)。"""
    e, sites = [], []
    rws = spec.get("remoteWrite") or []
    if len(rws) != 2:
        e.append("remoteWrite 要剛好兩條，兩座各一條（現在 %d 條）。少一條就沒有備援，那一座停機時資料會永久遺失"
                 % len(rws))
    urls = [str(rw.get("url", "")) for rw in rws]
    if any("/insert/multitenant/" not in u for u in urls):
        e.append("remoteWrite 的 url 要是 multitenant 端點（/insert/multitenant/...），"
                 "不然 vm_account_id 不會被當成租戶")
    if len(rws) == 2 and urls[0] == urls[1]:
        e.append("兩條 remoteWrite 的網址一樣，等於同一座寫兩次，沒有備援")
    tokens = [rw.get("bearerTokenSecret") or {} for rw in rws]
    if not all(t.get("name") and t.get("key") for t in tokens):
        e.append("remoteWrite 要用 bearerTokenSecret 帶 token，token 不能寫在這份裡")
    else:
        sites = [str(t["key"]) for t in tokens]
        if len(set(sites)) != len(sites):
            e.append("兩條 remoteWrite 帶同一把 token（key 都是 %s）。兩座各發各的，另一座會一直 401" % sites[0])

    if spec.get("statefulMode") is not True:
        e.append("statefulMode 要是 true。不開的話排隊的資料在 pod 裡，vmagent 一重啟就沒了，不報錯")
    limit = size((spec.get("remoteWriteSettings") or {}).get("maxDiskUsagePerURL"))
    pvc = size((((((spec.get("statefulStorage") or {}).get("volumeClaimTemplate") or {}).get("spec") or {})
                 .get("resources") or {}).get("requests") or {}).get("storage"))
    if not limit:
        e.append("remoteWriteSettings.maxDiskUsagePerURL 要設暫存上限。沒設的話一個目的地停太久會把磁碟寫滿")
    if not pvc:
        e.append("statefulStorage 要寫 PVC 的大小（volumeClaimTemplate.spec.resources.requests.storage）")
    if limit and pvc and pvc < limit * max(len(rws), 1):
        e.append("PVC 比 %d 個目的地的暫存上限加起來還小，一個目的地的佇列會把磁碟吃滿，另一個也寫不進去"
                 % len(rws))
    return sites, e


def site_file_errors(path, doc, sites):
    """platform/victoriametrics/site-<廠區>.yaml：只能有 vmagent.spec.externalLabels.site，值要跟檔名一樣。"""
    name = os.path.basename(path)[len("site-"):-len(".yaml")]
    labels = (((doc or {}).get("vmagent") or {}).get("spec") or {}).get("externalLabels") or {}
    if doc != {"vmagent": {"spec": {"externalLabels": {"site": labels.get("site")}}}}:
        return ["只能有 vmagent.spec.externalLabels.site 這一項。其他設定放這裡，兩廠的 vmagent 會不一樣"]
    e = []
    if labels["site"] not in sites:
        e.append("site「%s」不是允許的值（允許：%s，從 %s 的 remoteWrite 推出）"
                 % (labels["site"], ", ".join(sites), VMSTACK))
    if labels["site"] != name:
        e.append("檔名是 site-%s.yaml，裡面寫的卻是 %s。照檔名選檔的人會貼錯廠區" % (name, labels["site"]))
    return e


TABLE, ROUTING_ERRORS = routing(load(VMSTACK)["vmagent"]["spec"])
SITES, WRITE_ERRORS = writes(load(VMSTACK)["vmagent"]["spec"])
ALLOWED = set(TABLE)


def mode(doc, ns_template):
    """namespace label 由誰貼：argocd、manual（chart 自己的 namespace.yaml），判不出來回 None。"""
    if doc.get("argocd") is not None:
        return None if ns_template else "argocd"
    return "manual" if ns_template else None


def declared_ns(doc, ns_template):
    """這個 app 宣告的 namespace。"""
    if mode(doc, ns_template) == "manual":
        return doc.get("namespace")
    a = doc.get("argocd")
    return a.get("namespace") if isinstance(a, dict) else None


def check(app, doc, ns_template=False):
    """一份 values.yaml 的錯誤清單。ns_template：chart 有沒有 templates/namespace.yaml。"""
    e = []
    a = doc.get("argocd")
    if a is not None and ns_template:
        e.append("同時有 argocd 區塊和 templates/namespace.yaml，ArgoCD 和 chart 會搶同一個 namespace。"
                 "走 ArgoCD 就刪掉 namespace.yaml，手動 Helm 就刪掉 argocd 區塊")
    elif a is None and not ns_template:
        e.append("沒有 argocd 區塊也沒有 templates/namespace.yaml，沒有人貼 namespace label。"
                 "走 ArgoCD 就補 argocd 區塊，手動 Helm 就在 chart 加 templates/namespace.yaml")
    elif a is None:
        if not doc.get("namespace"):
            e.append("namespace 必填（手動 Helm 的 namespace 由 chart 建立）")
    elif not isinstance(a, dict):
        e.append("argocd 要是一個區塊，ApplicationSet 產生不了 Application")
    else:
        e += ["argocd.%s 必填" % k for k in ARGOCD_FIELDS if k not in a]
        if "autoSync" in a and not isinstance(a["autoSync"], bool):
            e.append("argocd.autoSync 要是 true 或 false")
        if a.get("environment") == "production" and a.get("autoSync") is True:
            e.append("production 不可自動同步，argocd.autoSync 要是 false")
        e += ["argocd.%s 超過 63 字元（label value 上限）" % k
              for k in ARGOCD_FIELDS[:-1] if len(str(a.get(k, ""))) > 63]
    if len(app) > 53:
        e.append("app 名稱超過 53 字元（Helm release name 上限）")

    o = doc.get("observability")
    if not isinstance(o, dict):
        return e + ["缺少 observability 區塊，這個 app 的 metrics 會全部進 fallback"]
    if "group" in o:
        e.append("observability.group 已改名為 tenant（tsid / bsid / icsd / imc / platform），一個 namespace 只能有一個")
    extra = sorted(set(o) - set(OBS_FIELDS) - {"group"})
    if extra:
        e.append("observability 有不認得的欄位：%s。打錯字的欄位會被默默忽略" % ", ".join(extra))
    e += ["observability.%s 必填" % k for k in ("dept", "tenant") if not o.get(k)]
    for k in OBS_FIELDS:
        v = o.get(k)
        if v is not None and not (isinstance(v, str) and LABEL_VALUE.match(v)):
            e.append("observability.%s「%s」不是合法的 label 值" % (k, v))
    t = o.get("tenant")
    if isinstance(t, str) and t and t not in ALLOWED:
        e.append("tenant「%s」不是允許的值（允許：%s）" % (t, ", ".join(sorted(ALLOWED))))
    return e


OVERLAY_KEYS = ("scrapeClasses", "inlineRelabelConfig", "statefulMode", "statefulStorage",
                "remoteWriteSettings", "remoteWrite")


def overlay_errors(overlay, vmstack):
    """overlay 只能有 vmagent.spec 的分流與雙寫那幾段，而且要跟 vmstack-values.yaml 一樣。"""
    e = []
    overlay = overlay or {}
    agent = overlay.get("vmagent") or {}
    spec = agent.get("spec") or {}
    extra = ([k for k in overlay if k != "vmagent"]
             + ["vmagent.%s" % k for k in agent if k != "spec"]
             + ["vmagent.spec.%s" % k for k in spec if k not in OVERLAY_KEYS])
    if extra:
        e.append("只能有 vmagent.spec 的 %s，多了 %s（會蓋掉該環境原本的設定）"
                 % (" / ".join(OVERLAY_KEYS), ", ".join(extra)))
    want = vmstack["vmagent"]["spec"]
    for k in OVERLAY_KEYS[:-1]:
        if spec.get(k) != want[k]:
            e.append("%s 跟 %s 不一樣" % (k, VMSTACK))
    # url 可以依環境不同；token、其他設定都要一樣
    no_url = lambda rws: [{k: v for k, v in rw.items() if k != "url"} for rw in rws or []]
    if no_url(spec.get("remoteWrite")) != no_url(want["remoteWrite"]):
        e.append("remoteWrite 跟 %s 不一樣（只有 url 可以不同）" % VMSTACK)
    return e


def render_labels(values, tmp):
    """用 applicationset.yaml 的 templatePatch 渲染，回傳 namespace 會拿到的 label。"""
    patch = load(APPSET)["spec"]["templatePatch"]
    chart = os.path.join(tmp, "render")
    os.makedirs(os.path.join(chart, "templates"), exist_ok=True)
    io.open(os.path.join(chart, "Chart.yaml"), "w").write("apiVersion: v2\nname: render\nversion: 0.0.0\n")
    io.open(os.path.join(chart, "patch.tmpl"), "w", encoding="utf-8").write(patch)
    # ArgoCD 給 template 的參數是 map[string]interface{}，helm 的 .Values 不是，先轉一手
    io.open(os.path.join(chart, "templates", "out.yaml"), "w").write(
        '{{ tpl (.Files.Get "patch.tmpl") (.Values | toYaml | fromYaml) }}\n')
    vf = os.path.join(tmp, "values.yaml")
    with io.open(vf, "w", encoding="utf-8") as f:
        yaml.safe_dump(values, f, allow_unicode=True)
    out = subprocess.run(["helm", "template", "r", chart, "-f", vf],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    doc = next(d for d in yaml.safe_load_all(out.stdout) if d)
    return doc["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"] or {}


def render_chart_labels(chart, tmp, obs=None):
    """手動 Helm 的 app：渲染 chart，回傳它自己建的 Namespace 的 label。obs 用來換掉 observability。"""
    cmd = ["helm", "template", "ci", chart]
    if obs is not None:
        vf = os.path.join(tmp, "obs.yaml")
        with io.open(vf, "w", encoding="utf-8") as f:
            # 沒給的欄位寫成 null，helm 才會把 chart 預設的值拿掉
            yaml.safe_dump({"observability": {k: obs.get(k) for k in OBS_FIELDS}}, f, allow_unicode=True)
        cmd += ["-f", vf]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise RuntimeError(out.stderr.strip().splitlines()[-1])
    nss = [d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "Namespace"]
    if len(nss) != 1:
        raise RuntimeError("chart 要剛好渲染出一個 Namespace，實際 %d 個" % len(nss))
    return nss[0]["metadata"].get("labels") or {}


def expected_labels(values):
    o = values.get("observability") or {}
    return {k: o[k] for k in OBS_FIELDS if o.get(k)}


def report(ok, title, detail=()):
    global failed
    print("  %s  %s" % ("OK  " if ok else "FAIL", title))
    for d in detail:
        print("        %s" % d)
    failed += not ok


tmp = tempfile.mkdtemp()
apps = sorted(glob.glob("app/*/k8s/values.yaml"))
# Windows 上 glob 回傳的是反斜線路徑
docs = {p.replace("\\", "/").split("/")[1]: load(p) or {} for p in apps}

print("=== 1. app/*/k8s/values.yaml ===")
ns_tpl = {app: os.path.exists("app/%s/k8s/templates/namespace.yaml" % app) for app in docs}
modes = {app: mode(doc, ns_tpl[app]) for app, doc in docs.items()}
owners = {}
for app, doc in docs.items():
    ns = declared_ns(doc, ns_tpl[app])
    if ns:
        owners.setdefault(ns, []).append(app)
for app, doc in docs.items():
    errs = check(app, doc, ns_tpl[app])
    ns = declared_ns(doc, ns_tpl[app])
    if ns and len(owners[ns]) > 1:
        errs.append("namespace %s 也被 %s 宣告，兩個 Application 會輪流覆蓋對方的 label"
                    % (ns, ", ".join(a for a in owners[ns] if a != app)))
    chart = "app/%s/k8s" % app
    r = subprocess.run(["helm", "template", "ci", chart], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        errs.append("chart 渲染失敗：%s" % r.stderr.strip().splitlines()[-1])
    report(not errs, app + ("（手動 Helm）" if modes[app] == "manual" else ""), errs)

print("\n=== 2. platform/ 的分流設定 ===")
print("  對照（從 %s 讀出）：%s"
      % (VMSTACK, " ".join("%s=%s" % kv for kv in sorted(TABLE.items(), key=lambda kv: kv[1]))))
vmstack = load(VMSTACK)
overlay = load(OVERLAY)
report(not ROUTING_ERRORS, "%s 的租戶分流" % VMSTACK, ROUTING_ERRORS)
report(not WRITE_ERRORS, "%s 的雙寫與暫存（目的地：%s）" % (VMSTACK, "、".join(SITES)), WRITE_ERRORS)
placeholder = [rw["url"] for rw in vmstack["vmagent"]["spec"]["remoteWrite"] if ".invalid/" in str(rw.get("url"))]
if placeholder:
    print("  注意  remoteWrite 還有 %d 個網址是佔位（.invalid），確認前不能上線" % len(placeholder))
site_files = {p.replace("\\", "/"): load(p) for p in sorted(glob.glob("platform/victoriametrics/site-*.yaml"))}
for path, doc in site_files.items():
    errs = site_file_errors(path, doc, SITES)
    report(not errs, path, errs)
missing = [x for x in SITES if "platform/victoriametrics/site-%s.yaml" % x not in site_files]
report(not missing, "每個廠區都有 platform/victoriametrics/site-<廠區>.yaml", ["缺 %s" % ", ".join(missing)] if missing else [])
errs = overlay_errors(overlay, vmstack)
report(not errs, "%s 跟 %s 一致（url 除外）" % (OVERLAY, VMSTACK), errs)


def broken(fn):
    spec = copy.deepcopy(vmstack["vmagent"]["spec"])
    fn(spec)
    return spec


def swap(rules, i, j):
    rules[i], rules[j] = rules[j], rules[i]


# 自我檢查：改壞的分流一定要被抓到。每一種都是「不會報錯、資料安靜地進錯租戶」
bad_routing = [
    ("拿掉清掉 vm_account_id 那條", lambda s: s["inlineRelabelConfig"].pop(0)),
    ("AccountID 重複", lambda s: s["inlineRelabelConfig"][1].update(replacement="1004")),
    ("兜底不在最後", lambda s: swap(s["inlineRelabelConfig"], 1, -1)),
    ("兜底先改 vm_account_id 才改 tenant", lambda s: swap(s["inlineRelabelConfig"], -1, -2)),
    ("兜底的 regex 寫成空字串", lambda s: s["inlineRelabelConfig"][-1].update(regex="")),
    ("對照的 regex 寫成 .*", lambda s: s["inlineRelabelConfig"][2].update(regex=".*")),
    ("抓取時沒把沒貼的補成兜底", lambda s: s["scrapeClasses"][0]["relabelConfigs"].pop(2)),
]
for name, fn in bad_routing:
    errs = routing(broken(fn))[1]
    report(bool(errs), "自我檢查：%-24s → %s" % (name, errs[0] if errs else "沒有被擋下"))

# 自我檢查：改壞的雙寫與暫存一定要被抓到
storage = lambda s: s["statefulStorage"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]
bad_writes = [
    ("remoteWrite 只剩一條", lambda s: s["remoteWrite"].pop()),
    ("remoteWrite 三條", lambda s: s["remoteWrite"].append(dict(s["remoteWrite"][0]))),
    ("兩條的網址一樣", lambda s: s["remoteWrite"][1].update(url=s["remoteWrite"][0]["url"])),
    ("兩條帶同一把 token", lambda s: s["remoteWrite"][1].update(bearerTokenSecret=s["remoteWrite"][0]["bearerTokenSecret"])),
    ("url 不是 multitenant 端點", lambda s: s["remoteWrite"][0].update(url="https://x/prometheus/api/v1/write")),
    ("沒開 statefulMode", lambda s: s.pop("statefulMode")),
    ("沒設暫存上限", lambda s: s.pop("remoteWriteSettings")),
    ("PVC 比兩個上限的總和小", lambda s: storage(s).update(storage="30Gi")),
]
for name, fn in bad_writes:
    errs = writes(broken(fn))[1]
    report(bool(errs), "自我檢查：%-24s → %s" % (name, errs[0] if errs else "沒有被擋下"))

# 自我檢查：廠區檔寫錯一定要被抓到，寫對要放行
site_doc = lambda **labels: {"vmagent": {"spec": {"externalLabels": labels}}}
bad_sites = [
    ("site 檔多帶別的設定", True, "platform/victoriametrics/site-ctsp.yaml", site_doc(site="ctsp", cluster="x")),
    ("site 的值不在清單裡", True, "platform/victoriametrics/site-taipei.yaml", site_doc(site="taipei")),
    ("site 檔名跟值不一致", True, "platform/victoriametrics/site-ctsp.yaml", site_doc(site="erlin")),
    ("site 檔寫對", False, "platform/victoriametrics/site-erlin.yaml", site_doc(site="erlin")),
]
for name, should_fail, path, doc in bad_sites:
    errs = site_file_errors(path, doc, SITES)
    report(bool(errs) == should_fail, "自我檢查：%-24s → %s" % (name, errs[0] if errs else "放行"))


def mutate(fn):
    doc = copy.deepcopy(overlay)
    fn(doc["vmagent"]["spec"])
    return doc


# 自我檢查：改壞的 overlay 一定要被抓到，只改 url 一定要放行
mutants = [
    ("overlay 的對照少一個租戶", True, lambda s: s["inlineRelabelConfig"].pop(1)),
    ("overlay 帶成別的 Secret key", True, lambda s: s["remoteWrite"][0]["bearerTokenSecret"].update(key="it")),
    ("overlay 多帶 externalLabels: {}", True, lambda s: s.update(externalLabels={})),
    ("overlay 的 scrapeClass 少了 tenant", True, lambda s: s["scrapeClasses"][0]["relabelConfigs"].pop(1)),
    ("overlay 沒帶 statefulMode", True, lambda s: s.pop("statefulMode")),
    ("overlay 的暫存上限不一樣", True, lambda s: s["remoteWriteSettings"].update(maxDiskUsagePerURL="5GiB")),
    ("overlay 只把 url 換成別的 vmauth", False,
     lambda s: s["remoteWrite"][0].update(url="http://vmauth.example.svc:8427/insert/multitenant/prometheus/api/v1/write")),
]
for name, should_fail, fn in mutants:
    errs = overlay_errors(mutate(fn), vmstack)
    ok = bool(errs) == should_fail
    report(ok, "自我檢查：%-24s → %s" % (name, errs[0] if errs else "放行"))

print("\n=== 3. values.yaml → namespace label ===")
for app, doc in docs.items():
    if modes[app] is None:
        print("  --    %-14s 判不出誰貼 label（見第 1 段），不渲染" % app)
        continue
    want = expected_labels(doc)
    if modes[app] == "manual":
        # 手動 Helm：label 是 chart 自己的 namespace.yaml 貼的，不經 ApplicationSet
        try:
            got = render_chart_labels("app/%s/k8s" % app, tmp)
        except RuntimeError as err:
            report(False, app, [str(err)])
            continue
    else:
        got = render_labels(doc, tmp)
    report(got == want, "%-14s %s" % (app, " ".join("%s=%s" % kv for kv in sorted(got.items()))),
           [] if got == want else ["預期 %s" % want])

print("\n=== 4. 自我檢查：這些寫法一定要被擋下 ===")
base = {"argocd": {"namespace": "x-prod", "project": "p", "environment": "development",
                   "owner": "o", "criticality": "low", "autoSync": True},
        "observability": {"dept": "D3120", "tenant": "icsd", "system": "mes"}}
bad = [
    ("tenant=imcc", ("observability", "tenant", "imcc")),
    ("tenant=it（舊的值）", ("observability", "tenant", "it")),
    ("tenant=it.imc（不再支援）", ("observability", "tenant", "it.imc")),
    ("沒有 tenant", ("observability", "tenant", None)),
    ("還在寫 group", ("observability", "group", "imc")),
    ("沒有 dept", ("observability", "dept", None)),
    ("欄位打錯 tenent", ("observability", "tenent", "imc")),
    ("system 有空白", ("observability", "system", "MES System")),
    ("production 自動同步", ("argocd", "environment", "production")),
    ("沒有 observability", ("observability", None, None)),
]
for name, (block, key, value) in bad:
    doc = copy.deepcopy(base)
    if key is None:
        del doc[block]
    elif value is None:
        del doc[block][key]
    else:
        doc[block][key] = value
    errs = check("x", doc)
    report(bool(errs), "%-26s → %s" % (name, errs[0] if errs else "沒有被擋下"))

# 誰貼 namespace label：argocd 區塊和 templates/namespace.yaml 要剛好有一個
manual = {"namespace": "x-dev", "observability": base["observability"]}
who = [
    ("argocd 和 namespace.yaml 都有", True, base, True),
    ("argocd 和 namespace.yaml 都沒有", True, manual, False),
    ("手動 Helm 沒寫 namespace", True, {"observability": base["observability"]}, True),
    ("手動 Helm，tenant=imcc", True, dict(manual, observability=dict(base["observability"], tenant="imcc")), True),
    ("手動 Helm，寫對", False, manual, True),
]
for name, should_fail, doc, ns_template in who:
    errs = check("x", doc, ns_template)
    report(bool(errs) == should_fail,
           "%-26s → %s" % (name, errs[0] if errs else ("放行" if not should_fail else "沒有被擋下")))

print("\n=== 5. 自我檢查：渲染一定要照原值 ===")
cases = [
    ("tenant=icsd", {"dept": "D3120", "tenant": "icsd", "system": "mes"}),
    ("tenant=imc", {"dept": "D1110", "tenant": "imc", "system": "wip"}),
    ("tenant=platform", {"dept": "PLATFORM", "tenant": "platform", "system": "sim"}),
    ("沒寫 system", {"dept": "D3120", "tenant": "icsd"}),
]
for name, obs in cases:
    doc = dict(copy.deepcopy(base), observability=obs)
    got = render_labels(doc, tmp)
    empty = [k for k, v in got.items() if v == ""]
    ok = got == obs and not empty
    report(ok, "%-14s %s" % (name, " ".join("%s=%s" % kv for kv in sorted(got.items()))),
           [] if ok else ["預期 %s" % obs])

# 手動 Helm 那條路也要照原值：拿第一個手動 app 的 chart，換 observability 渲染
manual_apps = [app for app in docs if modes[app] == "manual"]
if not manual_apps:
    print("  --    沒有手動 Helm 的 app，略過 chart 的渲染檢查")
for name, obs in cases if manual_apps else []:
    try:
        got = render_chart_labels("app/%s/k8s" % manual_apps[0], tmp, obs)
    except RuntimeError as err:
        report(False, "手動 Helm %-14s" % name, [str(err)])
        continue
    ok = got == obs and not [k for k, v in got.items() if v == ""]
    report(ok, "手動 Helm %-14s %s" % (name, " ".join("%s=%s" % kv for kv in sorted(got.items()))),
           [] if ok else ["預期 %s" % obs])

print("\n" + ("PASS" if not failed else "FAIL（%d 項）" % failed))
sys.exit(1 if failed else 0)
PY
