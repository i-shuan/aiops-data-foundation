#!/usr/bin/env python3
"""把 grafana/helmchart/files/ 的 provisioning 檔合成 Grafana 社群維護的 chart（grafana-community/grafana，原 grafana/grafana 已棄用）的 values：grafana/values-official.yaml。

用法：python3 grafana/code/build-official-values.py            產生（覆蓋 grafana/values-official.yaml）
      python3 grafana/code/build-official-values.py --check    產生的跟檔案裡的不一樣就失敗（CI 用，確認 helmchart/files/ 改了有重新產生）

helmchart/files/ 永遠是唯一的來源，values-official.yaml 是產生出來的，不要手改。
官方 chart 的對應：datasources、alerting（rules／contactpoints／policies）、dashboardProviders、dashboards。
chart 會把 datasources、alerting 的內容先 toYaml 再當 Helm template（tpl）處理，所以 Grafana 自己的 {{ $labels.x }}
要跳脫成 {{ `{{ $labels.x }}` }}（Go template 的原始字串，裡面不用引號，toYaml 之後也不會被改壞）；alerting 的值要是 map，不能是字串。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "helmchart" / "files"          # provisioning 的檔（也是自建 chart 讀的）
OUT = HERE.parent / "values-official.yaml"

HEADER = """\
# Grafana chart（helm repo grafana-community/grafana；原 grafana/grafana 已棄用、搬到 grafana-community）的 values：data source、告警、儀表板都在這一份。
# 由 grafana/code/build-official-values.py 從 grafana/helmchart/files/ 產生，**不要手改**：改 helmchart/files/ 的檔，再跑
#   python3 grafana/code/build-official-values.py
#
# 部署（Secret grafana-env 要先有：ES_URL、GRAFANA_ES_API_KEY、NOTIFY_IT_URL、NOTIFY_IMC_URL、NOTIFY_PLATFORM_URL、GF_SECURITY_ADMIN_USER、GF_SECURITY_ADMIN_PASSWORD；
# 或部署時用 --set secretData.… 建）：
#   helm repo add grafana-community https://grafana-community.github.io/helm-charts
#   helm upgrade --install grafana grafana-community/grafana -n grafana -f grafana/values-official.yaml \\
#     --set image.registry=<nexus> --set image.tag=13.0.7
#
# 要先改的：image（換成 Nexus）、contactpoints 的通知管道、policies 的 dept 清單、
#   儀表板 aiops-feedback 裡的兩個 http://localhost:18080（換成使用者瀏覽器連得到的回饋服務網址）。
# 回饋表單外掛（Business Forms）：公司的 Grafana 已經有裝就不用管；沒有的話在 plugins 加上（見下面），
#   chart 的 plugins 不核對 SHA-256，要核對請用 grafana/helmchart（我們自己的 chart）。
"""


class Literal(str):
    pass


def _literal(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(Literal, _literal, Dumper=yaml.SafeDumper)


def escape_helm(text: str) -> str:
    """Grafana 的 {{ ... }} 不能被 Helm 吃掉：每一段 {{ ... }} 換成 {{ `{{ ... }}` }}（Go template 的原始字串）。"""
    return re.sub(r"\{\{.*?\}\}", lambda m: "{{ `" + m.group(0) + "` }}", text)


def escape_tree(x):
    """map、list 裡的字串都跳脫（key 不會有 {{ }}）。"""
    if isinstance(x, str):
        return escape_helm(x)
    if isinstance(x, list):
        return [escape_tree(i) for i in x]
    if isinstance(x, dict):
        return {k: escape_tree(v) for k, v in x.items()}
    return x


def load(name: str):
    return yaml.safe_load((SRC / name).read_text(encoding="utf-8"))


def alerting_file(name: str) -> dict:
    """alerting 的檔：載成 map（chart 要 map，不收字串）、字串裡的 {{ }} 跳脫給 Helm。"""
    return escape_tree(load(name))


def build() -> str:
    values = {
        "image": {"registry": "nexus.example.internal", "repository": "grafana/grafana", "tag": "13.0.7"},
        "testFramework": {"enabled": False},
        # admin 帳密：chart 預設自己產生隨機密碼，grafana-env 裡的 GF_SECURITY_ADMIN_PASSWORD 不會生效（實測登入失敗），要明確指到 Secret
        "admin": {"existingSecret": "grafana-env", "userKey": "GF_SECURITY_ADMIN_USER", "passwordKey": "GF_SECURITY_ADMIN_PASSWORD"},
        "envFromSecret": "grafana-env",     # 檔裡的 ${ES_URL}、${GRAFANA_ES_API_KEY}、${NOTIFY_*_URL} 由 Grafana 從這個 Secret 的環境變數展開
        "datasources": {"datasources.yaml": load("datasource.yaml")},
        "alerting": {
            "rules.yaml": alerting_file("alert-rules.yaml"),
            "contactpoints.yaml": alerting_file("contact-points.yaml"),
            "policies.yaml": alerting_file("policies.yaml"),
        },
        "dashboardProviders": {"dashboardproviders.yaml": {"apiVersion": 1, "providers": [{
            "name": "aiops", "orgId": 1, "folder": "AIOps", "type": "file",
            "disableDeletion": False, "editable": True,
            "options": {"path": "/var/lib/grafana/dashboards/aiops"}}]}},
        "dashboards": {"aiops": {
            p.stem: {"json": Literal(p.read_text(encoding="utf-8"))}
            for p in sorted((SRC / "dashboards").glob("*.json"))}},
    }
    body = yaml.safe_dump(values, allow_unicode=True, sort_keys=False, width=1000)
    plugins = ("\n# 回饋表單外掛（Business Forms 6.3.1）：公司的 Grafana 沒裝再打開，官方 chart 用 grafana-cli 下載\n"
               "# plugins:\n#   - volkovlabs-form-panel 6.3.1 https://nexus.example.internal/repository/raw/grafana-plugins/volkovlabs-form-panel-6.3.1.zip\n")
    return HEADER + body + plugins


if __name__ == "__main__":
    out = build().replace("\r\n", "\n")
    if "--check" in sys.argv:
        cur = OUT.read_text(encoding="utf-8").replace("\r\n", "\n") if OUT.exists() else ""
        if cur != out:
            print("grafana/values-official.yaml 跟 helmchart/files/ 不一致：跑 python3 grafana/code/build-official-values.py", file=sys.stderr)
            sys.exit(1)
        print("values-official.yaml 跟 helmchart/files/ 一致")
    else:
        OUT.write_text(out, encoding="utf-8", newline="\n")
        print(f"已產生 {OUT.relative_to(HERE.parent.parent)}（{len(out)} 字）")
