"""規則：狀態變化、離極限多遠。不用學、不用歷史，裝上去第一天就有用（docs/metrics/README.md 的做法 2、5）。

每條規則（configs/baseline/rules.yaml）：從一個資料流讀某個欄位，依 by 切成一條條序列，看最近一段時間的每個點：

  type: value     欄位 op 門檻                    例：Pod 狀態 == Failed、節點 Ready == 0、抓取 up == 0
  type: compare   欄位 − 另一個欄位 op 門檻         例：要的副本數 − 可用副本數 >= 1（副本不夠）
  type: ratio     欄位 ÷ (分母欄位加總) op 門檻     例：記憶體 working_set ÷ (working_set + available) >= 0.9
  type: increase  累計值這段時間增加了多少 op 門檻   例：重啟次數增加 >= 1、失敗的 Job Pod 增加 >= 1
                  （數字變小當成歸零重來，只能用在計數器；會上下動的量測值用 delta）
  type: delta     量測值這段時間淨增加多少（最後 − 最前） 例：Kafka 消費落後 5 分鐘內多了 >= 5,000 筆
  type: increase_ratio  兩個累計值增加量的比例        例：被限流的週期 ÷ 全部週期 >= 25%（CPU throttling）
  type: rate      這段時間的加總 ÷ 秒數 op 門檻       例：GC 花的秒數 ÷ 經過的秒數 >= 10%（直方圖的 sum）
  type: total     這段時間的加總 op 門檻              例：5 分鐘內 major GC 幾次 >= 5（hist_count: true 時數直方圖的次數）
  type: window    整段 lookback 算一個值 op 門檻      例：5 分鐘內的錯誤率 >= 5%、p95 >= 2 秒（直方圖的比例、百分位）
                  value、filter 的寫法跟 series.yaml 一樣（hist_ratio、percentile…）；min_events：請求數不到的不判斷
  type: slope     最近 for 這段的趨勢（Theil–Sen 斜率）  例：記憶體照這個速度 <= 6 小時後到上限（像洩漏）
                  有 den（上限 = den 加總）就算「幾小時後到上限」，沒有就是每小時漲多少（欄位的單位）。
                  只算一路往上的：三段的低點要一路墊高（GC 鋸齒回得到原來的低點就不算）；對象要有滿 for 的資料、
                  至少 min_points 個點（新容器啟動期的暖機不算）。

join：分母在另一種文件時（例如連線池「使用中」和「上限」分開存），用同一個對象、同一個時間點的值補進來：
  join: {field: metrics.db_client_connections_max}       之後 den 就可以寫這個欄位

for：要「這段時間都成立」才算（例如副本不夠 3 分鐘，避免換版滾動時的短暫不足）。0 表示最新一個點成立就算。
     對象要在 for 開頭之前就有資料（剛建立的 Pod、Deployment 要等滿 for）。
fresh：最新的點要在這段時間內，不然當成對象已經不在了（預設 2m）。
lookback：這條規則讀最近多久的資料（預設用 rules.yaml 的 lookback）。很久才收一次的（主機時鐘 30 分鐘一次）要調長。
abs: true：type value 取絕對值再比（時鐘偏移：快、慢都不對）。
report_gone: true：對象不在了也照報（容器重啟、Job 失敗：Pod 刪掉了，重啟過就是重啟過）。
min_ratio：這段時間裡至少多少比例的點成立（預設 1，全部）。一直重啟的服務每次會短暫起來幾十秒，
           「全部」永遠不成立；設 0.8 表示「3 分鐘裡至少 80% 的時間不夠」。
"""
from __future__ import annotations

import operator

import numpy as np
import pandas as pd

from common.infra.es import get_field
from detect.config.rules import Rule, RulesConfig
from detect.store.models import Anomaly
from detect.base import Detector
from detect.utils import _freq
import detect.series_ops.manual_adjust as A

OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}


def _dur(s) -> pd.Timedelta:
    return pd.Timedelta(str(s).replace("m", "min") if str(s).endswith("m") else str(s)) if s else pd.Timedelta(0)


