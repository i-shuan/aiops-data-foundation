"""Check：事故紀錄、值班回饋、誤報統計（docs/feedback/README.md、docs/pdca/README.md 的 Check）。

先只收集、不自動生效：
  事故 aiops-incidents   每次事故記時間、影響的服務、根因、處置；可選擇同時提一筆「這段不學」（proposed，人核准才生效）
  回饋 aiops-feedback    值班對每一筆異常的判斷：true_positive（真的）、false_positive（誤報）、
                         wrong_root_cause（真的但根因報告錯了）、missed（漏報：該報卻沒報）
  summary               每條序列的告警數、被標記的比例、誤報率；誤報累積到 min_false_positive 次的列出來
  suggest               誤報累積多次的 → 提一筆放寬門檻的建議（status: proposed，人 approve 才生效）；
                        單次回饋不動門檻（防污染：一個人按錯不會改壞）

這裡只做計算（單元測試用）；讀寫 ES 在 __main__.py。
"""
from __future__ import annotations

import json
import re

import pandas as pd

FEEDBACK_INDEX = "aiops-feedback"
INCIDENT_INDEX = "aiops-incidents"
VERDICTS = ("true_positive", "false_positive", "wrong_root_cause", "missed")

FEEDBACK_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "anomaly_id": {"type": "keyword"}, "anomaly_at": {"type": "date"},
    "series": {"type": "keyword"}, "keys": {"type": "flattened"}, "verdict": {"type": "keyword"},
    "incident_id": {"type": "keyword"}, "correct_root_cause": {"type": "text"}, "note": {"type": "text"},
    "by": {"type": "keyword"}, "alert_id": {"type": "keyword"}, "alert_started_at": {"type": "date"},
    "fp_reason": {"type": "keyword"},
    # 自由文字＋LLM 抽出來的四欄（docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）；extract.ok false 就只有原文
    "feedback_text": {"type": "text"},
    "extract": {"properties": {"ok": {"type": "boolean"}, "provider": {"type": "keyword"}, "reason": {"type": "text"},
                               "feedback_type": {"type": "keyword"}, "identified_cause": {"type": "keyword"},
                               "target_service": {"type": "keyword"}, "suggested_action": {"type": "keyword"}}},
    "tuning_id": {"type": "keyword"},      # 抽取結果寫進 aiops-tuning 的那一筆（proposed）
    # 採納率的兩題：可能根因／建議的排查動作有沒有參考價值（yes、no、not_seen）
    "rca_cause_useful": {"type": "keyword"}, "rca_action_useful": {"type": "keyword"}}}}
RCA_USEFUL = ("yes", "no", "not_seen")
# identified_cause（core/llm.py 的 CAUSE_CODES）→ 表單的根因分類；規則草稿的 root_cause_category 用。沒列的是 other
CAUSE_CATEGORY = {"db_slow_query": "db", "db_lock": "db", "db_connection_pool": "db", "network": "network", "dns": "network",
                  "config_change": "config_change", "deploy": "deploy", "external_api": "external", "resource_cpu": "resource",
                  "resource_memory": "resource", "disk_full": "resource", "bug": "bug", "data_volume": "data", "dirty_data": "data"}
