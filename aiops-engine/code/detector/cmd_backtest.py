"""backtest：改門檻之前先回測（docs/pdca/phase5-learning-loop/2026-10-loop-update.md）。

一筆 aiops-tuning 的建議（status proposed，feedback suggest 提的、或人用 tune／adjust 寫的）：
用「現行設定」和「現行＋這筆建議」各重跑過去 N 週的 detect（不寫 ES），回答三件事：
  1. 告警（status anomaly 的點）少了幾個
  2. 已確認的事故（aiops-incidents 的 confirmed、corrected、resolved）在新設定下還抓不抓得到：
     事故的服務、時間範圍裡至少還有一個 anomaly 才算抓到；少抓一件就「不建議」
  3. 漏報（aiops-feedback 的 missed）有沒有抓到（降門檻的建議才看）
結果寫回那筆建議的 backtest 欄位，adjust list 看得到；adjust approve 沒回測或不建議的要 --force。

「升級成規則」（rule_suggestion）另一種算法（run_rule_backtest）：過去 N 週每 lookback（5 分鐘）一點算規則的值，
看提這條規則的那幾件事故抓不抓得到、事故以外會多報幾次。
"""
from __future__ import annotations

import json
import re

import pandas as pd

from .cmd_baseline import detect_series
from .es import ES, fetch, load_baseline
from .model import DetectOptions
from . import adjust as A
from . import feedback as F
from . import rules as R
from .common import _freq, _now, keys_of
from .tuning import TUNING_INDEX, load_adjustments


def _confirmed_incidents(es, since) -> list[dict]:
    hits = es.req("POST", f"/{F.INCIDENT_INDEX}/_search?ignore_unavailable=true", {
        "size": 1000, "query": {"bool": {"filter": [{"terms": {"status": ["confirmed", "corrected", "resolved"]}},
                                                    {"range": {"start": {"gte": since.isoformat()}}}]}}}).get("hits", {}).get("hits", [])
    return [{"id": h["_id"], **h["_source"]} for h in hits if h["_source"].get("services")]


def _missed(es, since) -> list[dict]:
    hits = es.req("POST", f"/{F.FEEDBACK_INDEX}/_search?ignore_unavailable=true", {
        "size": 1000, "query": {"bool": {"filter": [{"term": {"verdict": "missed"}}, {"range": {"@timestamp": {"gte": since.isoformat()}}}]}}}
    ).get("hits", {}).get("hits", [])
    return [h["_source"] for h in hits if h["_source"].get("anomaly_at")]


def _ts(x, tz: str) -> pd.Timestamp:
    """事故、漏報存的時間可能沒有時區（incident add 打 "2026-10-05 10:00"）：當成設定檔的時區，才能跟有時區的異常時間比。"""
    t = pd.Timestamp(x)
    return t.tz_localize(tz) if t.tzinfo is None else t


def caught(df: pd.DataFrame, services, start, end, tz: str = "UTC") -> bool:
    """這段時間、這些服務在 detect 結果裡至少有一個 anomaly。"""
    if df.empty:
        return False
    m = df["ts"].between(_ts(start, tz), _ts(end, tz))
    if services:
        m &= df["_svc"].isin([str(s) for s in services])
    return bool(m.any())


class _CachedES:
    """回測期間包住 ES：before／after 讀的是同一批原始資料（detect_series 裡的 fetch、alive_times），只有門檻不同，
    同一個查詢只打 ES 一次（GET／POST 的 _search 照 method、path、body 記住）。寫入（PUT、DELETE、_update、bulk_index）不快取、直接送。"""
    def __init__(self, es):
        self._es, self._cache = es, {}

    @staticmethod
    def _key(method, path, body):
        return (method, path, json.dumps(body, sort_keys=True, default=str))

    @staticmethod
    def _cacheable(method, path) -> bool:
        return method in ("GET", "POST") and "/_search" in path and "/_update" not in path

    def search(self, index, body):
        k = self._key("POST", f"/{index}/_search", body)
        if k not in self._cache:
            self._cache[k] = self._es.search(index, body)
        return self._cache[k]

    def req(self, method, path, body=None, **kw):
        if not self._cacheable(method, path):
            return self._es.req(method, path, body, **kw)
        k = self._key(method, path, body)
        if k not in self._cache:
            self._cache[k] = self._es.req(method, path, body, **kw)
        return self._cache[k]

    def __getattr__(self, name):    # bulk_index、url… 其他都交給真的 ES
        return getattr(self._es, name)


def _service_of(row: dict, keys: list[str]):
    for f in ("service", "resource.attributes.service.name", "entry.service"):
        if f in keys and row.get(f) is not None:
            return str(row[f])
    return None


