"""alert：最近的異常 → 合成一件（investigator/alerts.py）→ 新的那件跑一次根因分析 → 寫 aiops-alerts。

Grafana 讀 aiops-alerts 發通知（engine/configs/grafana/）：status open 的每一件一則，依 dept 分送；
@timestamp 是「這次執行還看到它開著」的時間（心跳），所以 Grafana 只要查最近幾分鐘有 @timestamp 的 open 件。
這支要定期跑（建議每 1～2 分鐘，跟 rules、detect 之後）。
"""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.es import ES

from . import alerts as AL

ALERT_INDEX = "aiops-alerts"


def _dur(s: str) -> timedelta:
    """30s、30m、2h、1d"""
    s = str(s)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[s[-1]]: int(s[:-1])})
ALERT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "alert_id": {"type": "keyword"}, "category": {"type": "keyword"},
    "service": {"type": "keyword"}, "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "severity": {"type": "keyword"}, "status": {"type": "keyword"},
    "started_at": {"type": "date"}, "updated_at": {"type": "date"}, "resolved_at": {"type": "date"},
    "anomaly_ids": {"type": "keyword"}, "anomaly_count": {"type": "integer"}, "series": {"type": "keyword"},
    "routes": {"type": "keyword"},
    # Grafana 的告警只能帶 labels（terms 分組的值），所以標題、摘要、報告連結都存成 keyword
    "title": {"type": "keyword", "ignore_above": 1024}, "summary": {"type": "keyword", "ignore_above": 4096},
    "report_url": {"type": "keyword"}, "report": {"type": "text", "index": False}, "report_at": {"type": "date"},
}}}


def _anomalies(es: ES, index: str, start: datetime, end: datetime) -> list[dict]:
    out, after = [], None
    while True:
        body = {"size": 5000, "query": {"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}},
                "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.search(index, body)["hits"]["hits"]
        out += [{**h["_source"], "_id": h["_id"]} for h in hits]
        if len(hits) < 5000:
            return out
        after = hits[-1]["sort"]


def _open_alerts(es: ES) -> list[dict]:
    hits = es.search(ALERT_INDEX, {"size": 10000, "query": {"term": {"status": "open"}}})["hits"]["hits"]
    return [h["_source"] for h in hits]


def _done_ids(es: ES, since: datetime) -> set:
    """最近結束的件裡的異常 id：重跑時這些異常不再開新的一件。"""
    body = {"size": 10000, "_source": ["anomaly_ids"], "query": {"bool": {"filter": [
        {"term": {"status": "resolved"}}, {"range": {"resolved_at": {"gte": since.isoformat()}}}]}}}
    return {i for h in es.search(ALERT_INDEX, body)["hits"]["hits"] for i in h["_source"].get("anomaly_ids") or []}


def run(cfg: dict, args, investigate) -> int:
    """investigate：根因分析的函式（investigator.__main__.investigate），傳進來避免循環 import。"""
    es = ES()
    es.ensure_index(ALERT_INDEX, ALERT_MAPPING)
    acfg = cfg.get("alerts", {})
    tz = ZoneInfo(cfg.get("timezone", "Asia/Taipei"))
    now = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(timezone.utc)
    close_after, lookback = _dur(acfg.get("close_after", "30m")), _dur(acfg.get("lookback", "2h"))
    interval = _dur(cfg.get("interval", "5m"))
    open_alerts = _open_alerts(es)
    oldest = min([datetime.fromisoformat(str(a["updated_at"]).replace("Z", "+00:00")) for a in open_alerts], default=now)
    start = min(now - lookback, oldest - close_after)
    alerts = AL.merge(open_alerts, _anomalies(es, cfg["indices"]["anomalies"], start, now), now, close_after,
                      _done_ids(es, start - close_after))
    docs, new, resolved = [], 0, 0
    for al in alerts:
        al["dept"], al["group"] = AL.owner(al) if al.get("_anoms") else (al.get("dept"), al.get("group") or "unassigned")
        report = al.get("report")
        if (al.get("_new") or not report) and acfg.get("rca", True) and al["category"] == "service" and al["status"] == "open":
            # 新的一件跑一次根因分析：時間是這件的開始到最後一筆異常再往後一個 interval；只有一支 API 時只查那支
            route = al["routes"][0] if len(al["routes"]) == 1 else None
            try:
                report = investigate(cfg, Namespace(service=al["service"], route=route, start=AL._ts(al["started_at"]).isoformat(),
                                                    end=(AL._ts(al["updated_at"]) + interval).isoformat(), at=None, window=None))
            except Exception as e:   # 根因分析失敗不擋這一件
                report = f"根因分析失敗：{e}"
            al["report_at"] = now
        if al.get("_anoms") or al.get("_new") or not al.get("summary"):
            al["title"], al["summary"] = AL.headline(al), AL.summary(al, report, tz)
        url = acfg.get("report_url")
        doc = {k: v for k, v in al.items() if not k.startswith("_")}
        doc.update({"report": report, "report_url": url.format(alert_id=al["alert_id"]) if url else None,
                    "anomaly_count": len(al["anomaly_ids"]),
                    "@timestamp": (al["resolved_at"] if al["status"] == "resolved" else now)})
        docs.append((al["alert_id"], {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in doc.items()}))
        if al.get("_new") and al["status"] == "resolved":   # 已經過去的（例如這支停了一陣子才跑）：記下來，不算新開、不通知
            print(f"  ⏺ 已經過去的：{doc['title']}")
        elif al.get("_new"):
            new += 1
            print(f"  🆕 {doc['title']}（{doc['dept'] or '—'}／{doc['group']}）")
        elif al["status"] == "resolved":
            resolved += 1
            print(f"  ✅ 結束：{doc['title']}")
        elif al.get("_upgraded"):
            print(f"  ⬆️ {doc['title']}")
    if docs:
        es.bulk_index(ALERT_INDEX, docs)
    still = sum(1 for _, d in docs if d["status"] == "open")
    print(f"{now.astimezone(tz):%m-%d %H:%M}：新開 {new} 件、結束 {resolved} 件、進行中 {still} 件")
    return 0