# status：open／resolved（人補登的）、auto（事件整合自動開的，還沒人看）、confirmed（老師傅標真的）、corrected（真的但根因改過）
# 相似事故檢索只用 confirmed、corrected（investigator/similar.py）
ROOT_CAUSE_CATEGORIES = ("db", "network", "config_change", "deploy", "external", "resource", "bug", "data", "other")
INCIDENT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "title": {"type": "text"}, "start": {"type": "date"}, "end": {"type": "date"},
    "services": {"type": "keyword"}, "severity": {"type": "keyword"}, "status": {"type": "keyword"},
    "root_cause": {"type": "text"}, "action": {"type": "text"}, "anomaly_ids": {"type": "keyword"},
    "detected_at": {"type": "date"}, "resolved_at": {"type": "date"}, "by": {"type": "keyword"},
    # 老師傅的經驗（docs/pdca/phase5-learning-loop/2026-10-expert-feedback.md）
    "alert_id": {"type": "keyword"}, "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "root_cause_category": {"type": "keyword"}, "how_detected": {"type": "text"}, "checked": {"type": "keyword"},
    "improve": {"type": "text"}, "confirmed_at": {"type": "date"}, "confirmed_by": {"type": "keyword"},
    # 歷史事故單匯入（investigator/importer.py）：source import；note 是匯入檔的備註
    "source": {"type": "keyword"}, "note": {"type": "text"},
    # 指紋：相似事故檢索用（investigator/similar.py）
    "fingerprint": {"properties": {"services": {"type": "keyword"}, "routes": {"type": "keyword"}, "series": {"type": "keyword"},
                                   "slow_segments": {"type": "keyword"}, "error_origins": {"type": "keyword"},
                                   "patterns": {"type": "text"}}}}}}


def target_of(keys: dict | None) -> str:
    """一個異常的「對象」，用來把同一個對象的回饋歸在一起。"""
    return json.dumps(keys or {}, sort_keys=True, ensure_ascii=False)


def summarize(anomalies: pd.DataFrame, feedback: pd.DataFrame, *, min_false_positive: int = 1) -> dict:
    """anomalies：series、target、status（只算 anomaly；warning 還沒通知任何人）；
    feedback：series、target、verdict、anomaly_id（missed 沒有）。

    回傳 {"series": 每條序列一列的 DataFrame, "candidates": 誤報累積到 min_false_positive 次的（序列＋對象）,
          "missed": 漏報的}。
    """
    a = anomalies[anomalies["status"] == "anomaly"] if not anomalies.empty else anomalies
    cols = ["series", "alerts", "reviewed", "true_positive", "false_positive", "wrong_root_cause", "missed",
            "fp_rate", "reviewed_rate"]
    if a.empty and feedback.empty:
        return {"series": pd.DataFrame(columns=cols), "candidates": pd.DataFrame(columns=["series", "target", "false_positive"]),
                "missed": pd.DataFrame(columns=["series", "target"])}
    alerts = a.groupby("series").size().rename("alerts") if not a.empty else pd.Series(dtype=int, name="alerts")
    f = feedback.copy() if not feedback.empty else pd.DataFrame(columns=["series", "target", "verdict", "anomaly_id"])
    # 同一筆異常多人標記：最後一筆為準（feedback 已依時間排序）
    judged = f[f["verdict"] != "missed"]
    if "anomaly_id" in judged and judged["anomaly_id"].notna().any():
        judged = pd.concat([judged[judged["anomaly_id"].notna()].drop_duplicates("anomaly_id", keep="last"),
                            judged[judged["anomaly_id"].isna()]])
    counts = judged.groupby(["series", "verdict"]).size().unstack(fill_value=0)
    for v in VERDICTS[:3]:
        if v not in counts:
            counts[v] = 0
    out = pd.concat([alerts, counts[list(VERDICTS[:3])]], axis=1).fillna(0)
    out["missed"] = f[f["verdict"] == "missed"].groupby("series").size().reindex(out.index).fillna(0)
    # 只在漏報裡出現的序列
    only_missed = set(f[f["verdict"] == "missed"]["series"]) - set(out.index)
    for s in only_missed:
        out.loc[s] = [0, 0, 0, 0, int((f[(f["verdict"] == "missed") & (f["series"] == s)]).shape[0])]
    out = out.astype(int).reset_index().rename(columns={"index": "series"})
    out["reviewed"] = out["true_positive"] + out["false_positive"] + out["wrong_root_cause"]
    out["fp_rate"] = (out["false_positive"] / out["reviewed"]).where(out["reviewed"] > 0)
    out["reviewed_rate"] = (out["reviewed"] / out["alerts"]).where(out["alerts"] > 0)
    fp = judged[judged["verdict"] == "false_positive"].groupby(["series", "target"]).size().rename("false_positive").reset_index()
    return {"series": out[cols].sort_values(["alerts", "series"], ascending=[False, True]).reset_index(drop=True),
            "candidates": fp[fp["false_positive"] >= min_false_positive].sort_values("false_positive", ascending=False).reset_index(drop=True),
            "missed": f[f["verdict"] == "missed"][["series", "target"]].drop_duplicates().reset_index(drop=True)}


