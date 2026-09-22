"""從正式的 vmstack-values.yaml 產生測試用的 VMAgent。

重點：scrapeClasses 和 remoteWrite 兩段**原封不動**從正式設定拿過來，
只把 remoteWrite 的 URL 換成本機的 vmauth。所以測的就是正式那份設定，
不是另外手寫的一份——手寫的話，測過了也不代表正式那份是對的。

其他欄位是測試專用（scrapeInterval 加快、只抓 metrics-sim、cluster 寫死），
都不影響分流邏輯，下面各自註明理由。

用法：python gen_vmagent.py | kubectl apply -f -
"""
import copy
import io
import os
import sys
from urllib.parse import urlsplit

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
# 可以用 VMSTACK_VALUES 指到別的檔案，用來做「故意改壞，確認測試抓得到」的驗證。
PROD_VALUES = os.environ.get(
    "VMSTACK_VALUES",
    os.path.join(HERE, "..", "..", "platform", "cluster-addons", "vmstack-values.yaml"))
TEST_VMAUTH = "http://vmauth-test.route-test.svc:8427"


def main():
    with io.open(PROD_VALUES, encoding="utf-8") as f:
        spec = yaml.safe_load(f)["vmagent"]["spec"]

    scrape_classes = copy.deepcopy(spec["scrapeClasses"])
    remote_write = copy.deepcopy(spec["remoteWrite"])
    for rw in remote_write:
        # 只換 scheme + host，路徑保留（/prometheus/api/v1/write），
        # 這樣 vmauth 收到的請求跟正式環境一樣。
        rw["url"] = TEST_VMAUTH + urlsplit(rw["url"]).path

    vmagent = {
        "apiVersion": "operator.victoriametrics.com/v1beta1",
        "kind": "VMAgent",
        "metadata": {"name": "under-test", "namespace": "route-test"},
        "spec": {
            # 正式是 30s。測試加快，只影響資料多快出現，不影響分流。
            "scrapeInterval": "10s",
            # 正式由 ApplicationSet 的 helm.parameters 注入。
            "externalLabels": {"cluster": "zk"},
            # 只抓 metrics-sim，避免跟 cluster 裡原本那台 vmagent 搶著抓別的東西。
            "selectAllByDefault": False,
            "serviceScrapeNamespaceSelector": {},
            "serviceScrapeSelector": {"matchLabels": {"app.kubernetes.io/name": "metrics-sim"}},
            "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"memory": "192Mi"}},
            # ↓ 這兩段來自正式設定，一個字都沒改（除了 URL）
            "scrapeClasses": scrape_classes,
            "remoteWrite": remote_write,
        },
    }

    print("# 由 tests/routing/gen_vmagent.py 產生，來源：platform/cluster-addons/vmstack-values.yaml", file=sys.stdout)
    yaml.safe_dump(vmagent, sys.stdout, allow_unicode=True, sort_keys=False)

    print("從正式設定取用：scrapeClasses（%d 條 relabel）、remoteWrite（%d 條）"
          % (len(scrape_classes[0]["relabelConfigs"]), len(remote_write)), file=sys.stderr)


if __name__ == "__main__":
    main()
