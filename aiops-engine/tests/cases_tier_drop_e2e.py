"""歷史 case 補根因分析、P0 服務升嚴重度、baseline 往下掉：對真的 ES 跑一遍（docs/pdca/meta/2026-11-cases-tier-drop.md）。

  ES_URL=http://127.0.0.1:9200 ES_AUTH=x:x python3 -X utf8 -m tests.cases_tier_drop_e2e      （在 aiops-engine/ 底下）
會重寫 APM 的合成資料、aiops-synth-drop，清掉 aiops-alerts、aiops-incidents、aiops-mapping，以及 aiops-baseline、aiops-anomalies、
aiops-tuning 裡 synth. 開頭的。

預期（先寫）：
  往下掉 ①4 週歷史的延遲 120ms、最後一天 wip-worker 掉到 40ms：detect 不判成異常（只判往上）
         ②shift 提一筆 synth.latency／wip-worker 的 reset_point（proposed），理由寫「只判往上」；其他服務不提
  P0     ③APM 合成資料（下游 wip-worker 出錯、上游 mes-api 連帶）的那一件，沒列 P0 是某個等級；列了 P0 升一級、why 寫 P0
  case   ④放一件確認過的事故（同一段、db 類、勾過 DB 連線池），重開告警：報告有「歷史 case 的線索」與「先查什麼」
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yaml

from common.infra.es import ES
from tests.aiops_es import AiopsES     # engine 自己的資料在 PostgreSQL：aiops-* 的請求轉過去
from tests import apm_synth

ok = True
TZ = "Asia/Taipei"
DROP_INDEX = "aiops-synth-drop"
TODAY = pd.Timestamp("2026-10-05", tz=TZ)


def check(name, cond, detail=None):
    global ok
    ok &= bool(cond)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond or detail is None else f"：{detail}"))


def main_(*args, source="apm") -> tuple[int, str]:
    env = {**os.environ, "AIOPS_SOURCE": source, "AIOPS_PLUGIN": "observability"}
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "main", *args], capture_output=True, text=True, env=env)
    return r.returncode, (r.stdout + r.stderr).strip()


def hits(es, index, query=None, size=100):
    es.req("POST", f"/{index}/_refresh", ok=(200, 404))
    res = es.req("POST", f"/{index}/_search", {"size": size, "query": query or {"match_all": {}}}, ok=(200, 404))
    return [{"_id": h["_id"], **h["_source"]} for h in res.get("hits", {}).get("hits", [])]


def clear(es, index, query=None):
    es.req("POST", f"/{index}/_delete_by_query?refresh=true&conflicts=proceed", {"query": query or {"match_all": {}}}, ok=(200, 404))


# ── 往下掉 ──

def drop_part(es: ES) -> None:
    print("== baseline 往下掉（只判往上的延遲）")
    rng = np.random.default_rng(7)
    ts = pd.date_range(TODAY - pd.Timedelta(weeks=4), TODAY + pd.Timedelta(days=1), freq="5min", inclusive="left")
    docs = []
    for svc in ("mes-api", "wip-worker"):
        day = (ts.hour >= 8) & (ts.hour < 20)
        lat = np.where(day, 120.0, 80.0) + rng.normal(0, 6, len(ts))
        if svc == "wip-worker":
            lat = np.where(ts >= TODAY, 40.0 + rng.normal(0, 2, len(ts)), lat)       # 最後一天修好了：掉到 40ms 停住
        req = np.where(day, 100.0, 20.0)
        docs += [(None, {"@timestamp": t.isoformat(), "service": svc, "requests": float(r), "latency_ms": float(v), "errors": 0.0})
                 for t, v, r in zip(ts, lat, req)]
    es.req("DELETE", f"/{DROP_INDEX}", ok=(200, 404))
    es.req("PUT", f"/{DROP_INDEX}", {"mappings": {"properties": {"@timestamp": {"type": "date"}, "service": {"type": "keyword"},
                                                                  "requests": {"type": "double"}, "latency_ms": {"type": "double"}, "errors": {"type": "double"}}}})
    for i in range(0, len(docs), 5000):
        lines = [x for _, d in docs[i:i + 5000] for x in (json.dumps({"index": {"_index": DROP_INDEX}}), json.dumps(d))]
        r = es.s.post(f"{es.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"}, timeout=300)
        assert not r.json().get("errors"), r.text[:300]
    es.req("POST", f"/{DROP_INDEX}/_refresh")
    cfg = yaml.safe_load(open("tests/synth.yaml", encoding="utf-8"))
    cfg["series"] = [dict(s, index=DROP_INDEX) for s in cfg["series"] if s["name"] == "synth.latency"]
    cfg.update(adjustments_file=None, calendar_file=None)
    path = os.path.join(tempfile.mkdtemp(), "drop.yaml")
    yaml.safe_dump(cfg, open(path, "w", encoding="utf-8"), allow_unicode=True)
    for idx in ("aiops-baseline", "aiops-anomalies", "aiops-tuning"):
        clear(es, idx, {"prefix": {"series": "synth"}})
    end_hist, end_today = TODAY.tz_convert("UTC").isoformat(), (TODAY + pd.Timedelta(days=1)).tz_convert("UTC").isoformat()
    main_("detector", "train", "--config", path, "--end", end_hist)
    main_("detector", "detect", "--config", path, "--window", "1D", "--at", end_today)
    anoms = [a for a in hits(es, "aiops-anomalies", {"prefix": {"series": "synth.latency"}}, 1000) if a["keys"].get("service") == "wip-worker"]
    base = hits(es, "aiops-baseline", {"prefix": {"series": "synth.latency"}}, 10)
    check("①學到 baseline、往下掉不判成異常（只判往上，不通知）", base and not anoms, (len(base), len(anoms)))
    rc, out = main_("detector", "shift", "--config", path, "--at", end_today)
    props = [t for t in hits(es, "aiops-tuning", {"term": {"kind": "reset_point"}}) if "synth" in str(t.get("series"))]
    svcs = [list(t["match"].values()) for t in props]
    check("②shift 提了 wip-worker 的 reset_point（proposed），理由寫只判往上；mes-api 沒提",
          len(props) == 1 and "wip" in str(svcs) and props[0]["status"] == "proposed" and "只判往上" in props[0]["reason"], (rc, svcs, out[-400:]))


# ── P0、case ──

def rca_cfg(extra_alerts: dict | None = None) -> str:
    cfg = yaml.safe_load(open("configs/apm/rca.yaml", encoding="utf-8"))
    # 規則的 critical 不直接算 critical（critical_levels 寫一個不存在的 level）：照訊號廣度評，P0 升一級才看得出來
    cfg.setdefault("alerts", {}).update({"llm": False, "deploy_grace": False, "critical_levels": ["_none"], "rca_lead": "20m", **(extra_alerts or {})})
    path = os.path.join(tempfile.mkdtemp(), "rca.yaml")
    yaml.safe_dump(cfg, open(path, "w", encoding="utf-8"), allow_unicode=True)
    return path


def run_alert(es, cfg, at) -> dict | None:
    clear(es, "aiops-alerts", {"bool": {"must_not": [{"ids": {"values": ["detect-status", "source-check-series", "source-check-trace"]}}]}})
    clear(es, "aiops-incidents", {"term": {"status": "auto"}})
    rc, out = main_("investigator", "alert", "--config", cfg, "--at", at)
    al = hits(es, "aiops-alerts", {"term": {"category": "service"}})       # 合成資料：下游 wip-worker 開一件、上游 mes-api 連帶併進去
    if not al:
        print(out[-800:])
    return al[0] if al else None


def alert_part(es: ES) -> None:
    print("== P0、歷史 case（APM 合成資料）")
    got = apm_synth.load(os.environ["ES_URL"], minutes=40, incident=15, clean=True)
    at = got["now"]
    merge_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    for idx in ("aiops-alerts", "aiops-incidents", "aiops-mapping"):
        clear(es, idx)
    clear(es, "traces-merged-*")
    es.req("DELETE", "/aiops-state/_doc/trace-merge", ok=(200, 404))
    main_("detector", "trace-merge", "--config", "configs/apm/trace.yaml", "--at", merge_at, "--window", "60m")
    clear(es, "aiops-anomalies")
    main_("detector", "rules", "--config", "configs/apm/rules.yaml", "--at", at)

    at = (pd.Timestamp(at) + pd.Timedelta(minutes=5)).isoformat()      # 異常在 at 那一點；告警讀到 at 之前
    a0 = run_alert(es, rca_cfg(), at)
    check("③-0 沒列 P0：開了一件", a0 is not None)
    if not a0:
        return
    svc = a0["service"]
    a1 = run_alert(es, rca_cfg({"p0_services": [svc]}), at)
    up = {"warning": "high", "high": "critical", "critical": "critical"}
    check(f"③列 P0：{a0['severity']} → {up[a0['severity']]}，why 寫 P0", a1 and a1["severity"] == up[a0["severity"]] and "P0" in (a1.get("severity_why") or ""),
          (a1 or {}).get("severity_why"))

    segs = [s["segment"] for s in (a0.get("findings") or {}).get("segments") or [] if s.get("delta_ms", 0) > 0]
    check("④-0 根因分析有變慢的段（有 trace）", bool(segs), (a0.get("findings") or {}).keys())
    if not segs:
        return
    start = a0["started_at"]
    inc = {"status": "confirmed", "source": "e2e", "title": "e2e：DB 慢（同一段）", "start": start, "end": a0.get("updated_at"),
           "services": [svc], "root_cause_category": "db", "root_cause": "e2e", "checked": ["DB 連線池"],
           "fingerprint": {"services": [svc], "routes": [], "series": [], "slow_segments": [segs[0]], "error_origins": [], "patterns": []}}
    es.req("PUT", "/aiops-incidents/_doc/e2e-case-1?refresh=true", inc)
    a2 = run_alert(es, rca_cfg(), at)
    report = (a2 or {}).get("report") or ""
    check("④報告有「歷史 case 的線索」（那一段、那件事故）與「先查什麼」（DB 連線池排第一）",
          "歷史 case 的線索" in report and "e2e：DB 慢" in report and "先查什麼" in report and "- [ ] DB 連線池" in report,
          report[:600])
    check("④findings 存了 case_hints", ((a2 or {}).get("findings") or {}).get("case_hints", [{}])[0].get("cases") == ["e2e-case-1"],
          ((a2 or {}).get("findings") or {}).get("case_hints"))


def main() -> int:
    es = AiopsES(ES())
    drop_part(es)
    alert_part(es)
    print("\n" + ("全部通過" if ok else "有項目失敗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