def suggest_override(series: str, target: str, n_false_positive: int, current_z: float = 5.0, factor: float = 1.4) -> dict:
    """誤報累積多次 → 放寬門檻的建議（z 乘 factor）。status: proposed，人 approve 才生效。
    match 用「完全符合這個對象」：欄位名稱不變，值前後加 ^$ 並跳脫。"""
    keys = json.loads(target)
    return {"kind": "override", "series": re.escape(series), "match": {k: f"^{re.escape(str(v))}$" for k, v in keys.items()},
            "z": round(current_z * factor, 1), "status": "proposed", "source": "feedback",
            "reason": f"誤報 {n_false_positive} 次（值班回饋），z {current_z:g} → {round(current_z * factor, 1)}"}


def suggest_by_precision(series_rows, current_z: float = 5.0, *, low: float = 0.7, high: float = 0.95, min_reviewed: int = 10,
                         loosen: float = 1.4, tighten: float = 0.8) -> list[dict]:
    """每條序列看最近的 precision（真的 ÷ 有人標的；根因報錯也算真的）：低於 low 提放寬（整條序列，z × loosen）、
    高於 high 而且有漏報提收緊（z × tighten）。reviewed 不到 min_reviewed 不提（幾筆就跳不準）。
    series_rows：summarize()["series"] 的 DataFrame（series、reviewed、true_positive、wrong_root_cause、missed）。
    一樣是 proposed，照舊回測、人核准；跟誤報累積 3 次（單一對象）那條互補：這條看整條序列的比例。"""
    out = []
    for r in series_rows.to_dict("records"):
        n = int(r.get("reviewed") or 0)
        if n < min_reviewed:
            continue
        prec = (int(r.get("true_positive") or 0) + int(r.get("wrong_root_cause") or 0)) / n
        base = {"kind": "override", "series": re.escape(str(r["series"])), "match": {}, "status": "proposed", "source": "feedback"}
        if prec < low:
            out.append({**base, "z": round(current_z * loosen, 1),
                        "reason": f"precision {prec:.0%}（{n} 筆有人標，低於 {low:.0%}），整條放寬 z {current_z:g} → {round(current_z * loosen, 1)}"})
        elif prec > high and int(r.get("missed") or 0) > 0:
            out.append({**base, "z": round(current_z * tighten, 1),
                        "reason": f"precision {prec:.0%}（{n} 筆）且漏報 {int(r['missed'])} 次，整條收緊 z {current_z:g} → {round(current_z * tighten, 1)}"})
    return out


def _match_of(target: str) -> dict:
    return {k: f"^{re.escape(str(v))}$" for k, v in json.loads(target).items()}


def _slot(fps: list[dict], tz: str, min_share: float, min_n: int = 3):
    """誤報是不是集中在同一個時段：先看「每月最後一天的同一個小時」，再看「同一個星期幾的同一個小時」。
    回傳 (schedule, 說明) 或 None。schedule 是 recurring_override 的格式（cron 當地時間、duration、days_of_month）。
    min_n：至少幾筆才看得出時段（老師傅已經說是批次時可以 1 筆）。"""
    import calendar
    from collections import Counter
    local = [pd.Timestamp(f["at"]).tz_convert(tz) for f in fps if f.get("at")]
    if len(local) < max(1, min_n):
        return None
    last = Counter(t.hour for t in local if t.day == calendar.monthrange(t.year, t.month)[1])
    if last:
        h, n = last.most_common(1)[0]
        if n / len(local) >= min_share:
            return {"cron": f"0 {h} * * *", "duration": "1h", "days_of_month": [-1]}, f"{n}/{len(local)} 次在每月最後一天 {h} 點"
    (dow, h), n = Counter((t.day_of_week, t.hour) for t in local).most_common(1)[0]
    if n / len(local) >= min_share:
        names = "一二三四五六日"
        return {"cron": f"0 {h} * * {(dow + 1) % 7}", "duration": "1h"}, f"{n}/{len(local)} 次在星期{names[dow]} {h} 點"
    return None


