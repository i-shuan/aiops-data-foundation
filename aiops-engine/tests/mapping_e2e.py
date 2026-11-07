"""欄位對照的覆寫，對真的 ES 跑一遍：APM 的合成資料，把「部門」從 labels.dept 改成 labels.group，核准後新寫的異常就用新的。

  ES_URL=http://127.0.0.1:9200 ES_AUTH=x:x python3 -X utf8 -m tests.mapping_e2e      （在 aiops-engine/ 底下；會重寫 APM 的合成資料、清掉 aiops-mapping）
預期（先寫）：①沒覆寫時異常的 dept 是 D3120（labels.dept）；②提了還沒核准，照舊；③核准後 dept 變 mes（labels.group）；
④提一個資料裡沒有的欄位，核准被擋；⑤停用後回到 D3120；⑥ES 裡的 yaml 沒被動（覆寫只在 aiops-mapping）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import json
from datetime import timedelta

from common.infra.es import ES
from tests import apm_synth

ok = True
ENV = {**os.environ, "AIOPS_SOURCE": "apm", "AIOPS_PLUGIN": "observability"}


def check(name, cond, detail=None):
    global ok
    ok &= bool(cond)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond or detail is None else f"：{detail}"))


def main_(*args) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "main", *args], capture_output=True, text=True, env=ENV)
    return r.returncode, (r.stdout + r.stderr).strip()


def depts(es: ES) -> set:
    es.req("POST", "/aiops-anomalies/_refresh", ok=(200, 404))
    res = es.req("POST", "/aiops-anomalies/_search", {"size": 100, "query": {"term": {"level": "rule"}}}, ok=(200, 404))
    return {h["_source"].get("dept") for h in res.get("hits", {}).get("hits", [])}


def run_rules(es: ES, at: str) -> set:
    es.req("POST", "/aiops-anomalies/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}}, ok=(200, 404))
    main_("detector", "rules", "--config", "configs/apm/rules.yaml", "--at", at)
    return depts(es)


def main() -> int:
    es = ES()
    got = apm_synth.load(os.environ["ES_URL"], minutes=40, incident=15, clean=True)
    at = got["now"]
    es.req("DELETE", "/aiops-mapping", ok=(200, 404))

    print("== 沒覆寫")
    check("①異常的 dept 是 labels.dept 的值（D3120）", run_rules(es, at) == {"D3120"}, depts(es))

    rc, out = main_("detector", "mapping", "propose", "--source", "apm", "--section", "owner", "--field", "dept", "--value", "labels.group", "--by", "e2e", "--reason", "部門改用 group")
    _id = next((w for w in out.split() if len(w) == 40 and all(c in "0123456789abcdef" for c in w)), None)
    _id = _id or out.split("已提：")[1].split("（")[0] if "已提：" in out else _id
    print("  " + out.splitlines()[0])
    check("②提了還沒核准：照舊（D3120）", rc == 0 and run_rules(es, at) == {"D3120"}, (rc, depts(es)))

    rc, out = main_("detector", "mapping", "approve", "--id", _id, "--by", "e2e-主管")
    print("  " + out.splitlines()[0])
    check("③核准後新寫的異常用新的欄位（labels.group → mes）", rc == 0 and run_rules(es, at) == {"mes"}, (rc, depts(es)))

    rc, out = main_("detector", "mapping", "propose", "--source", "apm", "--section", "owner", "--field", "service", "--value", "labels.nope", "--by", "e2e")
    bad = out.split("已提：")[1].split("（")[0]
    rc2, out2 = main_("detector", "mapping", "approve", "--id", bad, "--by", "e2e-主管")
    check("④資料裡沒有的欄位：提得出來、但核准被擋", rc == 0 and rc2 == 2 and "--force" in out2, out2[:200])

    rc, out = main_("detector", "mapping", "disable", "--id", _id, "--by", "e2e-主管")
    check("⑤停用後回到 yaml 的值（D3120）", rc == 0 and run_rules(es, at) == {"D3120"}, (rc, depts(es)))

    rc, out = main_("detector", "mapping", "list")
    check("⑥list 看得到 yaml 的值與提過的三筆（active→disabled、proposed）", "labels.dept" in out and out.count("[disabled]") == 1 and out.count("[proposed]") == 1, out[-400:])
    print("\n" + ("全部通過" if ok else "有項目失敗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
