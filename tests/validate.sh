#!/usr/bin/env bash
# 檢查每個 app/<app>/k8s/values.yaml，以及它們會變成什麼 namespace label。
#
#   ./tests/validate.sh
#
# 不需要 cluster，只要 python（PyYAML）和 helm。全部通過 exit 0，任何一項不過 exit 1。
#
#   1. values.yaml 合法：argocd 與 observability 必填欄位、production 不可自動同步、
#      namespace 不能被兩個 app 共用、chart 渲染得出來
#   2. tenant 只能是允許的值，一個 namespace 一個。清單只有一份，從
#      platform/otel/common.yaml 的 ALLOWED_TENANTS 讀
#   3. platform/argocd/applicationset.yaml 的 templatePatch 渲染出來的 namespace label，
#      要跟 values.yaml 寫的一模一樣；沒寫的欄位不能變成空字串的 label
#
# app 有兩種，看 chart 自己的檔案分辨，不另外維護清單：
#   - values.yaml 有 argocd 區塊：走 ArgoCD，照上面檢查
#   - 沒有 argocd 區塊、有 templates/namespace.yaml：手動 Helm，label 是 chart 自己貼的。
#     不查 argocd 欄位，observability 照查，第 3 項改成渲染 chart、取出 Namespace 的 label 來比
#   - 兩個都有或都沒有：擋下
#   4. 自我檢查：內建的錯誤寫法一定要被擋下，渲染一定要照原值，否則這支腳本本身就是壞的
#   5. platform/otel/ 的收集設定：改錯了不會報錯、資料只是安靜地掉或進錯部門的那幾個地方
#      （重試、佇列、擋冒充、對 pod、server 的標籤、密碼）
#   6. 自我檢查：第 5 項的每一條，改壞了一定要被擋下
#
# 渲染用的是 helm。helm 和 ArgoCD 都是 Go template + sprig，但這不是 ArgoCD 本身；
# 目前沒有真的經過 ArgoCD 的驗證。
set -euo pipefail
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."

python - <<'PY'
import copy, glob, io, os, re, subprocess, sys, tempfile
import yaml

APPSET = "platform/argocd/applicationset.yaml"
ARGOCD_FIELDS = ["namespace", "project", "environment", "owner", "criticality", "autoSync"]
OBS_FIELDS = ["dept", "tenant", "system"]
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
failed = 0


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