# 老師傅在表單選的「為什麼不是問題」（feedback_web.app.FP_REASONS）→ 這批誤報是哪一種；沒選就用算出來的特徵
REASON_SLOT = ("batch",)            # 固定時段的批次：那個時段不報、不學
REASON_ONEOFF = ("loadtest",)       # 一次性的：那段時間不報、不學（exclude_window）
REASON_VOLUME = ("low_volume",)     # 量太少幾筆就跳：min_count
REASON_DEPLOY = ("deploy",)         # 換版：reset_point 重學
REASON_SLOW = ("known_slow",)       # 本來就慢：min_abs


def fp_features(fps: list[dict], *, current_z: float = 5.0, factor: float = 1.4, tz: str = "Asia/Taipei", min_share: float = 0.8,
                reasons: list[str] | None = None, deploy_at=None, deploy_grace_min: int = 10) -> dict:
    """誤報的特徵，每個獨立算（docs/pdca/meta/2026-11-suggest-features.md）：
      reason        老師傅選的原因（最多人選的；沒選是 None）
      slot          集中在固定時段：(schedule, 說明) 或 None；老師傅說是批次時 1 筆也算
      z_ok          放寬 z（× factor）壓得掉：每一筆的 z 都 < z × factor（沒有 z 資料當壓得掉）
      low_volume    量太少：每筆符合的事件數（值 × 事件數）都 ≤ 5
      after_deploy  每一筆都在換版後 deploy_grace_min 分鐘內
      max_dev、max_hits  偏差最大值、符合的事件數最大值（給 min_abs、min_count 用）"""
    from collections import Counter
    reason = Counter(r for r in (reasons or []) if r and r != "other").most_common(1)
    reason = reason[0][0] if reason else None
    slot = _slot(fps, tz, min_share, min_n=1 if reason in REASON_SLOT else 3)
    zs = [abs(float(f["z"])) for f in fps if f.get("z") is not None]
    devs = [abs(float(f["value"]) - float(f["median"])) for f in fps if f.get("value") is not None and f.get("median") is not None]
    hits = [round(float(f["value"]) * float(f["n"])) for f in fps if f.get("value") is not None and f.get("n") not in (None, 0)]
    after = False
    if deploy_at is not None:
        d = pd.Timestamp(deploy_at)
        ats = [pd.Timestamp(f["at"]) for f in fps if f.get("at")]
        after = bool(ats) and all(pd.Timedelta(0) <= t - d < pd.Timedelta(minutes=deploy_grace_min) for t in ats)
    return {"reason": reason, "slot": slot, "z_ok": not zs or max(zs) < current_z * factor, "low_volume": bool(hits) and max(hits) <= 5,
            "after_deploy": after, "max_dev": max(devs) if devs else None, "max_hits": max(hits) if hits else None, "n": len(fps)}


