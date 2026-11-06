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

import numpy as np
import pandas as pd

from core.incident import INCIDENT_INDEX
from core.tuning import TUNING_INDEX
from detector import adjust as A
from detector import rules as R
from detector.adjust import load_adjustments
from detector.cmd_baseline import detect_series
from detector.common import _freq, _now, keys_of
from detector.es import ES, fetch, load_baseline
from detector.model import DetectOptions
from . import suggest as F


def _confirmed_incidents(es, since) -> list[dict]:
    hits = es.req("POST", f"/{INCIDENT_INDEX}/_search?ignore_unavailable=true", {
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


def _scope_hits(df: pd.DataFrame, proposals: list[dict], tz: str) -> int:
    """回測窗內「之前」的異常裡，落在這組建議範圍（序列、對象、時段）的有幾筆：0 筆就沒機會驗，不能說建議無效。"""
    if df.empty:
        return 0
    hit = np.zeros(len(df), dtype=bool)
    for p in proposals:
        sr = p.get("series") or ".*"
        m = np.array([bool(re.fullmatch(sr, str(s))) and A.match_keys(k or {}, p.get("match")) for s, k in zip(df["_series"], df["_keys"])])
        kind = p.get("kind") or "override"
        if kind == "recurring_override" and p.get("schedule"):
            m &= A.in_schedule(df["ts"], p["schedule"], tz)
        elif kind == "exclude_window":
            m &= df["ts"].between(_ts(p["start"], tz), _ts(p["end"], tz)).to_numpy()
        elif kind == "reset_point":
            m &= (df["ts"] >= _ts(p["reset_at"], tz)).to_numpy()
        hit |= m
    return int(hit.sum())


def run_backtest(es, cfg: dict, proposal, weeks: int, now=None, quiet: bool = True, ablate: bool = True) -> dict:
    """proposal：一筆建議（dict），或同一個對象的一組建議（list）——一組一起測，有效才逐筆剔除看哪筆多餘
    （docs/pdca/meta/2026-11-backtest-group.md）。
    回傳 {"verdict": effective／ineffective／unknown／harmful／worse, "recommended", "reason", "anomalies_before", "anomalies_after",
    "scope_hits", "incidents": [...], "incidents_caught_before/after", "missed_caught_after", "ablation": {id: redundant／necessary}}。
    每天一段（window 1D）跑 detect_series，每套設定各一次。"""
    es = _CachedES(es)   # 幾套設定讀同一批資料，只查一次；呼叫端（cmd_backtest）寫回時用的還是真的 es
    proposals = list(proposal) if isinstance(proposal, list) else [proposal]
    now = now or _now(None)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    dopts = DetectOptions(**cfg.get("detect", {}))
    cur_active = A.active(load_adjustments(es, cfg), now)
    kinds_data = ("exclude_window", "reset_point", "recurring_override")
    only_override = all((p.get("kind") or "override") == "override" for p in proposals)
    specs = [s for s in cfg["series"] if any(re.fullmatch(p.get("series") or ".*", s["name"]) for p in proposals)] if only_override \
        else list(cfg["series"])
    if not specs:
        return {"verdict": "unknown", "recommended": False, "reason": f"沒有序列符合 {[p.get('series') for p in proposals]}"}
    since = now - pd.Timedelta(weeks=weeks)
    days = pd.date_range(since.floor("D"), now.floor("D"), freq="D")
    bases = {s["name"]: load_baseline(es, s["name"], keys_of(s)) for s in specs}

    def run(adjs_all: list[dict]) -> pd.DataFrame:
        ov = [a for a in adjs_all if (a.get("kind") or "override") == "override"]
        adjs = [a for a in adjs_all if a.get("kind") in kinds_data]
        frames = []
        for spec in specs:
            keys, base = keys_of(spec), bases[spec["name"]]
            if base.empty:
                continue
            for d in days:
                day_end = min(d + pd.Timedelta(days=1), now)
                for r, s in detect_series(es, cfg, spec, base, day_end, day_end - d, dopts, tz, ov, adjs, quiet=quiet):
                    if s.empty:
                        continue
                    s = s[s["status"] == "anomaly"]
                    if not s.empty:
                        frames.append(s.assign(_series=spec["name"], _svc=[_service_of(x, keys) for x in s[keys].to_dict("records")],
                                               _keys=s[keys].to_dict("records")))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["ts", "_series", "_svc", "_keys"])

    def judge(before: pd.DataFrame, after: pd.DataFrame) -> dict:
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
        hits = _scope_hits(before, proposals, tz)
        res = {"anomalies_before": int(len(before)), "anomalies_after": int(len(after)), "scope_hits": hits,
               "incidents_total": len(incidents), "incidents_caught_before": sum(i["before"] for i in incidents),
               "incidents_caught_after": sum(i["after"] for i in incidents), "incidents_lost": [i["id"] for i in lost],
               "missed_total": len(missed), "missed_caught_after": sum(m["after"] for m in missed)}
        got_missed = any(m["after"] for m in missed)
        inc_txt = (f"已確認事故 {res['incidents_caught_after']}/{len(incidents)} 件還抓得到" if res["incidents_caught_before"]
                   else f"已確認事故 {len(incidents)} 件都不是這些序列抓的，不受影響" if incidents else "沒有已確認的事故可以對照")
        if lost:
            res.update(verdict="harmful", recommended=False, reason=f"有害：會漏掉 {len(lost)} 件已確認的事故：{'、'.join(i['title'] or i['id'] for i in lost)}")
        elif len(after) > len(before) and not got_missed:
            res.update(verdict="worse", recommended=False, reason=f"告警反而變多（{len(before)} → {len(after)}），也沒抓到漏報")
        elif len(after) == len(before) and not got_missed and hits == 0:
            res.update(verdict="unknown", recommended=False,
                       reason=f"無法判斷：回測 {weeks} 週內沒有落在這組建議範圍（序列、對象、時段）的異常，沒機會驗；拉長 --weeks 或等它再發生")
        elif len(after) == len(before) and not got_missed:
            res.update(verdict="ineffective", recommended=False, reason=f"無效：範圍內有 {hits} 筆異常，告警 {len(before)} → {len(after)} 一筆都沒少，也沒抓到漏報")
        else:
            res.update(verdict="effective", recommended=True, reason=f"有效：告警 {len(before)} → {len(after)}（範圍內 {hits} 筆）；{inc_txt}"
                                                                 + (f"；漏報抓到 {res['missed_caught_after']}/{len(missed)}" if missed else ""))
        return res

    before = run(cur_active)
    after = run(cur_active + [{**p, "status": "active"} for p in proposals])
    res = {"weeks": weeks, "series": [s["name"] for s in specs], "at": now.isoformat(), "group": [p.get("id") for p in proposals] if len(proposals) > 1 else None,
           **judge(before, after)}
    # 整組有效才逐筆剔除：拿掉某筆後結果不變 → 那筆多餘；變差 → 必要
    if ablate and len(proposals) > 1 and res["verdict"] == "effective":
        abl = {}
        for i, p in enumerate(proposals):
            others = [{**q, "status": "active"} for j, q in enumerate(proposals) if j != i]
            r_i = judge(before, run(cur_active + others))
            same = r_i["anomalies_after"] == res["anomalies_after"] and r_i["incidents_caught_after"] == res["incidents_caught_after"] \
                and r_i["missed_caught_after"] == res["missed_caught_after"]
            abl[p.get("id") or f"#{i + 1}"] = "redundant" if same else "necessary"
        res["ablation"] = abl
        n_red = sum(v == "redundant" for v in abl.values())
        if n_red:
            res["reason"] += f"；{n_red} 筆拿掉也一樣（多餘）：{'、'.join(k for k, v in abl.items() if v == 'redundant')}"
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
    got = es.req("POST", f"/{INCIDENT_INDEX}/_mget", {"ids": ids}).get("docs", []) if ids else []
    incidents = [{"id": g["_id"], **g["_source"]} for g in got if g.get("found") and g["_source"].get("start")]
    df = fetch(es, draft, now - pd.Timedelta(weeks=weeks), now, draft.get("lookback", "5m"))   # ES 的 fixed_interval 要 5m，不是 pandas 的 5min
    return rule_backtest_result(df, draft, incidents, weeks, tz)


def format_backtest(r: dict) -> str:
    if r.get("kind") == "rule_suggestion" and "fires" in r:
        return (f"回測 {r['weeks']} 週（規則 {r['rule']}，門檻 {r['threshold']:g}）：{'建議核准' if r['recommended'] else '不建議'}"
                f"（{r['reason']}）")
    if "anomalies_before" not in r:
        return f"回測：{r.get('reason')}"
    head = f"回測 {r['weeks']} 週（{'、'.join(r['series'])}" + (f"；一組 {len(r['group'])} 筆" if r.get("group") else "") + "）"
    abl = "；逐筆剔除：" + "、".join(f"{k[:8]} {'多餘' if v == 'redundant' else '必要'}" for k, v in r["ablation"].items()) if r.get("ablation") else ""
    return f"{head}：{'建議核准' if r['recommended'] else '不建議'}（{r['reason']}）{abl}"


def siblings(es, prop: dict) -> list[dict]:
    """同一個對象（同序列、同 match）、還是 proposed 的其他建議：同一批誤報提的幾個動作要一起測。"""
    hits = es.req("POST", f"/{TUNING_INDEX}/_search", {"size": 50, "query": {"bool": {"filter": [{"term": {"status": "proposed"}}]}}},
                  ok=(200, 404)).get("hits", {}).get("hits", [])
    same = lambda s: s.get("series") == prop.get("series") and json.dumps(s.get("match"), sort_keys=True) == json.dumps(prop.get("match"), sort_keys=True)
    return [{**h["_source"], "id": h["_id"]} for h in hits if h["_id"] != prop.get("id") and same(h["_source"]) and h["_source"].get("kind") != "rule_suggestion"]


def cmd_backtest(cfg: dict, args) -> int:
    es = ES()
    ids = args.id if isinstance(args.id, list) else [args.id]
    props = []
    for i in ids:
        got = es.req("GET", f"/{TUNING_INDEX}/_doc/{i}", ok=(200, 404))
        if not got.get("found"):
            print(f"找不到 {i}（adjust list 看得到）")
            return 2
        props.append({**got["_source"], "id": i})
    if getattr(args, "group", False):
        for p in list(props):
            props += [s for s in siblings(es, p) if s["id"] not in {q["id"] for q in props}]
    for p in props:
        print(f"建議：{A.describe(p)}")
    now = _now(args.at)
    if len(props) == 1 and props[0].get("kind") == "rule_suggestion":
        r = run_rule_backtest(es, cfg, props[0], args.weeks, now)
    else:
        if any(p.get("kind") == "rule_suggestion" for p in props):
            print("規則建議要單獨回測（rule_suggestion 不能跟門檻的建議一組）")
            return 2
        r = run_backtest(es, cfg, props if len(props) > 1 else props[0], args.weeks, now, quiet=not args.verbose)
    print(format_backtest(r))
    for p in props:
        mine = dict(r)
        if r.get("ablation") and r["ablation"].get(p["id"]) == "redundant":
            mine = {**r, "recommended": False, "reason": f"整組有效但這筆多餘（拿掉也一樣）；{r['reason']}"}
        es.req("POST", f"/{TUNING_INDEX}/_update/{p['id']}?refresh=true", {"doc": {"backtest": mine}})
    ok = [p["id"] for p in props if r.get("recommended") and (r.get("ablation") or {}).get(p["id"]) != "redundant"]
    print("已寫回這些建議的 backtest 欄位（adjust list 看得到）" + (f"；核准：{'  '.join('adjust approve --id ' + i for i in ok)}" if ok else "；沒有要核准的"))
    return 0
