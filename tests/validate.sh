#!/usr/bin/env bash
# 檢查每個 applications/<app>/helm/values.yaml，以及它們會變成什麼 namespace label。
#
#   ./tests/validate.sh
#
# 不需要 cluster，只要 python（PyYAML）和 helm。全部通過 exit 0，任何一項不過 exit 1。
#
#   1. values.yaml 合法：argocd 與 observability 必填欄位、production 不可自動同步、
#      namespace 不能被兩個 app 共用、chart 渲染得出來
#   2. group 只能是允許的值。允許什麼直接從 platform/vmstack-values.yaml 的 remoteWrite 讀，
#      不另外維護清單。platform/vmagent-routing-overlay.yaml 的分流設定要跟它一模一樣
#      （只有 url 可以不同），而且不能帶這兩段以外的設定
#   3. platform/applicationset.yaml 的 templatePatch 渲染出來的 namespace label，
#      要跟 values.yaml 寫的一模一樣；沒寫的欄位不能變成空字串的 label
#   4. 自我檢查：內建的錯誤寫法一定要被擋下，渲染一定要照原值，否則這支腳本本身就是壞的
#
# 渲染用的是 helm。helm 和 ArgoCD 都是 Go template + sprig，但這不是 ArgoCD 本身；
# 真的經過 ArgoCD 的驗證在 tests/routing。
set -euo pipefail
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."

python - <<'PY'
import copy, glob, io, itertools, os, re, subprocess, sys, tempfile
import yaml

APPSET = "platform/applicationset.yaml"
VMSTACK = "platform/vmstack-values.yaml"
OVERLAY = "platform/vmagent-routing-overlay.yaml"
ARGOCD_FIELDS = ["namespace", "project", "environment", "owner", "criticality", "autoSync"]
OBS_FIELDS = ["dept", "group", "system"]
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
failed = 0


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def allowed_groups():
    """從 vmstack-values.yaml 的 remoteWrite 推出允許的 group。"""
    routed, fallback = [], None
    for rw in load(VMSTACK)["vmagent"]["spec"]["remoteWrite"]:
        name = rw["bearerTokenSecret"]["key"]
        if rw["inlineUrlRelabelConfig"][0]["action"] == "drop":
            fallback = name
        else:
            routed.append(name)
    allowed = set(routed) | {fallback}
    # 多個 tenant 用點串起來，順序照 remoteWrite 的順序（it.imc，不是 imc.it）
    for n in range(2, len(routed) + 1):
        allowed.update(".".join(c) for c in itertools.combinations(routed, n))
    return allowed, routed


ALLOWED, ROUTED = allowed_groups()


def check(app, doc):
    """一份 values.yaml 的錯誤清單。"""
    e = []
    a = doc.get("argocd")
    if not isinstance(a, dict):
        e.append("缺少 argocd 區塊，ApplicationSet 產生不了 Application")
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
    extra = sorted(set(o) - set(OBS_FIELDS))
    if extra:
        e.append("observability 有不認得的欄位：%s。打錯字的欄位會被默默忽略" % ", ".join(extra))
    e += ["observability.%s 必填" % k for k in ("dept", "group") if not o.get(k)]
    for k in OBS_FIELDS:
        v = o.get(k)
        if v is not None and not (isinstance(v, str) and LABEL_VALUE.match(v)):
            e.append("observability.%s「%s」不是合法的 label 值" % (k, v))
    g = o.get("group")
    if isinstance(g, str) and g and g not in ALLOWED:
        hint = ""
        parts = g.split(".")
        if set(parts) <= set(ROUTED) and len(parts) == len(set(parts)):
            hint = "，順序要照 vmstack-values.yaml：寫成 %s" % ".".join(t for t in ROUTED if t in parts)
        elif "platform" in parts:
            hint = "。platform 是 fallback，不能跟別的 tenant 組合"
        e.append("group「%s」不是允許的值（允許：%s）%s" % (g, ", ".join(sorted(ALLOWED)), hint))
    return e


def overlay_errors(overlay, vmstack):
    """overlay 只能有 vmagent.spec 的 scrapeClasses 和 remoteWrite，而且要跟 vmstack-values.yaml 一樣。"""
    e = []
    overlay = overlay or {}
    agent = overlay.get("vmagent") or {}
    spec = agent.get("spec") or {}
    extra = ([k for k in overlay if k != "vmagent"]
             + ["vmagent.%s" % k for k in agent if k != "spec"]
             + ["vmagent.spec.%s" % k for k in spec if k not in ("scrapeClasses", "remoteWrite")])
    if extra:
        e.append("只能有 vmagent.spec.scrapeClasses 和 remoteWrite，多了 %s（會蓋掉該環境原本的設定）"
                 % ", ".join(extra))
    want = vmstack["vmagent"]["spec"]
    if spec.get("scrapeClasses") != want["scrapeClasses"]:
        e.append("scrapeClasses 跟 %s 不一樣" % VMSTACK)
    # url 可以依環境不同；過濾條件、token、順序都要一樣
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
apps = sorted(glob.glob("applications/*/helm/values.yaml"))
# Windows 上 glob 回傳的是反斜線路徑
docs = {p.replace("\\", "/").split("/")[1]: load(p) or {} for p in apps}