def suggest_for_false_positives(series: str, target: str, fps: list[dict], current_z: float = 5.0,
                                current_min_abs: float = 0.0, tz: str = "Asia/Taipei", factor: float = 1.4,
                                min_share: float = 0.8, *, reasons: list[str] | None = None, current_min_count: int | None = None,
                                deploy_at=None) -> list[dict]:
    """有人標誤報就提（不用累積）：特徵全部算完（fp_features）再查表，老師傅選的原因優先。回傳一到兩筆建議（都是 proposed）。

    | 時段固定 | z 壓得掉 | 動作 |
    |---|---|---|
    | 是 | 是 | 那 1 小時 z × factor（recurring_override effect.z） |
    | 是 | 否 | 那 1 小時不報、不學（recurring_override effect.mute + train exclude） |
    | 否 | 是 | 整條 z × factor（override z） |
    | 否 | 否 | min_abs 調到最大偏差 × 1.2（override min_abs） |
    老師傅說換版／都在換版後 → reset_point；說壓測 → 那段時間不報不學（exclude_window）；量太少（說的或算的）→ 再加一筆 min_count。
    fps：這個對象被標成誤報的異常，每筆 {at, value, median, z, n}（缺的欄位就跳過那個判斷）。"""
    f = fp_features(fps, current_z=current_z, factor=factor, tz=tz, min_share=min_share, reasons=reasons, deploy_at=deploy_at)
    n, match, sr = f["n"], _match_of(target), re.escape(series)
    base = {"series": sr, "match": match, "status": "proposed", "source": "feedback"}
    who = f"誤報 {n} 次（值班回饋" + (f"：{f['reason']}" if f["reason"] else "") + "）"
    out = []
    if f["reason"] in REASON_DEPLOY or f["after_deploy"]:
        at = pd.Timestamp(deploy_at) if deploy_at is not None else min(pd.Timestamp(x["at"]) for x in fps if x.get("at")) - pd.Timedelta(minutes=10)
        out.append({**base, "kind": "reset_point", "reset_at": at.isoformat(), "reason": f"{who}，換版後的震盪：{at:%m-%d %H:%M} 起重學（reset_point）"})
    elif f["reason"] in REASON_ONEOFF:
        ats = [pd.Timestamp(x["at"]) for x in fps if x.get("at")]
        out.append({**base, "kind": "exclude_window", "start": (min(ats) - pd.Timedelta(minutes=5)).isoformat(),
                    "end": (max(ats) + pd.Timedelta(minutes=5)).isoformat(), "apply_to": ["train", "detect"],
                    "reason": f"{who}，一次性的（壓測、演練）：那段時間不報、不學"})
    elif f["slot"] and (f["reason"] in REASON_SLOT or not f["z_ok"]):
        sched, why = f["slot"]
        out.append({**base, "kind": "recurring_override", "schedule": sched, "effect": {"mute": True, "train": "exclude"},
                    "reason": f"{who}，{why}" + ("" if f["z_ok"] else f"、z 放寬到 {current_z * factor:g} 也壓不掉") + "：那 1 小時不報、也不學進基準線"})
    elif f["slot"]:
        sched, why = f["slot"]
        z = round(current_z * factor, 1)
        out.append({**base, "kind": "recurring_override", "schedule": sched, "effect": {"z": z}, "reason": f"{who}，{why}：只在那 1 小時 z {current_z:g} → {z}"})
    elif f["z_ok"] and f["reason"] not in REASON_SLOW:
        out.append(suggest_override(series, target, n, current_z, factor))
    else:
        new = float(f"{(f['max_dev'] or current_min_abs) * 1.2:.2g}")
        if new > current_min_abs:
            out.append({**base, "kind": "override", "min_abs": new,
                        "reason": f"{who}，z 放寬到 {current_z * factor:g} 也壓不掉、偏差最多 {f['max_dev']:.3g}：min_abs {current_min_abs:g} → {new:g}"})
        else:
            out.append(suggest_override(series, target, n, current_z, factor))
    if (f["reason"] in REASON_VOLUME or f["low_volume"]) and f["max_hits"] is not None:
        mc = int(f["max_hits"]) + 1
        if current_min_count is None or mc > current_min_count:
            out.append({**base, "kind": "override", "min_count": mc,
                        "reason": f"{who}，每次只有 {f['max_hits']} 筆符合就跳：min_count {current_min_count if current_min_count is not None else '—'} → {mc}"})
    return out