def run_backtest(es, cfg: dict, proposal: dict, weeks: int, now=None, quiet: bool = True) -> dict:
    """回傳 {"anomalies_before", "anomalies_after", "incidents": [...], "incidents_caught_before/after", "missed_caught_after",
    "recommended", "reason"}。每天一段（window 1D）跑 detect_series，兩套設定各一次。"""
    es = _CachedES(es)   # 兩套設定讀同一批資料，只查一次；呼叫端（cmd_backtest）寫回時用的還是真的 es
    now = now or _now(None)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    dopts = DetectOptions(**cfg.get("detect", {}))
    current = load_adjustments(es, cfg)
    kinds_data = ("exclude_window", "reset_point", "recurring_override")
    cur_active = A.active(current, now)
    prop = {**proposal, "status": "active"}
    with_prop = cur_active + [prop]
    series_re = proposal.get("series") or ".*"
    specs = [s for s in cfg["series"] if re.fullmatch(series_re, s["name"])] if proposal.get("kind", "override") == "override" \
        else list(cfg["series"])
    if not specs:
        return {"recommended": False, "reason": f"沒有序列符合 {series_re}"}
    since = now - pd.Timedelta(weeks=weeks)
    days = pd.date_range(since.floor("D"), now.floor("D"), freq="D")
    frames = {"before": [], "after": []}
    for spec in specs:
        keys = keys_of(spec)
        base = load_baseline(es, spec["name"], keys)
        if base.empty:
            continue
        for label, adjs_all in (("before", cur_active), ("after", with_prop)):
            ov = [a for a in adjs_all if (a.get("kind") or "override") == "override"]
            adjs = [a for a in adjs_all if a.get("kind") in kinds_data]
            for d in days:
                day_end = min(d + pd.Timedelta(days=1), now)
                for r, s in detect_series(es, cfg, spec, base, day_end, day_end - d, dopts, tz, ov, adjs, quiet=quiet):
                    if s.empty:
                        continue
                    s = s[s["status"] == "anomaly"]
                    if not s.empty:
                        frames[label].append(s.assign(_series=spec["name"], _svc=[_service_of(x, keys) for x in s[keys].to_dict("records")]))
    before = pd.concat(frames["before"], ignore_index=True) if frames["before"] else pd.DataFrame(columns=["ts", "_series", "_svc"])
    after = pd.concat(frames["after"], ignore_index=True) if frames["after"] else pd.DataFrame(columns=["ts", "_series", "_svc"])

    incidents = []
    for inc in _confirmed_incidents(es, since):
        b, a = caught(before, inc.get("services"), inc["start"], inc["end"], tz), caught(after, inc.get("services"), inc["start"], inc["end"], tz)
        incidents.append({"id": inc["id"], "title": inc.get("title"), "services": inc.get("services"), "before": b, "after": a})
    missed = []
    for m in _missed(es, since):
        t = _ts(m["anomaly_at"], tz)
        svc = (m.get("keys") or {}).get("service") or (m.get("keys") or {}).get("resource.attributes.service.name")
        missed.append({"series": m.get("series"), "service": svc, "at": m["anomaly_at"],
                       "after": caught(after, [svc] if svc else None, t - pd.Timedelta(_freq(cfg.get("interval", "5m"))), t + pd.Timedelta("15min"), tz)})
    lost = [i for i in incidents if i["before"] and not i["after"]]
    res = {"weeks": weeks, "series": [s["name"] for s in specs], "anomalies_before": int(len(before)), "anomalies_after": int(len(after)),
           "incidents_total": len(incidents), "incidents_caught_before": sum(i["before"] for i in incidents),
           "incidents_caught_after": sum(i["after"] for i in incidents), "incidents_lost": [i["id"] for i in lost],
           "missed_total": len(missed), "missed_caught_after": sum(m["after"] for m in missed),
           "at": now.isoformat()}
    if lost:
        res.update(recommended=False, reason=f"會漏掉 {len(lost)} 件已確認的事故：{'、'.join(i['title'] or i['id'] for i in lost)}")
    elif len(after) > len(before) and not any(m["after"] for m in missed):
        res.update(recommended=False, reason=f"告警反而變多（{len(before)} → {len(after)}），也沒抓到漏報")
    else:
        # 已確認的事故本來就不是這條序列抓的（在別條序列、別的規則）：說清楚「不受影響」，不要寫成 0/3 像是抓不到
        inc_txt = (f"已確認事故 {res['incidents_caught_after']}/{len(incidents)} 件還抓得到" if res["incidents_caught_before"]
                   else f"已確認事故 {len(incidents)} 件都不是這條序列抓的，不受影響" if incidents else "沒有已確認的事故可以對照")
        res.update(recommended=True, reason=f"告警 {len(before)} → {len(after)}；{inc_txt}"
                                            + (f"；漏報抓到 {res['missed_caught_after']}/{len(missed)}" if missed else ""))
    return res


RULE_MAX_EXTRA_PER_WEEK = 1.0   # 事故以外平均每週最多多報幾次（估的；規則一核准每次都直接報，比 baseline 更不能吵）


