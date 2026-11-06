"""慢性病清單：天天噴錯、沒人修的（docs/pdca/phase2-anomaly/2026-10-chronic-calendar.md）。

為什麼要：baseline 跟自己的歷史比，天天都有的錯會被學成「平常」，永遠不報；真的出事時，平常值已經被墊高，
反而更難抓。這種問題不是調門檻能解的，要列出來讓人修。修掉的下週自然從清單消失。

兩種來源（configs/baseline/chronic.yaml）：
  type: log        錯誤 log（依服務 × error.type）：最近 days 天裡至少 min_days 天有、每天至少 min_per_day 筆
  type: anomalies  aiops-anomalies 裡的規則、跟兄弟比（N+1、吞掉的例外、CPU 被限流、只有一個 Pod 壞…）：
                   同一條、同一個對象，最近 days 天裡至少 min_days 天有觸發

每個來源排前 top 名（天數多的先，再看量），附負責人（owners，依 match 找）。結果寫 aiops-chronic，並印成報告。

find_chronic、owner_of 只做計算（單元測試用）；fetch_* 是查詢。
"""
from __future__ import annotations

import json

import pandas as pd


from detect.adjustments import match_keys
from shared.connection import ES, doc_id
from shared.repos import Repos
from shared.scenario import load_scenario
from detect._util import _now, _v
from detect.providers import load_owners


def find_chronic(daily: pd.DataFrame, keys: list[str], *, min_days: int = 6, min_per_day: float = 1,
                 top: int = 10) -> pd.DataFrame:
    """daily：keys + ["day", "count"]，每個對象每天一列（沒有的那天可以不給）。

    回傳慢性病（keys + days、total、per_day＝有出現那幾天的中位數、first_day、last_day），排前 top 名：
    出現天數多的先，同天數看 total。每天至少 min_per_day 筆才算那天「有」。
    """
    cols = keys + ["days", "total", "per_day", "first_day", "last_day"]
    d = daily[daily["count"] >= min_per_day]
    if d.empty:
        return pd.DataFrame(columns=cols)
    g = d.groupby(keys, dropna=False)
    out = pd.DataFrame({"days": g["day"].nunique(), "total": g["count"].sum(), "per_day": g["count"].median(),
                        "first_day": g["day"].min(), "last_day": g["day"].max()}).reset_index()
    out = out[out["days"] >= min_days]
    return out.sort_values(["days", "total"], ascending=False).head(top)[cols].reset_index(drop=True)


def owner_of(key: dict, owners: list[dict]) -> str | None:
    """owners：[{match: {service: "^mes-"}, owner: "MES 團隊"}, …]，第一個符合的（欄位可用別名）。"""
    for o in owners or []:
        if match_keys(key, o.get("match")):
            return o["owner"]
    return None


def describe(source: dict, row: dict, keys: list[str], days: int) -> str:
    who = "、".join("（無）" if pd.isna(row[k]) else str(row[k]) for k in keys)   # None、NaN 都是「無」
    return (f"{source.get('title', source['name'])}：{who}，{days} 天裡 {int(row['days'])} 天有，"
            f"有的那幾天每天約 {row['per_day']:.0f} 筆，共 {int(row['total'])} 筆")


def anomaly_daily(repo: Repos, src: dict, start, end, tz: str) -> pd.DataFrame:
    """aiops-anomalies 裡 level 在 levels 的，每條（series）、每個對象（keys）每天幾筆。
    keys 是 flattened，聚合拿不到組合，所以讀出來在這裡數（一週的規則觸發量不大）。"""
    rows = [{"series": s["series"], "target": json.dumps(s.get("keys") or {}, sort_keys=True, ensure_ascii=False),
             "day": pd.Timestamp(s["@timestamp"]).tz_convert(tz).date(), "count": 1}
            for s in repo.anomalies.between(start, end, ["@timestamp", "series", "keys"], levels=src.get("levels", ["rule", "peer"]),
                                            series_regex=src.get("series"))]
    df = pd.DataFrame(rows, columns=["series", "target", "day", "count"])
    return df.groupby(["series", "target", "day"], as_index=False)["count"].sum() if not df.empty else df


def cmd_chronic(cfg: dict, args) -> int:
    repo = Repos(ES(), load_scenario())
    repo.chronic.ensure()
    tz = cfg.get("timezone", "Asia/Taipei")
    now = _now(args.at)
    days = cfg.get("days", 7)
    # 看最近 days 個「完整的日子」（當地時間）：今天還沒過完，不算
    end = now.tz_convert(tz).normalize().tz_convert("UTC")
    start = end - pd.Timedelta(days=days)
    week = f"{end.tz_convert(tz) - pd.Timedelta(days=1):%G-W%V}"
    owners = load_owners(cfg)
    docs, lines = [], [f"# 慢性病清單 {week}（{start.tz_convert(tz):%m/%d}～{(end - pd.Timedelta(days=1)).tz_convert(tz):%m/%d}，"
                       f"{days} 天裡至少 {cfg.get('min_days', 6)} 天有）", ""]
    for src in cfg["sources"]:
        if src["type"] == "log":
            keys = src.get("keys") or [f.split(".")[-1] for f in src["by"]]
            daily = repo.logs.daily_counts(src["index"], src["by"], keys, start, end, tz, src.get("filter"))
        elif src["type"] == "anomalies":
            daily = anomaly_daily(repo, src, start, end, tz)
            keys = ["series", "target"]
        else:
            raise ValueError(f"不認得的來源種類：{src['type']}")
        found = find_chronic(daily, keys, min_days=src.get("min_days", cfg.get("min_days", 6)),
                               min_per_day=src.get("min_per_day", 1), top=src.get("top", cfg.get("top", 10)))
        lines += [f"## {src.get('title', src['name'])}（{len(found)} 個）", ""]
        if not found.empty:
            lines += ["| # | 對象 | 天數 | 每天約 | 共 | 負責人 |", "|---|---|---|---|---|---|"]
        for rank, r in enumerate(found.to_dict("records"), 1):
            if src["type"] == "anomalies":
                target = json.loads(r["target"])
                key = {"series": r["series"], **target}
                who = f"{r['series']}：" + "、".join(f"{v}" for v in target.values())
            else:
                key = {k: _v(r[k]) for k in keys}
                who = "、".join(str(v) if v is not None else "（無）" for v in key.values())
            owner = owner_of(key, owners) or "未指定"
            msg = describe(src, r, keys, days) if src["type"] == "log" else (
                f"{src.get('title', src['name'])}：{who}，{days} 天裡 {int(r['days'])} 天有")
            docs.append((doc_id("chronic", week, src["name"], key), {
                "@timestamp": now.isoformat(), "week": week, "source": src["name"], "rank": rank, "keys": key,
                "days": int(r["days"]), "total": int(r["total"]), "per_day": float(r["per_day"]),
                "first_day": str(r["first_day"]), "last_day": str(r["last_day"]), "owner": owner, "message": msg}))
            lines.append(f"| {rank} | {who} | {int(r['days'])} | {r['per_day']:.0f} | {int(r['total'])} | {owner} |")
        lines.append("")
    repo.chronic.bulk(docs)
    print("\n".join(lines))
    return 0