def suggest_missed(series: str, target: str, current_z: float = 5.0, factor: float = 0.8) -> dict:
    """漏報 → 那條序列、那個對象的 z 降一點（proposed；backtest 看抓不抓得到、誤報會不會變多）。"""
    keys = json.loads(target)
    return {"kind": "override", "series": re.escape(series), "match": {k: f"^{re.escape(str(v))}$" for k, v in keys.items()},
            "z": round(current_z * factor, 1), "status": "proposed", "source": "feedback",
            "reason": f"漏報（值班回饋），z {current_z:g} → {round(current_z * factor, 1)}"}


LATENCY_SERIES = re.compile(r"latency|p95|duration")


def rule_threshold(incs: list[dict], anomalies_of: dict[str, list[dict]] | None) -> tuple[float | None, str | None, list[float], set]:
    """門檻 = 每件事故裡「最低的那個異常值」（事故裡每個點都超過它），再取三件的最小值：三件事故、每個點都抓得到，
    而且一跳就報（baseline 第一個點只算 warning）。用峰值的話只有在最高點那 5 分鐘才會報，實測第二件在 10:12 不觸發。
    回傳 (門檻, 用哪條序列, 各件的最低異常值, 路由)。"""
    peaks, series, routes = [], None, set()
    for inc in incs:
        anoms = [a for a in (anomalies_of or {}).get(inc["id"], []) if LATENCY_SERIES.search(a.get("series") or "") and a.get("value") is not None]
        if not anoms:
            continue
        low = min(anoms, key=lambda a: float(a["value"]))
        peaks.append(float(low["value"]))
        series = series or low["series"]
        routes |= {(a.get("keys") or {}).get("route") or (a.get("keys") or {}).get("attributes.http.route") for a in anoms} - {None}
    return (round(min(peaks), 3) if peaks else None), series, peaks, routes


