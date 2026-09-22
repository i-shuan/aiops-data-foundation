"""檢查 app.yaml 的 observability 區塊。不通過就擋下 merge。

要併進 devops repo 的 scripts/validate_application.py，或在 CI 裡另外跑一個 job：

    python validate_observability.py applications/*/app.yaml
    python validate_observability.py --ns-labeler-values cluster-addons/ns-labeler/values.yaml applications/*/app.yaml

為什麼要在 CI 擋：這些值最後變成 namespace 的 label，決定資料進哪個 tenant。
寫錯的話不會有任何錯誤——vmagent 照樣收、照樣送，只是送進錯的地方或全掉進兜底。
等到有人發現圖表是空的，那段時間的資料已經救不回來了。

dept → group 的對照表直接從 namespace-metadata.tmpl 讀，跟 ArgoCD 用的是同一份。
ns-labeler 的 values 裡還有一份（給不是 ArgoCD 部署的 namespace 用），
加上 --ns-labeler-values 會比對兩份，不一致就失敗。
"""
import io
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.environ.get("NAMESPACE_METADATA_TMPL", os.path.join(HERE, "namespace-metadata.tmpl"))

# Kubernetes label value：≤ 63 字元，英數開頭結尾，中間可以有 - _ .
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")


def load_groups():
    """從 template 裡的 `$groups := dict ...` 讀出 dept → group。"""
    text = io.open(TEMPLATE, encoding="utf-8").read()
    m = re.search(r"\$groups\s*:=\s*dict(.*?)\}\}", text, re.S)
    if not m:
        sys.exit("在 %s 找不到 $groups := dict ...，CI 無法驗證" % TEMPLATE)
    words = re.findall(r'"([^"]*)"', m.group(1))
    if not words or len(words) % 2:
        sys.exit("%s 的 $groups 不是成對的 key/value" % TEMPLATE)
    return dict(zip(words[0::2], words[1::2]))


def check_ns_labeler(path, groups):
    """dept → group 在 ns-labeler 的 values 裡還有一份，兩份必須一致。回傳錯誤清單。"""
    other = (yaml.safe_load(io.open(path, encoding="utf-8")) or {}).get("registry", {}).get("groups", {}) or {}
    errors = []
    for dept in sorted(set(groups) | set(other)):
        a, b = groups.get(dept), other.get(dept)
        if a != b:
            errors.append("%s：template 是 %s，ns-labeler 是 %s" % (dept, a or "（沒有）", b or "（沒有）"))
    return errors


def load_ns_labeler(path):
    """讀 ns-labeler 的 values，拿出它管的 namespace / app 清單和名稱解析規則。"""
    v = yaml.safe_load(io.open(path, encoding="utf-8")) or {}
    reg = v.get("registry", {}) or {}
    return {
        "namespaces": set((reg.get("namespaces") or {}).keys()),
        "apps": set((reg.get("apps") or {}).keys()),
        "clusters": v.get("clusters") or [],
        "envs": v.get("envs") or [],
    }


def resolve_app(ns, clusters, envs):
    """跟 ns-labeler 腳本同樣的規則：去掉 cluster 前綴和 env 後綴。"""
    for c in clusters:
        if ns.startswith(c + "-"):
            ns = ns[len(c) + 1:]
            break
    for e in envs:
        if ns.endswith("-" + e):
            ns = ns[:-(len(e) + 1)]
            break
    return ns


def check(path, groups, nsl=None):
    """回傳 (錯誤清單, 提醒清單)。"""
    known = set(groups.values()) | {"platform"}
    try:
        doc = yaml.safe_load(io.open(path, encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return ["YAML 解析失敗：%s" % e], []

    obs = doc.get("observability")
    if not isinstance(obs, dict):
        return ["缺少 observability 區塊。沒有它，這個 app 的資料會全部進兜底"], []

    errors, warns = [], []
    dept = obs.get("dept")
    if not dept:
        errors.append("observability.dept 必填")
    elif dept not in groups:
        errors.append("dept「%s」不在對照表裡（已知：%s）。不擋的話 group 會是空的，資料全進兜底"
                      % (dept, ", ".join(sorted(groups))))

    group = obs.get("group")
    if group is not None:
        parts = str(group).split(".")
        unknown = [g for g in parts if g not in known]
        if unknown:
            errors.append("group「%s」裡有不存在的名字：%s（已知：%s）"
                          % (group, ", ".join(unknown), ", ".join(sorted(known))))
        elif "platform" in parts and len(parts) > 1:
            errors.append("platform 是兜底，不能跟別的 group 組合：vmagent 用「不屬於任何 tenant」"
                          "認它，所以 %s 只會進其他 tenant，平台收不到" % group)
    if not obs.get("system"):
        warns.append("沒有 observability.system，log 上會是 unknown")

    for key in ("dept", "group", "system"):
        v = obs.get(key)
        if v is not None and not LABEL_VALUE.match(str(v)):
            errors.append("%s「%s」不是合法的 Kubernetes label 值（≤63 字元，英數開頭結尾，中間只能有 - _ .）"
                          % (key, v))

    unexpected = set(obs) - {"dept", "group", "system"}
    if unexpected:
        errors.append("observability 裡有不認得的欄位：%s（打錯字的欄位會被默默忽略）"
                      % ", ".join(sorted(unexpected)))

    # 同一個 namespace 不能同時由 app.yaml 和 ns-labeler 管：
    # 值不同的話 ArgoCD 的 selfHeal 和 ns-labeler 會互相覆蓋，label 一直翻。
    ns = doc.get("namespace")
    if nsl and ns:
        app = resolve_app(ns, nsl["clusters"], nsl["envs"])
        where = ("registry.namespaces.%s" % ns if ns in nsl["namespaces"]
                 else "registry.apps.%s" % app if app in nsl["apps"] else None)
        if where:
            errors.append("namespace %s 也在 ns-labeler 的對照表裡（%s）。兩邊同時管會互相覆蓋，"
                          "只能留一邊：搬到 app.yaml 的話，要在同一個 PR 把 ns-labeler 那行刪掉" % (ns, where))
    return errors, warns


def main(paths):
    groups = load_groups()
    failed = 0
    total = 0

    nsl = None
    if paths and paths[0] == "--ns-labeler-values":
        nsl_path, paths = paths[1], paths[2:]
        nsl = load_ns_labeler(nsl_path)
        drift = check_ns_labeler(nsl_path, groups)
        print("%-4s  dept → group 對照：%s vs %s" % ("FAIL" if drift else "OK", os.path.basename(TEMPLATE), nsl_path))
        for e in drift:
            print("      ✗ %s" % e)
        if drift:
            print("      兩份不一致時，同一個部門的 app 會依「誰標的」分流到不同 tenant，而且不會報錯")
        failed += bool(drift)
        total += 1
        print()
    for p in paths:
        errors, warns = check(p, groups, nsl)
        status = "FAIL" if errors else "OK"
        print("%-4s  %s" % (status, p))
        for e in errors:
            print("      ✗ %s" % e)
        for w in warns:
            print("      ! %s" % w)
        failed += bool(errors)
        total += 1
    print()
    print("%d 個檢查，%d 個不通過" % (total, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法：python validate_observability.py <app.yaml> [...]")
    sys.exit(main(sys.argv[1:]))