def rule_backtest_result(df: pd.DataFrame, rule: dict, incidents: list[dict], weeks: int, tz: str = "UTC",
                         max_extra_per_week: float = RULE_MAX_EXTRA_PER_WEEK) -> dict:
    """df：fetch(規則, 過去 N 週, interval=lookback) 的結果（ts、value、n），每一點等於「那一輪 rules 看到的值」（只差在對齊）。
    incidents：提這條規則的事故 [{id, title, start, end}]。事故時間內（前後各放一個 lookback）有觸發就算抓到；
    其他時間的觸發算多報。全部抓到、多報平均每週不超過 max_extra_per_week 才建議核准。"""
    lb = R._dur(rule.get("lookback", "5m"))
    op, th, need = R.OPS[rule.get("op", ">=")], rule["threshold"], rule.get("min_events", 0)
    if df.empty:
        hits = pd.Series([], dtype="datetime64[ns, UTC]")
    else:
        v = pd.to_numeric(df["value"], errors="coerce")
        ok = v.notna() & (pd.to_numeric(df["n"], errors="coerce").fillna(0) >= need)
        hits = pd.to_datetime(df.loc[ok & op(v.fillna(0), th), "ts"], utc=True).reset_index(drop=True)
    windows = [(_ts(i["start"], tz) - lb, _ts(i["end"], tz) + lb) for i in incidents]
    rows = [{"id": i["id"], "title": i.get("title"), "caught": bool(((hits >= a) & (hits <= b)).any())}
            for i, (a, b) in zip(incidents, windows)]
    inside = pd.Series(False, index=hits.index)
    for a, b in windows:
        inside |= (hits >= a) & (hits <= b)
    extra = hits[~inside]
    per_week = len(extra) / max(weeks, 1)
    res = {"kind": "rule_suggestion", "weeks": weeks, "rule": rule.get("name"), "threshold": th, "points": int(len(df)),
           "fires": int(len(hits)), "incidents": rows, "incidents_caught": sum(r["caught"] for r in rows),
           "incidents_total": len(rows), "extra_fires": int(len(extra)),
           "extra_days": int(extra.dt.tz_convert(tz).dt.date.nunique()) if len(extra) else 0}
    lost = [r for r in rows if not r["caught"]]
    if lost:
        res.update(recommended=False, reason=f"提這條規則的事故有 {len(lost)} 件抓不到：{'、'.join(r['title'] or r['id'] for r in lost)}")
    elif per_week > max_extra_per_week:
        res.update(recommended=False, reason=f"事故以外會多報 {len(extra)} 次（{res['extra_days']} 天，平均每週 {per_week:.1f} 次，"
                                             f"上限 {max_extra_per_week:g}）")
    else:
        res.update(recommended=True, reason=f"{res['incidents_caught']}/{len(rows)} 件事故抓得到；事故以外 {weeks} 週多報 {len(extra)} 次")
    return res


def run_rule_backtest(es, cfg: dict, proposal: dict, weeks: int, now=None) -> dict:
    draft = {k: v for k, v in (proposal.get("rule_draft") or {}).items() if not k.startswith("_")}
    if not isinstance(draft.get("threshold"), (int, float)):
        return {"kind": "rule_suggestion", "recommended": False, "reason": "這條規則算不出門檻，不能回測（見 rule_draft._note）"}
    now = now or _now(None)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    ids = list(proposal.get("incident_ids") or [])
    got = es.req("POST", f"/{F.INCIDENT_INDEX}/_mget", {"ids": ids}).get("docs", []) if ids else []
    incidents = [{"id": g["_id"], **g["_source"]} for g in got if g.get("found") and g["_source"].get("start")]
    df = fetch(es, draft, now - pd.Timedelta(weeks=weeks), now, draft.get("lookback", "5m"))   # ES 的 fixed_interval 要 5m，不是 pandas 的 5min
    return rule_backtest_result(df, draft, incidents, weeks, tz)


def format_backtest(r: dict) -> str:
    if r.get("kind") == "rule_suggestion" and "fires" in r:
        return (f"回測 {r['weeks']} 週（規則 {r['rule']}，門檻 {r['threshold']:g}）：{'建議核准' if r['recommended'] else '不建議'}"
                f"（{r['reason']}）")
    if "anomalies_before" not in r:
        return f"回測：{r.get('reason')}"
    return (f"回測 {r['weeks']} 週（{'、'.join(r['series'])}）：{'建議核准' if r['recommended'] else '不建議'}（{r['reason']}）")


def cmd_backtest(cfg: dict, args) -> int:
    es = ES()
    got = es.req("GET", f"/{TUNING_INDEX}/_doc/{args.id}", ok=(200, 404))
    if not got.get("found"):
        print(f"找不到 {args.id}（adjust list 看得到）")
        return 2
    prop = {**got["_source"], "id": args.id}
    print(f"建議：{A.describe(prop)}")
    if prop.get("kind") == "rule_suggestion":
        r = run_rule_backtest(es, cfg, prop, args.weeks, _now(args.at))
    else:
        r = run_backtest(es, cfg, prop, args.weeks, _now(args.at), quiet=not args.verbose)
    print(format_backtest(r))
    es.req("POST", f"/{TUNING_INDEX}/_update/{args.id}?refresh=true", {"doc": {"backtest": r}})
    print("已寫回這筆建議的 backtest 欄位（adjust list 看得到）；核准：adjust approve --id " + args.id)
    return 0