print("=== 1. applications/*/helm/values.yaml ===")
owners = {}
for app, doc in docs.items():
    ns = (doc.get("argocd") or {}).get("namespace")
    if ns:
        owners.setdefault(ns, []).append(app)
for app, doc in docs.items():
    errs = check(app, doc)
    ns = (doc.get("argocd") or {}).get("namespace")
    if ns and len(owners[ns]) > 1:
        errs.append("namespace %s 也被 %s 宣告，兩個 Application 會輪流覆蓋對方的 label"
                    % (ns, ", ".join(a for a in owners[ns] if a != app)))
    chart = "applications/%s/helm" % app
    r = subprocess.run(["helm", "template", "ci", chart], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        errs.append("chart 渲染失敗：%s" % r.stderr.strip().splitlines()[-1])
    report(not errs, app, errs)

print("\n=== 2. platform/ 的分流設定 ===")
print("  允許的 group（從 %s 推出）：%s" % (VMSTACK, ", ".join(sorted(ALLOWED))))
vmstack = load(VMSTACK)
overlay = load(OVERLAY)
errs = overlay_errors(overlay, vmstack)
report(not errs, "%s 跟 %s 一致（url 除外）" % (OVERLAY, VMSTACK), errs)


def mutate(fn):
    doc = copy.deepcopy(overlay)
    fn(doc["vmagent"]["spec"])
    return doc


# 自我檢查：改壞的 overlay 一定要被抓到，只改 url 一定要放行
mutants = [
    ("imc 的 regex 改成 'imc'", True,
     lambda s: s["remoteWrite"][1]["inlineUrlRelabelConfig"][0].update(regex="imc")),
    ("IT 那條帶成 imc 的 token", True,
     lambda s: s["remoteWrite"][0]["bearerTokenSecret"].update(key="imc")),
    ("少了兜底那條", True, lambda s: s["remoteWrite"].pop()),
    ("多帶 externalLabels: {}", True, lambda s: s.update(externalLabels={})),
    ("scrapeClass 少了 group", True, lambda s: s["scrapeClasses"][0]["relabelConfigs"].pop(1)),
    ("只把 url 換成別的 vmauth", False,
     lambda s: [rw.update(url="http://vmauth.example.svc:8427/prometheus/api/v1/write") for rw in s["remoteWrite"]]),
]
for name, should_fail, fn in mutants:
    errs = overlay_errors(mutate(fn), vmstack)
    ok = bool(errs) == should_fail
    report(ok, "自我檢查：%-24s → %s" % (name, errs[0] if errs else "放行"))

print("\n=== 3. values.yaml → namespace label ===")
for app, doc in docs.items():
    want, got = expected_labels(doc), render_labels(doc, tmp)
    report(got == want, "%-14s %s" % (app, " ".join("%s=%s" % kv for kv in sorted(got.items()))),
           [] if got == want else ["預期 %s" % want])

print("\n=== 4. 自我檢查：這些寫法一定要被擋下 ===")
base = {"argocd": {"namespace": "x-prod", "project": "p", "environment": "development",
                   "owner": "o", "criticality": "low", "autoSync": True},
        "observability": {"dept": "D3120", "group": "it", "system": "mes"}}
bad = [
    ("group=imcc", ("observability", "group", "imcc")),
    ("group=it.imcc", ("observability", "group", "it.imcc")),
    ("group=it.platform", ("observability", "group", "it.platform")),
    ("group=imc.it（順序反了）", ("observability", "group", "imc.it")),
    ("沒有 group", ("observability", "group", None)),
    ("沒有 dept", ("observability", "dept", None)),
    ("欄位打錯 grop", ("observability", "grop", "it")),
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

print("\n=== 5. 自我檢查：渲染一定要照原值 ===")
cases = [
    ("group=it", {"dept": "D3120", "group": "it", "system": "mes"}),
    ("group=imc", {"dept": "D1110", "group": "imc", "system": "wip"}),
    ("group=it.imc", {"dept": "D3120", "group": "it.imc", "system": "mes"}),
    ("沒寫 system", {"dept": "D3120", "group": "it"}),
]
for name, obs in cases:
    doc = dict(copy.deepcopy(base), observability=obs)
    got = render_labels(doc, tmp)
    empty = [k for k, v in got.items() if v == ""]
    ok = got == obs and not empty
    report(ok, "%-14s %s" % (name, " ".join("%s=%s" % kv for kv in sorted(got.items()))),
           [] if ok else ["預期 %s" % obs])

print("\n" + ("PASS" if not failed else "FAIL（%d 項）" % failed))
sys.exit(1 if failed else 0)
PY
