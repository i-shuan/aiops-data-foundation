"""排除與調整、行事曆、慢性病清單的邊界（docs/pdca/phase2-anomaly/2026-10-adjust-schedule-verify.md）。

自己的 ES 灌合成資料（5 分鐘一點），用正式的指令（adjust add、train、detect、rules、chronic）跑，每一項印 PASS／FAIL。
可重跑：開頭刪掉自己的 index（aiops-adjtest*）、aiops-* 裡 adjtest 的資料、aiops-tuning 裡 adjtest- 開頭的調整。

執行（在 engine 底下；ES 先用 telemetry/scripts/elasticsearch-setup.sh 裝好 index 結構）：
  ES_URL=http://127.0.0.1:31505 ES_AUTH=x:x python -X utf8 -m tests.adjust_edge_e2e      （VERBOSE=1 印指令輸出）

三段時間軸（台北時間）：
  排除與調整  train 到 09-29 00:00（學 09-01～09-28 四週），detect 09-26、09-29～10-01
  行事曆      train 到 10-05 00:00，detect 10-05～10-11
  慢性病      chronic --at 10-12 08:00（看 10-05～10-11）、--at 10-19 08:00（看 10-12～10-18）
行事曆的台灣假日用 TaiwanCalendar 格式的測資（日期是挑的，不是真的假日），放在快取資料夾，不連網路。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd
import yaml

from detector.__main__ import main as _cli
from detector.es import ANOMALY_INDEX, ANOMALY_MAPPING, BASELINE_INDEX, ES

TZ = "Asia/Taipei"
MAIN, CAL, LOGS = "aiops-adjtest", "aiops-adjtest-cal", "aiops-adjtest-logs"
VERBOSE = os.environ.get("VERBOSE") == "1"
RNG = np.random.default_rng(7)
fails: list[str] = []


def L(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=TZ)


def cli(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cli([str(a) for a in argv])
    if VERBOSE:
        print(buf.getvalue())
    if rc:
        raise RuntimeError(f"{argv[0]} 回傳 {rc}：{buf.getvalue()[-500:]}")
    return buf.getvalue()


def check(name: str, ok: bool, got, want) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}：得到 {got}（預期 {want}）")
    if not ok:
        fails.append(name)


def noisy(v: np.ndarray) -> np.ndarray:
    return np.round(v * RNG.uniform(0.98, 1.02, len(v)), 2)


def grid(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(L(start), L(end), freq="5min", inclusive="left")


def put(es: ES, index: str, frames: list[pd.DataFrame], fields: dict) -> int:
    es.req("DELETE", f"/{index}", ok=(200, 404))
    es.req("PUT", f"/{index}", {"mappings": {"properties": {"@timestamp": {"type": "date"}, **fields}}})
    df = pd.concat(frames, ignore_index=True)
    rows = df.to_dict("records")
    for i in range(0, len(rows), 5000):
        lines = []
        for r in rows[i:i + 5000]:
            doc = {k: (v.isoformat() if isinstance(v, pd.Timestamp) else v) for k, v in r.items()
                   if not (isinstance(v, float) and np.isnan(v))}
            lines += [json.dumps({"index": {"_index": index}}), json.dumps(doc, ensure_ascii=False)]
        r = es.s.post(f"{es.url}/_bulk", data="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"}, timeout=300)
        if r.json().get("errors"):
            raise RuntimeError(r.text[:500])
    es.req("POST", f"/{index}/_refresh")
    return len(rows)


def baseline_of(es: ES, series: str) -> dict:
    hits = es.req("POST", f"/{BASELINE_INDEX}/_search", {"size": 10000, "query": {"term": {"series": series}}})["hits"]["hits"]
    return {(h["_source"]["keys"].get("service"), h["_source"]["dow"], h["_source"]["hour"]): h["_source"] for h in hits}


def anomalies(es: ES, prefix: str) -> list[tuple[str, str, pd.Timestamp]]:
    es.req("POST", f"/{ANOMALY_INDEX}/_refresh")
    hits = es.req("POST", f"/{ANOMALY_INDEX}/_search", {"size": 10000, "query": {"prefix": {"series": prefix}}})["hits"]["hits"]
    return [(h["_source"]["series"], (h["_source"]["keys"] or {}).get("service"),
             pd.Timestamp(h["_source"]["@timestamp"]).tz_convert(TZ)) for h in hits]


def clear_anomalies(es: ES, prefix: str) -> None:
    es.req("POST", f"/{ANOMALY_INDEX}/_delete_by_query?refresh=true&conflicts=proceed",
           {"query": {"prefix": {"series": prefix}}}, ok=(200, 404))


def write_yaml(path: str, data: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return path


def hhmm(ts: pd.Timestamp) -> str:
    return ts.strftime("%m-%d %H:%M")


# ── 1. 排除時段、重置點、週期放寬 ──

R_AT = L("2026-09-25 10:00")                      # 換版（重置點）
SPIKES_RST = [L("2026-09-26 09:30"), L("2026-09-26 10:30"), L("2026-09-29 15:00")]
BATCH = [(L("2026-09-29 20:00"), L("2026-09-30 00:00")), (L("2026-09-30 19:55"), L("2026-10-01 00:05"))]
MEGA = L("2026-09-30 21:00")


def main_data() -> list[pd.DataFrame]:
    ts = grid("2026-08-31", "2026-10-01 01:00")
    h, dow = ts.hour, ts.day_of_week
    day = np.where((h >= 8) & (h < 20), 100.0, 20.0) * np.where(dow >= 5, 0.5, 1.0)
    incident = (ts >= L("2026-09-14 22:00")) & (ts < L("2026-09-17 06:00"))    # 跨兩個午夜的長事故，3 倍
    out = [pd.DataFrame({"@timestamp": ts, "service": s, "requests": noisy(np.where(incident, day * 3, day))})
           for s in ("excl-on", "excl-off", "excl-naive")]
    ver = np.where(ts >= R_AT, 200.0, 100.0)          # 換版後量變 2 倍
    ver[ts.isin(SPIKES_RST)] = 1000.0
    out += [pd.DataFrame({"@timestamp": ts, "service": s, "requests": noisy(ver)}) for s in ("rst", "rst-ctl")]
    b = np.full(len(ts), 100.0)
    for a, z in BATCH:
        b[(ts >= a) & (ts < z)] = 300.0               # 批次：3 倍
    b[ts == MEGA] = 5000.0                            # 批次時段裡真的爆了
    out.append(pd.DataFrame({"@timestamp": ts, "service": "batch", "requests": noisy(b)}))
    return out


def test_adjustments(es: ES, tmp: str) -> None:
    print("\n== 1. 排除時段、重置點、週期放寬")
    n = put(es, MAIN, main_data(), {"service": {"type": "keyword"}, "requests": {"type": "double"}})
    print(f"  寫入 {n} 筆（6 個服務，08-31～10-01，5 分鐘一點）")
    cfg = write_yaml(os.path.join(tmp, "main.yaml"), {
        "interval": "5m", "history_weeks": 4,
        "adjustments_file": write_yaml(os.path.join(tmp, "adj.yaml"), {"adjustments": []}), "calendar_file": "",
        "train": {"timezone": TZ, "min_weeks": 2, "min_days": 3, "min_points": 50},
        "detect": {"z": 5, "rel_floor": 0.05},
        "series": [{"name": "adjtest.requests", "index": MAIN, "by": ["service"], "value": {"sum": "requests"},
                    "fill_zero": True, "direction": "both", "min_abs": 10}]})
    ser = ["--config", cfg, "--series", r"^adjtest\.requests$"]
    # 兩段重疊的排除時段（09-14 22:00～09-16 12:00、09-16 00:00～09-17 06:00）＝ 事故那 56 小時
    cli("adjust", "add", *ser, "--id", "adjtest-excl-1", "--match", "service=^excl-on$",
        "--start", "2026-09-14T22:00:00+08:00", "--end", "2026-09-16T12:00:00+08:00", "--reason", "事故前半")
    cli("adjust", "add", *ser, "--id", "adjtest-excl-2", "--match", "service=^excl-on$",
        "--start", "2026-09-16T00:00:00+08:00", "--end", "2026-09-17T06:00:00+08:00", "--reason", "事故後半（重疊）")
    # 值班手打、沒寫時區
    cli("adjust", "add", *ser, "--id", "adjtest-excl-naive", "--match", "service=^excl-naive$",
        "--start", "2026-09-21T02:00", "--end", "2026-09-21T05:00", "--reason", "沒寫時區")
    cli("adjust", "add", *ser, "--kind", "recurring_override", "--id", "adjtest-batch", "--match", "service=^batch$",
        "--cron", "0 20 * * *", "--duration", "4h", "--days-of-month", "-1", "--z", "100", "--reason", "月底批次")
    train_end = "2026-09-29T00:00:00+08:00"
    cli("train", "--config", cfg, "--end", train_end)
    b = baseline_of(es, "adjtest.requests")
    nb = lambda s, d, h: b[(s, d, h)]["n"]

    diff = sum(v["n"] for k, v in b.items() if k[0] == "excl-off") - sum(v["n"] for k, v in b.items() if k[0] == "excl-on")
    check("兩段重疊的排除時段：少學的點＝聯集（56 小時 × 12），不重複扣", diff == 672, diff, 672)
    got = [nb("excl-off", d, h) - nb("excl-on", d, h) for d, h in ((0, 21), (0, 22), (3, 5), (3, 6))]
    check("跨日排除的邊界 [開始, 結束)：週一 21 點、22 點、週四 5 點、6 點少學的點", got == [0, 12, 12, 0], got, [0, 12, 12, 0])
    p_off, p_on = b[("excl-off", 1, 12)]["p95"], b[("excl-on", 1, 12)]["p95"]
    check("事故期間不學：週二 12 點的 p95（沒排除 vs 排除；正常 100、事故 300）",
          p_off >= 250 and p_on <= 110, f"{p_off:.0f} vs {p_on:.0f}", "≥ 250 vs ≤ 110")
    got = [nb("excl-off", 0, h) - nb("excl-naive", 0, h) for h in (2, 4, 5, 10)]
    check("adjust add 沒寫時區（02:00～05:00）當成台北時間：週一 2、4、5、10 點少學的點", got == [12, 12, 0, 0], got, [12, 12, 0, 0])

    # 重置點：先 train（舊版的 baseline），再補標換版
    cli("adjust", "add", *ser, "--kind", "reset_point", "--id", "adjtest-rst", "--match", "service=^rst$",
        "--at", "2026-09-25T10:00:00+08:00", "--reason", "v2 上線")
    clear_anomalies(es, "adjtest.")
    cli("detect", "--config", cfg, "--at", "2026-09-30T00:00:00+08:00", "--window", "1D")
    a = anomalies(es, "adjtest.")
    n_rst = sum(1 for _, s, _ in a if s == "rst")
    n_ctl = sum(1 for _, s, _ in a if s == "rst-ctl")
    check("重置點：baseline 還是換版前學的（補標之前 train）→ 不判斷；沒標的對照組整天報",
          n_rst == 0 and n_ctl == 288, f"rst {n_rst}、對照 {n_ctl}", "rst 0、對照 288")

    cli("train", "--config", cfg, "--end", train_end)
    b = baseline_of(es, "adjtest.requests")
    rows = [v for k, v in b.items() if k[0] == "rst"]
    levels = sorted({v["level"] for v in rows})
    med = b[("rst", 1, 12)]["median"]
    check("重置點後重新 train：只用換版後 3.5 天學（level hour），平常是新版的 200",
          levels == ["hour"] and len(rows) == 168 and 190 <= med <= 210, f"{levels}、{len(rows)} 格、週二 12 點 {med:.0f}",
          "['hour']、168 格、190～210")

    clear_anomalies(es, "adjtest.")
    cli("detect", "--config", cfg, "--at", "2026-09-26T12:00:00+08:00", "--window", "3h")
    got = sorted(hhmm(t) for _, s, t in anomalies(es, "adjtest.") if s == "rst")
    check("暖機 24 小時：換版後 23.5 小時的爆量不報、24.5 小時的報", got == ["09-26 10:30"], got, ["09-26 10:30"])

    clear_anomalies(es, "adjtest.")
    cli("detect", "--config", cfg, "--at", "2026-10-01T00:05:00+08:00", "--window", "2885min")
    a = anomalies(es, "adjtest.")
    got = sorted(hhmm(t) for _, s, t in a if s == "rst")
    check("暖機後用新的平常：新版的 200 不報，爆量 1000 照報", got == ["09-29 15:00"], got, ["09-29 15:00"])

    bt = sorted(t for _, s, t in a if s == "batch")
    in29 = [t for t in bt if L("2026-09-29 20:00") <= t < L("2026-09-30 00:00")]
    in30 = [hhmm(t) for t in bt if L("2026-09-30 20:00") <= t < L("2026-10-01 00:00")]
    edges = [hhmm(t) for t in bt if t in (L("2026-09-30 19:55"), L("2026-10-01 00:00"))]
    check("不在週期內不放寬：09-29（不是月底）20:00～24:00 的批次照報", len(in29) == 48, f"{len(in29)} 點", "48 點")
    check("月底 20:00 起 4 小時放寬 z（100）：批次 z≈40 不報，真的爆量照報", in30 == ["09-30 21:00"], in30, ["09-30 21:00"])
    check("週期時段的邊界：19:55（開始前）、隔天 00:00（跨午夜、4 小時結束）照報",
          edges == ["09-30 19:55", "10-01 00:00"], edges, ["09-30 19:55", "10-01 00:00"])


# ── 2. 行事曆：台灣假日、補班日、維護、活動；假日當天真的故障 ──

HOLIDAYS = ["2026-09-16", "2026-10-06"]           # 週三（歷史裡）、週二（偵測那週）
MAKEUP = ["2026-09-19", "2026-10-10"]             # 週六補班
EVENTS = ["2026-09-24", "2026-10-08"]             # 活動（detect: true）
MAINT = (L("2026-10-07 02:00"), L("2026-10-07 06:00"))
OUTAGE = {"office": ["2026-10-05 14:00", "2026-10-06 14:00", "2026-10-07 03:00"], "fab": ["2026-10-06 14:00"]}


def cal_data() -> list[pd.DataFrame]:
    ts = grid("2026-09-06", "2026-10-12 01:00")
    date = pd.Index(ts.strftime("%Y-%m-%d"))
    h, dow = ts.hour, ts.day_of_week
    workday = ((dow < 5) | date.isin(MAKEUP)) & ~date.isin(HOLIDAYS)
    daytime = (h >= 8) & (h < 20)
    office = np.where(workday & daytime, 100.0, 5.0)
    office[date.isin(EVENTS) & daytime] = 300.0
    office_err = np.where(workday & daytime, 1.0, 0.0)
    fab = np.full(len(ts), 100.0)
    fab[(ts >= MAINT[0]) & (ts < MAINT[1] + pd.Timedelta("30min"))] = 0.0     # 維護超時 30 分鐘
    fab_err = np.full(len(ts), 2.0)
    for svc, err in (("office", office_err), ("fab", fab_err)):
        for s in OUTAGE[svc]:
            m = (ts >= L(s)) & (ts < L(s) + pd.Timedelta("1h" if svc == "fab" or "14:00" in s else "30min"))
            err[m] = 100.0
    return [pd.DataFrame({"@timestamp": ts, "service": "office", "requests": noisy(office), "errors": office_err}),
            pd.DataFrame({"@timestamp": ts, "service": "fab", "requests": noisy(fab), "errors": fab_err})]


def taiwan_fixture() -> list[dict]:
    """TaiwanCalendar 的格式：放假有 description、補班 isHoliday false 有 description、一般週末沒有 description。"""
    days = []
    for d in pd.date_range("2026-01-01", "2026-12-31", freq="D"):
        iso = d.strftime("%Y-%m-%d")
        if iso in HOLIDAYS:
            days.append({"date": d.strftime("%Y%m%d"), "week": "", "isHoliday": True, "description": "測試假日"})
        elif iso in MAKEUP:
            days.append({"date": d.strftime("%Y%m%d"), "week": "六", "isHoliday": False, "description": "測試補班"})
        else:
            days.append({"date": d.strftime("%Y%m%d"), "week": "", "isHoliday": d.day_of_week >= 5, "description": ""})
    return days


def test_calendar(es: ES, tmp: str) -> None:
    print("\n== 2. 行事曆")
    n = put(es, CAL, cal_data(), {"service": {"type": "keyword"}, "requests": {"type": "double"}, "errors": {"type": "double"}})
    print(f"  寫入 {n} 筆（office 週休、fab 24×7，09-06～10-12）")
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, "taiwan-2026.json"), "w", encoding="utf-8") as f:
        json.dump(taiwan_fixture(), f, ensure_ascii=False)
    tw = {"provider": "taiwan", "years": [2026], "cache_dir": cache, "cache_ttl": "3650d"}
    inline = [{"start": "2026-10-07T02:00", "end": "2026-10-07T06:00", "name": "測試機房維護", "type": "maintenance",
               "match": {"service": "^fab$"}}] + \
             [{"date": d, "name": "測試活動", "type": "event", "match": {"service": "^office$"}, "detect": True} for d in EVENTS]
    scoped = write_yaml(os.path.join(tmp, "cal-scoped.yaml"), {"timezone": TZ, "calendar": inline,
                                                                "sources": [{**tw, "match": {"service": "^office$"}}]})
    unscoped = write_yaml(os.path.join(tmp, "cal-unscoped.yaml"), {"timezone": TZ, "calendar": inline, "sources": [tw]})
    series = [{"name": "adjtest.cal.requests", "index": CAL, "by": ["service"], "value": {"sum": "requests"},
               "fill_zero": True, "direction": "both", "min_abs": 10},
              {"name": "adjtest.cal.errors", "index": CAL, "by": ["service"], "value": {"sum": "errors"},
               "fill_zero": True, "direction": "up", "min_abs": 5}]
    base = {"interval": "5m", "history_weeks": 4, "adjustments_file": "",
            "train": {"timezone": TZ, "min_weeks": 2, "min_days": 3, "min_points": 50}, "detect": {"z": 5, "rel_floor": 0.05},
            "series": series}
    cfg = write_yaml(os.path.join(tmp, "cal.yaml"), {**base, "calendar_file": scoped})
    cfg_none = write_yaml(os.path.join(tmp, "cal-none.yaml"), {**base, "calendar_file": ""})
    cfg_all = write_yaml(os.path.join(tmp, "cal-all.yaml"), {**base, "calendar_file": unscoped})

    cli("train", "--config", cfg, "--end", "2026-10-05T00:00:00+08:00")
    b = baseline_of(es, "adjtest.cal.requests")
    got = [b[("office", 2, 12)]["n"], b[("fab", 2, 12)]["n"]]
    check("台灣假日（歷史裡的週三）不學：office 週三 12 點少一天；限定 office，fab 照學", got == [36, 48], got, [36, 48])
    got = b[("office", 5, 12)]["n"]
    check("補班日（歷史裡的週六）不學；一般週末（沒有 description）不算假日", got == 36, got, 36)
    got = b[("office", 3, 12)]["n"]
    check("活動日（detect: true）也不學", got == 36, got, 36)

    def run(config: str) -> list[tuple[str, str, pd.Timestamp]]:
        clear_anomalies(es, "adjtest.cal.")
        cli("detect", "--config", config, "--at", "2026-10-12T00:00:00+08:00", "--window", "7D")
        return anomalies(es, "adjtest.cal.")

    def count(a, series, svc, start, end):
        return sorted(t for s, v, t in a if s == series and v == svc and L(start) <= t < L(end))

    a = run(cfg)
    a_none = run(cfg_none)
    a_all = run(cfg_all)
    req, err = "adjtest.cal.requests", "adjtest.cal.errors"
    got = (len(count(a, req, "office", "2026-10-10", "2026-10-11")), len(count(a_none, req, "office", "2026-10-10", "2026-10-11")))
    check("補班日（週六上班）：有行事曆不報；沒有行事曆跟週六比，白天整段誤報", got[0] == 0 and got[1] >= 140, got, "(0, ≥ 140)")
    got = (len(count(a, req, "office", "2026-10-06", "2026-10-07")), len(count(a_none, req, "office", "2026-10-06", "2026-10-07")))
    check("台灣假日（週二）流量掉：有行事曆不報；沒有行事曆白天整段誤報", got[0] == 0 and got[1] >= 140, got, "(0, ≥ 140)")
    got = [hhmm(t) for t in count(a, err, "fab", "2026-10-06", "2026-10-07")]
    check("假日當天真的故障：24×7 的 fab 錯誤爆量照報（假日限定 office）", len(got) == 12 and got[0] == "10-06 14:00",
          f"{len(got)} 點，從 {got[:1]}", "12 點，從 10-06 14:00")
    got = len(count(a, err, "office", "2026-10-06", "2026-10-07"))
    check("假日當天 office 自己的故障：baseline 不判斷（設計：假日不學也不報）", got == 0, got, 0)
    got = len(count(a_all, err, "fab", "2026-10-06", "2026-10-07"))
    check("台灣行事曆沒限定 match 時，fab 的故障也被吞（所以 taiwan 來源要寫 match）", got == 0, got, 0)
    rules = write_yaml(os.path.join(tmp, "rules.yaml"), {
        "interval": "1m", "lookback": "10m", "adjustments_file": "", "calendar_file": unscoped,
        "rules": [{"name": "adjtest.errors_total", "title": "測試：10 分鐘錯誤數", "severity": "critical", "type": "total",
                   "index": CAL, "field": "errors", "by": ["service"], "op": ">=", "threshold": 150, "fresh": "10m"}]})
    clear_anomalies(es, "rule.adjtest.errors_total")
    cli("rules", "--config", rules, "--at", "2026-10-06T14:10:00+08:00")
    got = sorted(v for s, v, _ in anomalies(es, "rule.adjtest.errors_total"))
    check("假日當天的故障，靜態臨界值規則不受行事曆影響：office、fab 都報", got == ["fab", "office"], got, ["fab", "office"])

    got = [hhmm(t) for t in count(a, req, "fab", "2026-10-07 00:00", "2026-10-07 08:00")]
    check("維護 02:00～06:00（只限 fab）：期間流量 0 不報，超時的 06:00～06:25 照報",
          got == [f"10-07 06:{m:02d}" for m in range(0, 30, 5)], got, "10-07 06:00～06:25 共 6 點")
    got = [hhmm(t) for t in count(a, err, "office", "2026-10-07 00:00", "2026-10-07 08:00")]
    check("維護限定 fab：同一時段 office 的錯誤爆量照報", got == [f"10-07 03:{m:02d}" for m in range(0, 30, 5)], got,
          "10-07 03:00～03:25 共 6 點")
    got = len(count(a, req, "office", "2026-10-08 08:00", "2026-10-08 20:00"))
    check("活動日（detect: true）：不學、但照常判斷，流量 3 倍照報", got == 144, got, 144)
    got = len(count(a, err, "office", "2026-10-05", "2026-10-06"))
    check("對照：平日 office 的故障照報", got == 12, got, 12)


# ── 3. 慢性病清單 ──

CHRONIC = {   # 服務 → (每天幾筆, 哪幾天有)；W1＝10-05～10-11、W2＝10-12～10-18
    "adjtest-nag": (60, pd.date_range("2026-10-05", "2026-10-14")),            # 天天噴，10-15 修好
    "adjtest-six": (30, pd.date_range("2026-10-05", "2026-10-11").drop(pd.Timestamp("2026-10-08"))),
    "adjtest-five": (30, pd.date_range("2026-10-05", "2026-10-11").drop([pd.Timestamp("2026-10-07"), pd.Timestamp("2026-10-09")])),
    "adjtest-twenty": (20, pd.date_range("2026-10-05", "2026-10-18")),
    "adjtest-nineteen": (19, pd.date_range("2026-10-05", "2026-10-18")),
    "adjtest-today": (999, pd.DatetimeIndex(["2026-10-12"])),                  # W1 跑的那天（還沒過完）
}


def chronic_data() -> list[pd.DataFrame]:
    out = []
    for svc, (per_day, days) in CHRONIC.items():
        for d in days:
            ts = pd.date_range(L(d.strftime("%Y-%m-%d")) + pd.Timedelta("1h"), periods=per_day, freq="20s")
            out.append(pd.DataFrame({"@timestamp": ts, "resource.attributes.service.name": svc, "log.level": "error",
                                     **({} if svc == "adjtest-nag" else {"error.type": "TimeoutException"})}))
    # 一筆 info 不算
    out.append(pd.DataFrame({"@timestamp": [L("2026-10-06 12:00")], "resource.attributes.service.name": "adjtest-info",
                             "log.level": "info"}))
    return out


def test_chronic(es: ES, tmp: str) -> None:
    print("\n== 3. 慢性病清單")
    n = put(es, LOGS, chronic_data(), {"resource.attributes.service.name": {"type": "keyword"},
                                       "log.level": {"type": "keyword"}, "error.type": {"type": "keyword"}})
    print(f"  寫入 {n} 筆錯誤 log")
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    clear_anomalies(es, "rule.adjtest.n_plus_one")
    docs = [(f"adjtest-nplus-{d:%m%d}", {"@timestamp": L(f"{d:%Y-%m-%d} 09:00").isoformat(), "series": "rule.adjtest.n_plus_one",
                                         "keys": {"service": "adjtest-nplus"}, "level": "rule", "value": 1})
            for d in pd.date_range("2026-10-05", "2026-10-11")]
    es.bulk_index(ANOMALY_INDEX, docs)
    es.req("POST", "/aiops-chronic/_delete_by_query?refresh=true&conflicts=proceed&ignore_unavailable=true",
           {"query": {"terms": {"week": ["2026-W41", "2026-W42"]}}}, ok=(200, 404))
    cfg = write_yaml(os.path.join(tmp, "chronic.yaml"), {
        "days": 7, "min_days": 6, "top": 10, "timezone": TZ,
        "sources": [{"name": "chronic.log_errors", "title": "天天有錯誤 log", "type": "log", "index": LOGS,
                     "filter": {"terms": {"log.level": ["error", "fatal"]}},
                     "by": ["resource.attributes.service.name", "error.type"], "keys": ["service", "error_type"], "min_per_day": 20},
                    {"name": "chronic.anomalies", "title": "天天觸發的規則", "type": "anomalies", "levels": ["rule", "peer"],
                     "series": "rule\\.adjtest\\..*", "min_per_day": 1}],
        "owners": [{"match": {"service": "^adjtest-"}, "owner": "測試團隊"}]})
    cli("chronic", "--config", cfg, "--at", "2026-10-12T08:00:00+08:00")
    cli("chronic", "--config", cfg, "--at", "2026-10-19T08:00:00+08:00")
    es.req("POST", "/aiops-chronic/_refresh")
    hits = es.req("POST", "/aiops-chronic/_search", {"size": 100, "query": {"terms": {"week": ["2026-W41", "2026-W42"]}}})["hits"]["hits"]
    weeks: dict[str, dict] = {"2026-W41": {}, "2026-W42": {}}
    for h in hits:
        s = h["_source"]
        who = s["keys"].get("series") or s["keys"].get("service")   # 規則那一類的 keys 是 series＋對象
        weeks[s["week"]][who] = s
    w1, w2 = weeks["2026-W41"], weeks["2026-W42"]
    got = (w1.get("adjtest-nag", {}).get("days"), w1.get("adjtest-nag", {}).get("keys", {}).get("error_type", "沒列"))
    check("天天噴錯（沒有 error.type）進清單：7 天裡 7 天", got == (7, None), got, "(7, None：歸成「（無）」)")
    check("修好後離開：10-15 起沒錯，下週（只剩 3 天）不在清單；上週那份還在", "adjtest-nag" not in w2 and "adjtest-nag" in w1,
          f"W42 {'有' if 'adjtest-nag' in w2 else '沒有'}、W41 {'有' if 'adjtest-nag' in w1 else '沒有'}", "W42 沒有、W41 有")
    got = ("adjtest-six" in w1, "adjtest-five" in w1)
    check("天數的邊界：7 天裡 6 天進、5 天不進", got == (True, False), got, (True, False))
    got = ("adjtest-twenty" in w1, "adjtest-twenty" in w2, "adjtest-nineteen" in w1)
    check("每天筆數的邊界：每天 20 筆進（兩週都在）、19 筆不進", got == (True, True, False), got, (True, True, False))
    got = ("adjtest-today" in w1, "adjtest-today" in w2)
    check("還沒過完的今天不算；下週只有 1 天也不算", got == (False, False), got, (False, False))
    got = ("rule.adjtest.n_plus_one" in w1, "rule.adjtest.n_plus_one" in w2)
    check("天天觸發的規則進清單、停了之後離開", got == (True, False), got, (True, False))
    owners = sorted({s["owner"] for s in list(w1.values()) + list(w2.values())})
    check("負責人依 match 對到", owners == ["測試團隊"], owners, ["測試團隊"])


def main() -> int:
    es = ES()
    info = es.req("GET", "/")
    print(f"ES {info['version']['number']}（{info['cluster_name']}），{es.url}")
    if not es.url.startswith(("http://127.0.0.1", "http://localhost")) and os.environ.get("ALLOW_REMOTE") != "1":
        print("這個測試會刪 aiops-* 裡 adjtest 的資料；只對本機 ES 跑（要對別的跑設 ALLOW_REMOTE=1）")
        return 2
    for idx in (BASELINE_INDEX, ANOMALY_INDEX):
        es.req("POST", f"/{idx}/_delete_by_query?refresh=true&conflicts=proceed&ignore_unavailable=true",
               {"query": {"prefix": {"series": "adjtest."}}}, ok=(200, 404))
    for i in ("adjtest-excl-1", "adjtest-excl-2", "adjtest-excl-naive", "adjtest-batch", "adjtest-rst"):
        es.req("DELETE", f"/aiops-tuning/_doc/{i}?refresh=true", ok=(200, 404))
    with tempfile.TemporaryDirectory() as tmp:
        test_adjustments(es, tmp)
        test_calendar(es, tmp)
        test_chronic(es, tmp)
    print(f"\n{'全部通過' if not fails else '失敗：' + '、'.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
