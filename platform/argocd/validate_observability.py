"""檢查 app.yaml 的 observability 區塊。不通過就擋下 merge。

    python validate_observability.py applications/*/app.yaml

一定要一次把全部 app.yaml 傳進來：「同一個 namespace 不能被兩個 app 宣告」要看全部才驗得出來。

為什麼要在 CI 擋：這些值會被 ArgoCD 貼到 namespace 上，決定資料進哪個 tenant。
寫錯的話執行時不會有任何錯誤——vmagent 照樣收、照樣送，只是送進錯的地方或全掉進 fallback。

dept → group 的對照直接從 namespace-metadata.tmpl 讀，跟 ArgoCD 用的是同一份，沒有第二份。
"""
import io
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.environ.get("NAMESPACE_METADATA_TMPL", os.path.join(HERE, "namespace-metadata.tmpl"))

FIELDS = {"dept", "group", "system"}
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


def check(doc, groups):
    """檢查一份 app.yaml。回傳 (錯誤清單, 提醒清單)。"""
    tenants = set(groups.values())
    obs = doc.get("observability")
    if not isinstance(obs, dict):
        return ["缺少 observability 區塊。沒有它，這個 app 的資料會全部進 fallback"], []

    errors, warns = [], []

    unknown_fields = set(obs) - FIELDS
    if unknown_fields:
        errors.append("observability 裡有不認得的欄位：%s。打錯字的欄位會被默默忽略，看起來有設其實沒有"
                      % ", ".join(sorted(unknown_fields)))

    for key in sorted(FIELDS):
        v = obs.get(key)
        if v is not None and not LABEL_VALUE.match(str(v)):
            errors.append("%s「%s」不是合法的 Kubernetes label 值（≤63 字元，英數開頭結尾，中間只能有 - _ .）"
                          % (key, v))

    dept = obs.get("dept")
    if not dept:
        errors.append("observability.dept 必填。log 是依 dept 分流的，少了它 log 會進 unassigned")
    elif dept not in groups:
        errors.append("dept「%s」不在對照表裡（已知：%s）。不擋的話 group 會是空的，資料全進 fallback"
                      % (dept, ", ".join(sorted(groups))))

    group = obs.get("group")
    if group is not None:
        parts = str(group).split(".")
        unknown = [g for g in parts if g not in tenants]
        if unknown:
            errors.append("group「%s」裡有不存在的 tenant：%s（已知：%s）"
                          % (group, ", ".join(unknown), ", ".join(sorted(tenants))))
        elif "platform" in parts and len(parts) > 1:
            errors.append("platform 是 fallback，不能跟別的 tenant 組合：vmagent 用「不屬於任何 tenant」"
                          "認它，所以 %s 只會進其他 tenant，platform 收不到" % group)
        elif dept in groups and groups[dept] not in parts:
            # group 只能用來「多送」，不能把資料從擁有者那邊拿走
            errors.append("歸屬矛盾：dept %s 屬於 %s，但 group「%s」沒有送給 %s，擁有者會看不到自己的資料。"
                          "group 只能用來多送給別的 tenant，要包含 %s"
                          % (dept, groups[dept], group, groups[dept], groups[dept]))

    if not obs.get("system"):
        warns.append("沒有 observability.system，namespace 上不會有 system label，log 上會是 unknown")
    return errors, warns


def main(paths):
    groups = load_groups()
    docs, errors, warns = {}, {}, {}
    for p in paths:
        try:
            docs[p] = yaml.safe_load(io.open(p, encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            docs[p], errors[p], warns[p] = {}, ["YAML 解析失敗：%s" % e], []
            continue
        errors[p], warns[p] = check(docs[p], groups)

    # 同一個 namespace 被兩個 app.yaml 宣告：兩個 ArgoCD Application 會輪流覆蓋對方的 label
    owners = {}
    for p, d in docs.items():
        if d.get("namespace"):
            owners.setdefault(d["namespace"], []).append(p)
    for ns, ps in owners.items():
        if len(ps) > 1:
            for p in ps:
                others = ", ".join(q for q in ps if q != p)
                errors[p].append("namespace %s 也被 %s 宣告。兩個 Application 會輪流覆蓋對方的 label，"
                                 "一個 namespace 只能屬於一個 app" % (ns, others))

    failed = 0
    for p in paths:
        print("%-4s  %s" % ("FAIL" if errors[p] else "OK", p))
        for e in errors[p]:
            print("      ✗ %s" % e)
        for w in warns[p]:
            print("      ! %s" % w)
        failed += bool(errors[p])
    print("\n%d 個檔案，%d 個不通過" % (len(paths), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法：python validate_observability.py applications/*/app.yaml")
    sys.exit(main(sys.argv[1:]))