OTEL = "platform/otel"
ROLES = {"cluster": "cluster-collector.yaml", "node": "node-collector.yaml"}
# ALLOWED_TENANTS 的固定寫法：小寫英數，用 | 隔開，沒有空白。寫法變了就擋下，不猜
TENANT_LIST = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\|[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
SECRET_LINE = re.compile(r"(?i)(password|passwd|token|api_key)\s*:\s*[\"']?([^\s\"'#]+)")
URL_CRED = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


def load_otel():
    """platform/otel/ 讀成一包：共用設定、兩份收集程式的設定與 workload、每個 cluster 的設定、原始文字。"""
    def docs(path):
        with io.open(path, encoding="utf-8") as f:
            return [d for d in yaml.safe_load_all(f) if d]

    def cm(ds, name):
        return next(d["data"] for d in ds if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == name)

    b = {"common": cm(docs(OTEL + "/common.yaml"), "otel-common"), "sites": {}, "texts": {}}
    for role, name in ROLES.items():
        ds = docs("%s/%s" % (OTEL, name))
        b[role] = {"cfg": yaml.safe_load(cm(ds, "otel-%s-collector" % role)["config.yaml"]),
                   "workload": next(d for d in ds if d.get("kind") in ("StatefulSet", "DaemonSet", "Deployment"))}
    for path in sorted(glob.glob(OTEL + "/site-*.yaml")):
        b["sites"][path.replace("\\", "/")] = cm(docs(path), "otel-site")
    for path in sorted(glob.glob(OTEL + "/*.yaml")):
        b["texts"][path.replace("\\", "/")] = io.open(path, encoding="utf-8").read()
    return b


def squash(s):
    return re.sub(r"\s+", "", str(s))


def statements(cfg, proc, ctx):
    out = []
    for blk in ((cfg.get("processors") or {}).get(proc) or {}).get("metric_statements") or []:
        if isinstance(blk, dict) and blk.get("context") == ctx:
            out += [squash(x) for x in blk.get("statements") or []]
    return out


def check_otel(b):
    """收集設定的錯誤清單。只查「改錯了不會報錯」的地方。"""
    e = []
    c = b["common"]
    allowed = str(c.get("ALLOWED_TENANTS", ""))
    if not TENANT_LIST.match(allowed):
        e.append("common：ALLOWED_TENANTS「%s」不是固定的寫法（小寫英數，用 | 隔開，不能有空白）" % allowed)
    for k in ("DEFAULT_TENANT", "SERVER_TENANT"):
        if c.get(k) not in allowed.split("|"):
            e.append("common：%s「%s」不在 ALLOWED_TENANTS 裡" % (k, c.get(k)))

    T = 'set(resource.attributes["tenant"],'
    want_tenant = [
        T + '"${env:DEFAULT_TENANT}")',
        T + '"${env:SERVER_TENANT}")whereresource.attributes["k8s.namespace.name"]==nil'
            'andresource.attributes["k8s.node.name"]!=nil',
        T + 'resource.attributes["ns.tenant"])whereresource.attributes["ns.tenant"]!=nil'
            'andIsMatch(resource.attributes["ns.tenant"],"^(${env:ALLOWED_TENANTS})$")',
    ]
    want_ds = 'set(resource.attributes["data_stream.namespace"],resource.attributes["tenant"])'

    for role in ROLES:
        cfg, wl = b[role]["cfg"], b[role]["workload"]
        add = lambda m, role=role: e.append("%s：%s" % (role, m))
        procs_def = cfg.get("processors") or {}

        # 重試與佇列
        exps = {k: v or {} for k, v in (cfg.get("exporters") or {}).items() if k.split("/")[0] == "elasticsearch"}
        if not exps:
            add("沒有 elasticsearch 的 exporter")
        for name, x in exps.items():
            r = x.get("retry") or {}
            n = r.get("max_retries")
            if r.get("enabled") is not True or not isinstance(n, int) or n < 1000:
                add("%s 的 retry 要開、max_retries 至少 1000（現在 %s）。寫 0 是不重試，"
                    "Elasticsearch 一連不上資料就直接丟掉，不報錯" % (name, n))
            q = x.get("sending_queue") or {}
            if q.get("enabled") is not True or q.get("storage") != "file_storage":
                add("%s 的 sending_queue 要開、storage 要是 file_storage，不然佇列只在記憶體裡" % name)
            if not str(x.get("password", "")).startswith("${env:"):
                add("%s 的 password 要寫成 ${env:...}，值從 Secret 來" % name)
        if "file_storage" not in ((cfg.get("service") or {}).get("extensions") or []):
            add("service.extensions 沒有 file_storage，佇列不會寫到磁碟")

        # 佇列的目錄有沒有真的掛在不會跟著 pod 消失的地方
        qdir = str(((cfg.get("extensions") or {}).get("file_storage") or {}).get("directory", ""))
        spec = wl["spec"]["template"]["spec"]
        mounts = [m for ct in spec["containers"] for m in ct.get("volumeMounts") or []
                  if qdir.startswith(m["mountPath"].rstrip("/") + "/")]
        durable = {t["metadata"]["name"] for t in wl["spec"].get("volumeClaimTemplates") or []}
        durable |= {v["name"] for v in spec.get("volumes") or [] if "hostPath" in v or "persistentVolumeClaim" in v}
        if not mounts or not all(m["name"] in durable for m in mounts):
            add("佇列的目錄 %s 沒有掛在 PVC 或節點的磁碟上。收集程式一重啟，排隊中的資料就沒了" % qdir)

        # 帳密只能從 Secret 來
        for ct in spec["containers"]:
            for ev in ct.get("env") or []:
                if re.search("PASSWORD|TOKEN|SECRET|USER", ev["name"]) and \
                        "secretKeyRef" not in (ev.get("valueFrom") or {}):
                    add("環境變數 %s 要用 secretKeyRef，不能直接寫值" % ev["name"])

        # 擋冒充：程式自己帶的身分欄位先丟掉
        pre = statements(cfg, "transform/pre", "resource")
        for k in ("tenant", "site", "ns.tenant", "data_stream.namespace"):
            if 'delete_key(resource.attributes,"%s")' % k not in pre:
                add("transform/pre 沒有先丟掉程式自己帶的 %s" % k)
        dp = statements(cfg, "transform/tenant", "datapoint")
        for k in ("tenant", "site", "data_stream.namespace"):
            if 'delete_key(datapoint.attributes,"%s")' % k not in dp:
                add("transform/tenant 沒有丟掉樣本自己帶的 %s，程式可以自稱是別的部門" % k)

        # 對 pod、讀門牌
        if not any(x.startswith('set(resource.attributes["k8s.pod.ip"],resource.attributes["server.address"])')
                   for x in pre):
            add("transform/pre 少了把抓取對象的位址放進 k8s.pod.ip 的那一行，讀門牌時對不到 pod")
        ka = procs_def.get("k8s_attributes") or {}
        srcs = {x.get("name") for a in ka.get("pod_association") or [] for x in a.get("sources") or []}
        if not {"k8s.pod.ip", "k8s.pod.uid"} <= srcs:
            add("k8s_attributes 的 pod_association 要有 k8s.pod.uid 與 k8s.pod.ip")
        if not any(x.get("tag_name") == "ns.tenant" and x.get("key") == "tenant" and x.get("from") == "namespace"
                   for x in (ka.get("extract") or {}).get("labels") or []):
            add("k8s_attributes 沒有把 namespace 的 tenant 門牌讀成 ns.tenant")

        # 部門怎麼定：固定的三行，順序固定，最後才決定資料流
        res = statements(cfg, "transform/tenant", "resource")
        if [x for x in res if x.startswith(T)] != want_tenant:
            add("transform/tenant 決定 tenant 的三行被改過或順序不對"
                "（預設 → server 標 SERVER_TENANT → 門牌在清單裡才照門牌）")
        if not res or res[-1] != want_ds:
            add("transform/tenant 的最後一行要是 data_stream.namespace = tenant，不然資料流跟 tenant 對不上")

        # 每條 pipeline 的順序
        for pname, p in ((cfg.get("service") or {}).get("pipelines") or {}).items():
            procs = p.get("processors") or []
            recvs = [r.split("/")[0] for r in p.get("receivers") or []]
            if not procs or procs[-1] != "transform/tenant":
                add("pipeline %s 的最後一個 processor 要是 transform/tenant" % pname)
            if "transform/pre" not in procs:
                add("pipeline %s 沒有 transform/pre" % pname)
            if set(recvs) & {"prometheus", "kubelet_stats", "k8s_cluster"}:
                if "k8s_attributes" not in procs:
                    add("pipeline %s 沒有 k8s_attributes，讀不到門牌，資料全部變成預設的部門" % pname)
                elif "transform/pre" in procs and procs.index("transform/pre") > procs.index("k8s_attributes"):
                    add("pipeline %s 的 transform/pre 要在 k8s_attributes 前面" % pname)
            if "prometheus" in recvs and "cumulative_to_delta" not in procs:
                add("pipeline %s 沒有 cumulative_to_delta，處理時間的分布圖會被 Elasticsearch 的寫入程式丟掉" % pname)
            if "host_metrics" in recvs and "resource/node" not in procs:
                add("pipeline %s 沒有 resource/node，server 的資料不會被標成 SERVER_TENANT" % pname)
            if not [x for x in p.get("exporters") or [] if x in exps]:
                add("pipeline %s 沒有送到 elasticsearch" % pname)

    # 兩份的標籤規則與寫入設定要一樣
    for part, get in (("transform/pre", lambda r: (b[r]["cfg"].get("processors") or {}).get("transform/pre")),
                      ("transform/tenant", lambda r: (b[r]["cfg"].get("processors") or {}).get("transform/tenant")),
                      ("exporters.elasticsearch", lambda r: (b[r]["cfg"].get("exporters") or {}).get("elasticsearch"))):
        if get("cluster") != get("node"):
            e.append("兩份收集設定的 %s 不一樣。同一種資料在兩份裡會被標成不同的部門或用不同的方式重試" % part)

    # 每個 cluster 自己的設定
    if not b["sites"]:
        e.append("沒有 site-*.yaml，收集程式不知道自己在哪個廠")
    for path, st in b["sites"].items():
        e += ["%s：%s 必填" % (path, k) for k in ("SITE", "CLUSTER", "ES_ENDPOINT") if not st.get(k)]

    # 密碼不進檔案
    for path, text in b["texts"].items():
        if re.search(r"(?m)^kind:\s*Secret\s*$", text):
            e.append("%s：不能放 Secret，帳密不進 repo" % path)
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            m = SECRET_LINE.search(line)
            if (m and not m.group(2).startswith("${env:")) or URL_CRED.search(line):
                e.append("%s:%d 看起來把密碼直接寫在檔案裡" % (path, i))
    return e


try:
    otel = load_otel()
except Exception as err:  # 讀不出來就沒有允許的清單，後面全部沒有意義
    print("FAIL  讀不出 %s/ 的收集設定：%s" % (OTEL, err))
    sys.exit(1)
_allowed = str(otel["common"].get("ALLOWED_TENANTS", ""))
# 允許的 tenant：只有這一份。寫法不對時是空的，第 5 段會報
ALLOWED = set(_allowed.split("|")) if TENANT_LIST.match(_allowed) else set()


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

print("\n=== 2. values.yaml → namespace label ===")
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

print("\n=== 3. 自我檢查：這些寫法一定要被擋下 ===")
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

print("\n=== 4. 自我檢查：渲染一定要照原值 ===")
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

print("\n=== 5. platform/otel/ 收集設定 ===")
print("        允許的 tenant（從 %s/common.yaml 讀）：%s" % (OTEL, ", ".join(sorted(ALLOWED)) or "讀不出來"))
errs = check_otel(otel)
report(not errs, "重試、佇列、擋冒充、對 pod、server 的標籤、密碼", errs)
for path, st in otel["sites"].items():
    for k, v in st.items():
        if "CHANGE-ME" in str(v) or ".invalid" in str(v):
            print("  提醒  %s 的 %s 還是佔位，上線前要換掉" % (path, k))

print("\n=== 6. 自我檢查：收集設定改壞一定要被擋下 ===")


def cfg_of(b, role="cluster"):
    return b[role]["cfg"]


def drop(b, role, proc, ctx, needle):
    for blk in cfg_of(b, role)["processors"][proc]["metric_statements"]:
        if blk["context"] == ctx:
            blk["statements"] = [x for x in blk["statements"] if needle not in x]


def unpipe(b, role, pipeline, proc):
    cfg_of(b, role)["service"]["pipelines"][pipeline]["processors"].remove(proc)


def both(fn):
    """標籤規則兩份要一樣，所以改壞時兩份一起改，才測得到那一條本身。"""
    return lambda b: [fn(b, r) for r in ROLES]


def to_emptydir(b):
    wl = b["cluster"]["workload"]
    del wl["spec"]["volumeClaimTemplates"]
    wl["spec"]["template"]["spec"]["volumes"].append({"name": "queue", "emptyDir": {}})


def ds_first(b, role):
    for blk in cfg_of(b, role)["processors"]["transform/tenant"]["metric_statements"]:
        if blk["context"] == "resource":
            blk["statements"].insert(0, blk["statements"].pop())


def plain_env(b):
    env = b["cluster"]["workload"]["spec"]["template"]["spec"]["containers"][0]["env"]
    env[:] = [dict(name=x["name"], value="x") if x["name"] == "ES_PASSWORD" else x for x in env]


broken = [
    ("重試改成 0", both(lambda b, r: cfg_of(b, r)["exporters"]["elasticsearch"]["retry"].update(max_retries=0))),
    ("重試關掉", both(lambda b, r: cfg_of(b, r)["exporters"]["elasticsearch"]["retry"].update(enabled=False))),
    ("佇列不寫磁碟", both(lambda b, r: cfg_of(b, r)["exporters"]["elasticsearch"]["sending_queue"].pop("storage"))),
    ("佇列的 PVC 換成 emptyDir", to_emptydir),
    ("拿掉「丟掉樣本自己帶的」", both(lambda b, r: drop(b, r, "transform/tenant", "datapoint", "delete_key"))),
    ("沒先丟掉程式帶的 ns.tenant", both(lambda b, r: drop(b, r, "transform/pre", "resource", '"ns.tenant"'))),
    ("拿掉對 pod 的那一行", both(lambda b, r: drop(b, r, "transform/pre", "resource", "server.address"))),
    ("pipeline 拿掉 k8s_attributes", lambda b: unpipe(b, "cluster", "metrics/apps", "k8s_attributes")),
    ("pipeline 拿掉 transform/pre", lambda b: unpipe(b, "cluster", "metrics/apps", "transform/pre")),
    ("門牌不比對清單就照用", both(lambda b, r: drop(b, r, "transform/tenant", "resource", "IsMatch"))),
    ("資料流在 tenant 之前決定", both(ds_first)),
    ("server 沒有標 SERVER_TENANT", both(lambda b, r: drop(b, r, "transform/tenant", "resource", "SERVER_TENANT"))),
    ("server 的資料沒補節點名稱", lambda b: unpipe(b, "node", "metrics/host", "resource/node")),
    ("拿掉 cumulative_to_delta", lambda b: unpipe(b, "cluster", "metrics/apps", "cumulative_to_delta")),
    ("兩份的規則不一樣", lambda b: drop(b, "node", "transform/tenant", "resource", '"dept"')),
    ("密碼寫在設定裡", both(lambda b, r: cfg_of(b, r)["exporters"]["elasticsearch"].update(password="abc123"))),
    ("密碼寫在環境變數的值", plain_env),
    ("檔案裡有 password: 明文", lambda b: b["texts"].update(x="  password: abc123\n")),
    ("位址裡夾帳密", lambda b: b["texts"].update(x='  ES_ENDPOINT: "https://u:p@es:9200"\n')),
    ("清單寫法不對（有空白）", lambda b: b["common"].update(ALLOWED_TENANTS="tsid, bsid")),
    ("預設的部門不在清單裡", lambda b: b["common"].update(DEFAULT_TENANT="nobody")),
    ("SERVER_TENANT 不在清單裡", lambda b: b["common"].update(SERVER_TENANT="infra")),
]
for name, mutate in broken:
    b = copy.deepcopy(otel)
    mutate(b)
    errs = check_otel(b)
    report(bool(errs), "%-26s → %s" % (name, errs[0] if errs else "沒有被擋下"))

print("\n" + ("PASS" if not failed else "FAIL（%d 項）" % failed))
sys.exit(1 if failed else 0)
PY