def sample_value(rule: dict, doc: dict):
    """一個點的值（increase 以外）。欄位不存在就回 None（這個點不算）。"""
    v = get_field(doc, rule["field"])
    if v is None:
        return None
    t = rule["type"]
    if t == "value":
        return abs(float(v)) if rule.get("abs") else float(v)
    if t == "compare":
        other = get_field(doc, rule["other"])
        return None if other is None else float(other) - float(v)
    if t == "ratio":
        parts = [get_field(doc, f) for f in rule["den"]]
        if any(p is None for p in parts):
            return None
        den = sum(float(p) for p in parts)
        return float(v) / den if den else None
    raise ValueError(f"不認得的規則種類：{t}")


def increase(values: list[float]) -> float:
    """累計值增加了多少。數字變小代表歸零重來（例如容器被重建），從 0 開始算。"""
    total = 0.0
    for a, b in zip(values, values[1:]):
        total += b - a if b >= a else b
    return total


def hist_count(h) -> float | None:
    """直方圖在 _source 裡的次數總和。ES 8.19 的 histogram：{"values": [...], "counts": [...]}；
    ES 9 的 exponential_histogram：{"positive": {"counts": [...]}, "zero": {"count": n}, …}。"""
    if not isinstance(h, dict):
        return None
    return float(sum(h.get("counts", [])) + sum((h.get("positive") or {}).get("counts", []))
                 + sum((h.get("negative") or {}).get("counts", [])) + (h.get("zero") or {}).get("count", 0))


def _window(pts, now, hold):
    return [(t, d) for t, d in pts if t >= now - hold] if hold > pd.Timedelta(0) else pts


def slope_per_s(ts: list[pd.Timestamp], vals: list[float]) -> float:
    """Theil–Sen：所有兩點斜率的中位數，GC 鋸齒、單點跳動不會把它拉偏（最小平方會）。每秒漲多少。"""
    t = np.array([(x - ts[0]).total_seconds() for x in ts], dtype=float)
    v = np.array(vals, dtype=float)
    i, j = np.triu_indices(len(t), 1)
    dt = t[j] - t[i]
    ok = dt > 0
    return float(np.median((v[j] - v[i])[ok] / dt[ok])) if ok.any() else 0.0


