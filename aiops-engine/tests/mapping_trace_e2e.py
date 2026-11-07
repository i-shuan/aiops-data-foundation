"""欄位對照的覆寫擴到 trace，對真的 ES 跑一遍（docs/pdca/meta/2026-11-mapping-trace.md）：APM 的合成資料（labels.dept D3120、labels.group mes）。

  ES_URL=http://127.0.0.1:9200 ES_AUTH=x:x python3 -X utf8 -m tests.mapping_trace_e2e      （在 aiops-engine/ 底下；會重寫 APM 的合成資料、清掉 aiops-mapping、traces-merged-*）
預期（先寫）：①沒覆寫時合成的 trace：dept D3120、group mes、索引 traces-merged-mes-*；②核准「群組改讀 labels.dept」後重合：group d3120 那個索引
（索引名小寫）、文件 group D3120；③raw 索引提一個不是 trace 的索引（APM 的 metrics），核准被擋；④raw 索引核准成沒資料的索引（--force），
trace-merge 合 0 個；⑤停用後照 trace.yaml 合得回來；⑥list 看得到 trace.raw_index 的 yaml 值。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from common.infra.es import ES
from tests.aiops_es import AiopsES     # engine 自己的資料在 PostgreSQL：aiops-* 的請求轉過去
from tests import apm_synth
from tests.mapping_e2e import check, main_
import tests.mapping_e2e as base


def merge(es: ES, at: str) -> tuple[int, dict]:
    """清掉 merged 與 trace-merge 的進度，重合；回 (合了幾個, {索引: {(dept, group)…}})。"""
    es.req("POST", "/traces-merged-*/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}}, ok=(200, 404))
    es.req("DELETE", "/aiops-state/_doc/trace-merge", ok=(200, 404))
    rc, out = main_("detector", "trace-merge", "--config", "configs/apm/trace.yaml", "--at", at, "--window", "60m")
    n = int(out.rsplit("合了 ", 1)[1].split(" ")[0]) if "合了 " in out else -1
    es.req("POST", "/traces-merged-*/_refresh", ok=(200, 404))
    res = es.req("POST", "/traces-merged-*/_search", {"size": 500, "_source": ["dept", "group"]}, ok=(200, 404))
    got: dict = {}
    for h in res.get("hits", {}).get("hits", []):
        got.setdefault(h["_index"].rsplit("-", 1)[0], set()).add((h["_source"].get("dept"), h["_source"].get("group")))
    return n, got


def proposed_id(out: str) -> str:
    return out.split("已提：")[1].split("（")[0]


def main() -> int:
    es = AiopsES(ES())
    got = apm_synth.load(os.environ["ES_URL"], minutes=40, incident=15, clean=True)
    # trace-merge 看 event.ingested（真的寫進去的時間），--at 要在寫完之後：settle 30 秒，多給 2 分鐘
    at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    es.req("DELETE", "/aiops-mapping", ok=(200, 404))

    print("== 沒覆寫")
    n, idx = merge(es, at)
    check("①dept D3120、group mes、索引 traces-merged-mes", n > 0 and idx == {"traces-merged-mes": {("D3120", "mes")}}, (n, idx))

    rc, out = main_("detector", "mapping", "propose", "--source", "apm", "--section", "owner", "--field", "group", "--value", "labels.dept", "--by", "e2e")
    g = proposed_id(out)
    rc, out = main_("detector", "mapping", "approve", "--id", g, "--by", "e2e-主管")
    print("  " + out.splitlines()[0])
    n, idx = merge(es, at)
    check("②群組改讀 labels.dept：trace 也跟著（索引名小寫）", rc == 0 and idx == {"traces-merged-d3120": {("D3120", "D3120")}}, (rc, idx))
    main_("detector", "mapping", "disable", "--id", g, "--by", "e2e-主管")

    rc, out = main_("detector", "mapping", "propose", "--source", "apm", "--section", "trace", "--field", "raw_index", "--value", apm_synth.METRICS, "--by", "e2e")
    rc2, out2 = main_("detector", "mapping", "approve", "--id", proposed_id(out), "--by", "e2e-主管")
    check("③不是 trace 的索引（APM metrics）：核准被擋", rc == 0 and rc2 == 2 and "不是這個格式" in out2, out2[:300])

    rc, out = main_("detector", "mapping", "propose", "--source", "apm", "--section", "trace", "--field", "raw_index", "--value", "traces-apm.nothing-*", "--by", "e2e")
    empty = proposed_id(out)
    rc2, out2 = main_("detector", "mapping", "approve", "--id", empty, "--by", "e2e-主管")
    rc3, out3 = main_("detector", "mapping", "approve", "--id", empty, "--by", "e2e-主管", "--force")
    n, idx = merge(es, at)
    check("④沒資料的索引：不 --force 不給；--force 後 trace-merge 讀新的索引、合 0 個", rc2 == 2 and rc3 == 0 and n == 0, (rc2, rc3, n, out3[:200]))

    main_("detector", "mapping", "disable", "--id", empty, "--by", "e2e-主管")
    n, idx = merge(es, at)
    check("⑤停用後照 trace.yaml 合得回來", n > 0 and idx == {"traces-merged-mes": {("D3120", "mes")}}, (n, idx))

    rc, out = main_("detector", "mapping", "list")
    line = next((l for l in out.splitlines() if "raw_index" in l), "")
    check("⑥list 有 trace.raw_index、值是 trace.yaml 的 traces-apm-*", "traces-apm-*" in line, line)
    print("\n" + ("全部通過" if base.ok else "有項目失敗"))
    return 0 if base.ok else 1


if __name__ == "__main__":
    sys.exit(main())