def suggest_rules(incidents: list[dict], min_recurring: int = 3, anomalies_of: dict[str, list[dict]] | None = None,
                  series_specs: list[dict] | None = None) -> list[dict]:
    """同一個服務、同一個根因分類被確認 ≥ min_recurring 次 → 提「升級成規則」（kind rule_suggestion，附 rule_draft）。
    門檻從那幾件事故的延遲峰值算（rule_threshold）；規則的查法直接用那條 baseline 序列的定義（同 index、欄位、切法），只多「只看這個服務」。
    人 adjust approve 之後 detector rules 自動載入（cmd_rules），不用改 rules.yaml。算不出門檻的草稿 threshold 是 None，不能核准。
    anomalies_of：{事故 id: [{series, value, keys}]}（事故 anomaly_ids 對到的異常）；series_specs：series.yaml 的 series。"""
    from collections import Counter
    by: dict[tuple, list[dict]] = {}
    for inc in incidents:
        for svc in inc.get("services") or []:
            by.setdefault((svc, inc.get("root_cause_category")), []).append(inc)
    out = []
    for (svc, cat), incs in by.items():
        if len(incs) < min_recurring:
            continue
        segs = Counter(s for i in incs for s in ((i.get("fingerprint") or {}).get("slow_segments") or []))
        errs = Counter(s for i in incs for s in ((i.get("fingerprint") or {}).get("error_origins") or []))
        where = (segs or errs).most_common(1)[0][0] if (segs or errs) else None
        threshold, series, peaks, routes = rule_threshold(incs, anomalies_of)
        spec = next((s for s in (series_specs or []) if s.get("name") == series), None) or {
            "index": "metrics-*.otel-*", "filter": {"exists": {"field": "metrics.http.server.request.duration"}},
            "by": ["resource.attributes.service.name", "attributes.http.request.method", "attributes.http.route"],
            "value": {"percentile": {"field": "metrics.http.server.request.duration", "percent": 95}}}
        filters = ([spec["filter"]] if spec.get("filter") else []) + [{"term": {spec["by"][0]: svc}}]
        if len(routes) == 1 and len(spec["by"]) >= 3:
            filters.append({"term": {spec["by"][-1]: next(iter(routes))}})
        is_rate = any(k in spec["value"] for k in ("percentile", "ratio", "hist_ratio")) if isinstance(spec.get("value"), dict) else False
        draft = {"name": f"{svc}.{cat}.recurring", "severity": "critical", "type": "window", "lookback": "5m",
                 "index": spec["index"], "filter": {"bool": {"filter": filters}}, "by": list(spec["by"]), "value": spec["value"],
                 "op": ">=", "threshold": threshold, "show": "seconds" if "percentile" in str(spec.get("value")) else "value",
                 "_source": {"series": series, "incident_ids": [i["id"] for i in incs], "floors": peaks, "route": next(iter(routes)) if len(routes) == 1 else None},
                 "_note": (f"門檻 = 這 {len(peaks)} 件事故各自最低的 {series} 異常值（{'、'.join(f'{p:g}' for p in peaks)}）取最小值：每件、每個點都抓得到，一跳就報"
                           if threshold is not None else "這幾件事故沒有延遲類的異常，算不出門檻：不能核准，要先看報告決定用哪條序列")
                          + (f"；慢的那段／錯的源頭最常是：{where}" if where else "")}
        if is_rate:
            draft["min_events"] = 20
        if threshold is not None:
            draft["title"] = f"{svc} 又出現 {cat} 的狀況（確認過 {len(incs)} 次）：{series} ≥ {threshold:g}"
        else:
            draft["title"] = f"{svc} 又出現 {cat} 的狀況（確認過 {len(incs)} 次）"
        out.append({"kind": "rule_suggestion", "service": svc, "root_cause_category": cat, "incident_ids": [i["id"] for i in incs],
                    "status": "proposed", "source": "feedback", "rule_draft": draft,
                    "reason": f"{svc} 的「{cat}」被確認 {len(incs)} 次：建議升級成規則（一次就報、不用等 baseline）"
                              + (f"，{series} ≥ {threshold:g}；核准後 rules 自動載入" if threshold is not None else "，但算不出門檻（見 rule_draft._note）")})
    return out


def suggest_from_text(fields: dict, alert: dict, text: str, current_z: float = 5.0, factor: float = 1.4) -> dict | None:
    """回饋文字抽出來的四欄（core.llm.extract_feedback）→ aiops-tuning 的一筆（status proposed、source feedback、reason 帶原文），
    人核准才生效。suggested_action 對到既有的調整種類：
      suppress_during_cron → recurring_override（mute）：缺 schedule（cron、duration），文字沒寫清楚就不猜，reason 請人補；
                             adjust list 會標「cron 要 5 欄」、補齊前不會生效
      widen_threshold      → override：這件的序列、這個服務，z × factor（跟誤報 3 次的建議一樣；先 backtest 再 approve）
      add_rule             → rule_suggestion：threshold None（算不出門檻，不能核准），序列與門檻要人看報告決定
      none                 → 不提（回 None）
    target_service 是抽出來的（文字點名別的服務就是那個），不一定是告警的服務。"""
    svc, act, cause = fields["target_service"], fields["suggested_action"], fields["identified_cause"]
    base = {"status": "proposed", "source": "feedback", "alert_id": alert.get("alert_id"), "feedback_type": fields["feedback_type"],
            "identified_cause": cause, "feedback_text": text}
    quote = f"值班回饋：「{text}」"
    if act == "suppress_during_cron":
        return {**base, "kind": "recurring_override", "match": {"service": f"^{re.escape(svc)}$"}, "effect": {"mute": True},
                "missing": ["schedule.cron", "schedule.duration"],
                "reason": f"{quote}→ 那段時間不報（{cause}）。缺 schedule：cron 開始時間、duration（每月幾號用 days_of_month），"
                          f"回饋文字沒寫清楚、不猜；請補上再回測、核准"}
    if act == "widen_threshold":
        series = [s for s in (alert.get("series") or []) if s]
        z = round(current_z * factor, 1)
        return {**base, "kind": "override", "series": "|".join(re.escape(s) for s in series) or None,
                "match": {"service": f"^{re.escape(svc)}$"}, "z": z,
                "reason": f"{quote}→ 放寬 z {current_z:g} → {z}（{cause}）；先 backtest 再 approve"}
    if act == "add_rule":
        return {**base, "kind": "rule_suggestion", "service": svc, "root_cause_category": CAUSE_CATEGORY.get(cause, "other"),
                "incident_ids": [alert["alert_id"]] if alert.get("alert_id") else [],
                "rule_draft": {"name": f"{svc}.{cause}.feedback", "severity": "critical", "threshold": None,
                               "_note": "值班回饋說這種情況以後要直接報；用哪條序列、門檻多少要人看報告決定（threshold 是 None，不能核准），"
                                        "定好後寫進 rules.yaml 或改這筆"},
                "reason": f"{quote}→ 建議加規則（{cause}），但算不出門檻：要人定（見 rule_draft._note）"}
    return None


