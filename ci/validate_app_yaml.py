"""照 devops repo README 寫的規則檢查 applications/<app>/app.yaml。

這是依文件重寫的簡版，用途是確認這個 repo 的 app.yaml 搬過去能過你們的 CI。
**正式以 devops repo 的 scripts/validate_application.py 為準**，兩者有出入時聽那支的。

observability 區塊不在這裡檢查，那是 platform/argocd/validate_observability.py 的事。

用法：python ci/validate_app_yaml.py applications/*/app.yaml
"""
import glob
import io
import os
import sys

import yaml

REQUIRED = ["project", "namespace", "environment", "owner", "criticality", "sync"]
LABEL_FIELDS = ["namespace", "project", "owner", "environment", "criticality"]
BAD_VERSIONS = {"", "HEAD", "LATEST", "*"}


def check(path):
    app_dir = os.path.dirname(path)
    errors = []
    try:
        doc = yaml.safe_load(io.open(path, encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return ["YAML 解析失敗：%s" % e]

    # 1. 不得有 type
    if "type" in doc:
        errors.append("不得包含 type（已棄用）")

    # 2. 必填
    for k in REQUIRED:
        if k not in doc:
            errors.append("缺少必填欄位 %s" % k)

    # 3. production 不可自動同步 / prune
    sync = doc.get("sync") or {}
    if doc.get("environment") == "production":
        for k in ("automated", "prune"):
            if sync.get(k) is True:
                errors.append("production 的 sync.%s 不可為 true" % k)

    helm = doc.get("helm") or {}
    local = helm.get("local") is True
    external = any(k in helm for k in ("repoURL", "chart", "version")) and not local
    manifests = (doc.get("manifests") or {}).get("enabled") is True

    # 4. 外部與本地 Helm 互斥
    if local and "repoURL" in helm:
        errors.append("helm.repoURL 與 helm.local: true 不可同時設定")

    # 5. 至少一種來源
    if not (local or external or manifests):
        errors.append("no enabled source：helm.repoURL、helm.local、manifests.enabled 至少要有一個")

    # 6. 外部 Helm
    if external:
        for k in ("chart", "version", "valueFiles"):
            if k not in helm:
                errors.append("外部 Helm 缺少 helm.%s" % k)
        if str(helm.get("version", "")).strip() in BAD_VERSIONS:
            errors.append("helm.version 必須釘選，不可為空 / HEAD / LATEST / *")
        for f in helm.get("valueFiles") or []:
            if not os.path.isfile(os.path.join(app_dir, f)):
                errors.append("valueFiles 的 %s 不存在（相對於 app 目錄）" % f)

    # 7. Git 內 Helm
    if local:
        if not os.path.isfile(os.path.join(app_dir, "helm", "Chart.yaml")):
            errors.append("helm.local: true 但 helm/Chart.yaml 不存在")
        for f in helm.get("valueFiles") or []:
            if not os.path.isfile(os.path.join(app_dir, "helm", f)):
                errors.append("valueFiles 的 %s 不存在（相對於 helm/）" % f)

    # 8. manifests
    if manifests:
        mdir = os.path.join(app_dir, "manifests")
        files = glob.glob(os.path.join(mdir, "*.yaml")) + glob.glob(os.path.join(mdir, "*.yml"))
        real = [f for f in files if os.path.basename(f) not in ("kustomization.yaml", "kustomization.yml")]
        if not real:
            errors.append("manifests.enabled: true 但 manifests/ 底下沒有實際的資源檔")
        for f in files:
            try:
                list(yaml.safe_load_all(io.open(f, encoding="utf-8")))
            except yaml.YAMLError:
                errors.append("%s 不是合法的 YAML" % os.path.relpath(f, app_dir))

    # 9. 長度
    name = os.path.basename(os.path.abspath(app_dir))
    if len(name) > 53:
        errors.append("app 目錄名稱 %s 超過 53 字元（Helm release name 上限）" % name)
    for k in LABEL_FIELDS:
        if len(str(doc.get(k, ""))) > 63:
            errors.append("%s 超過 63 字元（Kubernetes label value 上限）" % k)
    return errors


def main(paths):
    failed = 0
    for p in paths:
        errors = check(p)
        print("%-4s  %s" % ("FAIL" if errors else "OK", p))
        for e in errors:
            print("      ✗ %s" % e)
        failed += bool(errors)
    print("\n%d 個檔案，%d 個不通過" % (len(paths), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法：python ci/validate_app_yaml.py applications/*/app.yaml")
    sys.exit(main(sys.argv[1:]))