def lows_rising(vals: list[float], parts: int = 3) -> bool:
    """切成幾段，每段的最低點要一段比一段高：洩漏是 GC 之後回不到原來的低點；純鋸齒（每次 GC 都回到同一個低點）不算。"""
    n = len(vals)
    lows = [min(vals[k * n // parts:(k + 1) * n // parts]) for k in range(parts)]
    return all(b > a for a, b in zip(lows, lows[1:]))


def _slope(rule: dict, key, pts, now: pd.Timestamp, op, th) -> dict | None:
    hold = _dur(rule.get("for", "30m"))
    step = pd.Timedelta(seconds=float(rule.get("step_s", 30)))
    w = [(t, d, float(v)) for t, d in _window(pts, now, hold) if (v := get_field(d, rule["field"])) is not None]
    # 點不夠、最新的太舊（對象不在了）、for 開頭沒資料（新容器還在暖機）：不判斷
    if len(w) < int(rule.get("min_points", 10)) or w[-1][0] < now - _dur(rule.get("fresh", "2m")) or w[0][0] > now - hold + 2 * step:
        return None
    vals = [v for _, _, v in w]
    per_h = slope_per_s([t for t, _, _ in w], vals) * 3600
    if per_h <= 0 or not lows_rising(vals):
        return None
    if rule.get("den"):
        # 上限 = den 加總（例如 working_set + available）；用最後一個兩者都有的點
        limit = None
        for _, d, _ in reversed(w):
            parts = [get_field(d, f) for f in rule["den"]]
            if all(p is not None for p in parts):
                limit = sum(float(p) for p in parts)
                break
        if limit is None:
            return None
        v = max(limit - vals[-1], 0.0) / per_h          # 幾小時後到上限
    else:
        v = per_h
    return {"key": key, "value": v, "since": w[0][0], "last": pts[-1][1], "per_hour": per_h} if op(v, th) else None


def evaluate(rule: dict, series: dict[tuple, list[tuple[pd.Timestamp, dict]]], now: pd.Timestamp) -> list[dict]:
    """series：{序列的 key: [(時間, 文件), …]}（時間由舊到新）。回傳觸發的：{"key", "value", "since", "last"（最新的文件）}。"""
    op, th = OPS[rule.get("op", ">=")], rule["threshold"]
    hold = _dur(rule.get("for", 0))
    out = []
    for key, pts in series.items():
        if rule["type"] == "slope":
            if (h := _slope(rule, key, pts, now, op, th)) is not None:
                out.append(h)
            continue
        if rule["type"] in ("increase_ratio", "rate", "total"):
            w = _window(pts, now, hold)
            if len(w) < 2 or w[-1][0] < now - _dur(rule.get("fresh", "2m")):
                continue
            if rule["type"] == "increase_ratio":
                num = increase([float(v) for _, d in w if (v := get_field(d, rule["field"])) is not None])
                den = increase([float(v) for _, d in w if (v := get_field(d, rule["den"][0])) is not None])
                v = num / den if den else None
            else:
                # 每一筆是那 30 秒的量（直方圖已換成差值）；同一個時間點可能有好幾筆（例如不同的 GC），全部加起來
                pick = (lambda d: hist_count(get_field(d, rule["field"]))) if rule.get("hist_count") \
                    else (lambda d: get_field(d, rule["field"]))
                total = sum(float(v) for _, d in w if (v := pick(d)) is not None)
                secs = max((w[-1][0] - w[0][0]).total_seconds() + float(rule.get("step_s", 30)), 1.0)
                v = total / secs if rule["type"] == "rate" else total
            if v is not None and op(v, th):
                out.append({"key": key, "value": v, "since": w[0][0], "last": w[-1][1]})
            continue
        if rule["type"] in ("increase", "delta"):
            # 對象已經不在了（Pod 刪掉）：最後幾分鐘的累計值還在回溯範圍裡，不算（除非 report_gone）
            if not rule.get("report_gone") and pts and pts[-1][0] < now - _dur(rule.get("fresh", "2m")):
                continue
            vals = [float(v) for _, d in _window(pts, now, hold) if (v := get_field(d, rule["field"])) is not None]
            # delta：量測值（gauge）的淨變化，最後 − 最前；變小就是變小，不當成歸零重來
            if len(vals) >= 2 and op(inc := (vals[-1] - vals[0] if rule["type"] == "delta" else increase(vals)), th):
                out.append({"key": key, "value": inc, "since": pts[0][0], "last": pts[-1][1]})
            continue
        vals = [(t, v) for t, d in pts if (v := sample_value(rule, d)) is not None]
        # 最新的點太舊（Pod 已經刪掉、節點拿掉了）：不是「現在」的狀態，不算
        if not vals or vals[-1][0] < now - _dur(rule.get("fresh", "2m")):
            continue
        if hold == pd.Timedelta(0):
            t, v = vals[-1]
            if op(v, th):
                out.append({"key": key, "value": v, "since": t, "last": pts[-1][1]})
            continue
        recent = [(t, v) for t, v in vals if t >= now - hold]
        # 要有兩個以上的點，最早的點離現在至少 for 的一半（避免剛好只抓到一個點就判斷「一直都是」），
        # 而且這個對象在 for 開頭之前就有資料：剛建立的對象（例如新 Pod 從第一筆就 Pending）要等滿 for，不是一半就報
        ok = sum(op(v, th) for _, v in recent)
        if len(recent) >= 2 and recent[0][0] <= now - hold / 2 and vals[0][0] <= now - hold and op(recent[-1][1], th) \
                and ok >= rule.get("min_ratio", 1.0) * len(recent):
            out.append({"key": key, "value": recent[-1][1], "since": recent[0][0], "last": pts[-1][1]})
    return out


def evaluate_window(rule: dict, df: pd.DataFrame, since: pd.Timestamp) -> list[dict]:
    """type window：df 是 detector.es.fetch（interval=None）的結果，每個對象一列（by + value + n）。
    事件數（請求數）不到 min_events 的不判斷：3 個請求錯 1 個不是「錯誤率 33%」。"""
    op, th, need = OPS[rule.get("op", ">=")], rule["threshold"], rule.get("min_events", 0)
    return [{"key": tuple(r[f] for f in rule["by"]), "value": float(r["value"]), "since": since, "last": {}}
            for r in df.to_dict("records")
            if r["value"] is not None and not pd.isna(r["value"]) and (r["n"] or 0) >= need and op(r["value"], th)]


def describe(rule: dict, hit: dict) -> str:
    """給人看的一句話，例如「容器重啟：imc-wip-prod、crash-app-…、app（1，門檻 >= 1）」。"""
    # 比例用百分比顯示；show: number 的（例如「每核幾條執行緒」）照數字顯示。total 是次數
    pct = (rule["type"] in ("ratio", "increase_ratio", "rate") and rule.get("show") != "number") or rule.get("show") == "pct"
    fmt = (lambda x: f"{x:.0%}") if pct else (lambda x: f"{x:g}")
    if rule.get("show") == "seconds":
        fmt = lambda x: f"{x:.2f} 秒"
    if rule.get("show") == "hours":
        fmt = lambda x: f"{x:.1f} 小時"
    if rule.get("show") == "ns_as_ms":   # 奈秒的數字（主機時鐘偏移）顯示成毫秒
        fmt = lambda x: f"{x / 1e6:,.0f} 毫秒"
    who = "、".join(str(k) for k in hit["key"] if k is not None)
    return f"{rule['title']}：{who}（{fmt(hit['value'])}，門檻 {rule.get('op', '>=')} {fmt(rule['threshold'])}）"


def approved_rules(repo, rules_config: RulesConfig, now: pd.Timestamp | None = None) -> list[dict]:
    """老師傅核准的「升級成規則」（aiops-tuning 的 rule_suggestion，status active、expires_at 還沒到、門檻是數字）：
    跟 rules.yaml 的一起跑。now 是這一輪的時間（--at），過期用它比。"""
    try:
        adjs = A.load_adjustments(repo, rules_config)   # 經模組取，測試才 mock 得到
    except Exception as e:
        print(f"  讀 aiops-tuning 的規則失敗（{e}），只跑 rules.yaml 的")
        return []
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    out = []
    for a in adjs:
        draft = a.get("rule_draft") or {}
        if a.get("kind") != "rule_suggestion" or a.get("status") != "active" or not isinstance(draft, dict) \
                or not isinstance(draft.get("threshold"), (int, float)) or not draft.get("name"):
            continue
        exp = A._ts(a.get("expires_at"))
        if exp is not None and exp <= now:
            continue
        out.append({k: v for k, v in draft.items() if not k.startswith("_")})
    return out


class RulesDetector(Detector):
    level = "rule"

    def checks(self) -> list[Rule | dict]:
        extra = approved_rules(self.repo, self.config, self.now)
        if extra:
            print(f"  從 aiops-tuning 載入 {len(extra)} 條核准的規則：{'、'.join(r['name'] for r in extra)}")
        self.rules = list(self.config.rules) + extra     # rules.yaml 的是 Rule，aiops-tuning 核准的是 dict；下面的函式兩種都吃（rule.get／rule[…]）
        return self.rules

    def evaluate(self, rule: dict) -> list[Anomaly]:
        now = self.now
        start = now - pd.Timedelta(_freq(rule.get("lookback") or self.config.lookback))
        if rule["type"] == "window":   # 整段算一個值（錯誤率、p95）：跟 series.yaml 同一套查法
            hits = evaluate_window(rule, self.repo.series.fetch(rule, start, now, None), start)
        else:
            hits = evaluate(rule, self.repo.series.points(rule, start, now), now)
        out = []
        for h in hits:
            keys = {f: v for f, v in zip(rule["by"], h["key"])}
            # 附帶的資訊（服務名稱、節點），根因分析用來對到服務；不參與「同一個對象」的判斷
            keys.update({f: v for f in rule.get("context", []) if (v := get_field(h["last"], f)) is not None})
            out.append(Anomaly(ts=self.slot, series=f"rule.{rule['name']}", rule=rule["name"], keys=keys, value=h["value"],
                               expected_high=rule["threshold"], level=self.level, direction=rule.get("op", ">="),
                               severity=rule.get("severity", "warning"), message=describe(rule, h), since=h["since"]))
        return out

    def on_error(self, rule: dict, exc: Exception) -> None:
        # 一條規則壞掉（查詢錯、核准的草稿缺欄位）只跳過那一條，其他照跑、照寫
        self.failed.append(rule["name"])
        print(f"  規則 {rule['name']} 失敗，跳過：{type(exc).__name__}: {str(exc)[:300]}")

    def summary(self) -> str:
        return (f"{self.slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{len(self.rules)} 條規則，觸發 {self.fired} 個"
                + (f"，{len(self.failed)} 條失敗：{'、'.join(self.failed)}" if self.failed else ""))


def cmd_rules(rules_config: RulesConfig, args, repo=None) -> int:
    return RulesDetector(rules_config, args, repo).run()