def suggest_exclude(incident: dict) -> list[dict]:
    """事故 → 這段時間不學的建議（status: proposed）。有列影響的服務就只排除那些服務，沒有就全部。"""
    services = incident.get("services") or []
    base = {"kind": "exclude_window", "start": incident["start"], "end": incident["end"], "apply_to": ["train"],
            "status": "proposed", "source": "incident", "incident_id": incident["id"],
            "reason": f"事故：{incident.get('title', '')}"}
    if not services:
        return [base]
    return [{**base, "match": {"service": f"^{re.escape(s)}$"}} for s in services]


def format_summary(s: dict, days: int) -> list[str]:
    lines = [f"# 回饋統計（最近 {days} 天）", ""]
    t = s["series"]
    if t.empty:
        return lines + ["沒有資料", ""]
    tot = t[["alerts", "reviewed", "true_positive", "false_positive", "wrong_root_cause", "missed"]].sum()
    fp = tot["false_positive"] / tot["reviewed"] if tot["reviewed"] else float("nan")
    lines += [f"告警 {int(tot['alerts'])} 筆，已標記 {int(tot['reviewed'])} 筆（{tot['reviewed'] / tot['alerts']:.0%}），"
              f"誤報率 {fp:.0%}，漏報 {int(tot['missed'])} 筆" if tot["alerts"] and tot["reviewed"] else
              f"告警 {int(tot['alerts'])} 筆，已標記 {int(tot['reviewed'])} 筆，漏報 {int(tot['missed'])} 筆", "",
              "| 序列 | 告警 | 已標記 | 真的 | 誤報 | 根因錯 | 漏報 | 誤報率 |", "|---|---|---|---|---|---|---|---|"]
    for r in t.to_dict("records"):
        rate = "—" if pd.isna(r["fp_rate"]) else f"{r['fp_rate']:.0%}"
        lines.append(f"| {r['series']} | {r['alerts']} | {r['reviewed']} | {r['true_positive']} | {r['false_positive']} | "
                     f"{r['wrong_root_cause']} | {r['missed']} | {rate} |")
    lines.append("")
    if not s["candidates"].empty:
        lines += ["## 誤報累積多次（可以考慮放寬，`feedback suggest`）", ""]
        lines += [f"- {r['series']}：{r['target']}，{r['false_positive']} 次" for r in s["candidates"].to_dict("records")]
        lines.append("")
    if not s["missed"].empty:
        lines += ["## 漏報（補序列、補規則、降門檻）", ""]
        lines += [f"- {r['series']}：{r['target']}" for r in s["missed"].to_dict("records")]
        lines.append("")
    return lines
