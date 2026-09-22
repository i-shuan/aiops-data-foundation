"""從正式設定產生 E2E 要用的東西。所有測試用的設定都從 platform/ 的正式檔案來，只換位址。

    python gen.py vmagent     測試用 VMAgent：取自 platform/vmstack-values.yaml，只換 remoteWrite 的 URL
    python gen.py appset      測試用 ApplicationSet：取自 platform/applicationset.yaml，只換 repoURL
    python gen.py repo        測試用 git repo 的內容（tar，寫到 stdout）

測試用 git repo 裡的 app，values.yaml 是從正式的 applications/metrics-sim/helm/values.yaml
改出來的：只換 namespace、映像、observability，chart 用的是真的。
情境定義在 check.py 的 CASES。

VMSTACK_VALUES 可以指到改壞的複本，用來確認測試抓得到錯。
"""
import copy
import io
import os
import sys
import tarfile
from urllib.parse import urlsplit

import yaml

import check

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
VMSTACK = os.environ.get("VMSTACK_VALUES", os.path.join(ROOT, "platform", "vmstack-values.yaml"))
APPSET = os.path.join(ROOT, "platform", "applicationset.yaml")
SIM_CHART = os.path.join(ROOT, "applications", "metrics-sim", "helm")

TEST_VMAUTH = "http://vmauth-test.route-test.svc:8427"
TEST_REPO = "http://git-server.route-test.svc:8080/repo.git"


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def dump(doc, source):
    print("# 由 tests/routing/gen.py 產生，來源：%s" % os.path.relpath(source, ROOT).replace("\\", "/"))
    yaml.safe_dump(doc, sys.stdout, allow_unicode=True, sort_keys=False)


def vmagent():
    spec = load(VMSTACK)["vmagent"]["spec"]
    remote_write = copy.deepcopy(spec["remoteWrite"])
    for rw in remote_write:
        # 只換 scheme + host，路徑保留，vmauth 收到的請求跟正式環境一樣
        rw["url"] = TEST_VMAUTH + urlsplit(rw["url"]).path
    dump({
        "apiVersion": "operator.victoriametrics.com/v1beta1",
        "kind": "VMAgent",
        "metadata": {"name": "under-test", "namespace": "route-test"},
        "spec": {
            "scrapeInterval": "10s",                   # 正式是 30s，只影響資料多快出現
            "externalLabels": {"cluster": "zk"},        # 正式由 cluster-addons 的 ApplicationSet 注入
            # 只抓 metrics-sim，不跟 cluster 裡原本那台 vmagent 搶
            "selectAllByDefault": False,
            "serviceScrapeNamespaceSelector": {},
            "serviceScrapeSelector": {"matchLabels": {"app.kubernetes.io/name": "metrics-sim"}},
            "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"memory": "192Mi"}},
            # ↓ 這兩段取自正式設定，除了 URL 一個字都沒改
            "scrapeClasses": copy.deepcopy(spec["scrapeClasses"]),
            "remoteWrite": remote_write,
        },
    }, VMSTACK)


def appset():
    doc = load(APPSET)
    # 只換 repo 位址，templatePatch（values → namespace label）原封不動
    for gen in doc["spec"]["generators"]:
        gen["git"]["repoURL"] = TEST_REPO
    doc["spec"]["template"]["spec"]["source"]["repoURL"] = TEST_REPO
    doc["metadata"]["name"] = "under-test"
    dump(doc, APPSET)


def repo():
    base = load(os.path.join(SIM_CHART, "values.yaml"))
    tar = tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT)

    def add_bytes(name, data):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    def add_dir(src, dest, skip=()):
        for d, _, files in os.walk(src):
            for f in files:
                p = os.path.join(d, f)
                rel = os.path.relpath(p, src).replace("\\", "/")
                if rel in skip:
                    continue
                add_bytes("%s/%s" % (dest, rel), open(p, "rb").read())

    for case, (observability, _) in check.CASES.items():
        values = copy.deepcopy(base)
        values["argocd"].update(namespace=check.namespace(case), project="default", autoSync=True)
        values["image"].update(repository="metrics-sim", tag="test")
        if observability is None:
            values.pop("observability", None)
        else:
            values["observability"] = observability
        app = "applications/sim-%s/helm" % case
        add_dir(SIM_CHART, app, skip=("values.yaml",))
        add_bytes(app + "/values.yaml", yaml.safe_dump(values, allow_unicode=True, sort_keys=False).encode())
    tar.close()


if __name__ == "__main__":
    {"vmagent": vmagent, "appset": appset, "repo": repo}[sys.argv[1]]()
