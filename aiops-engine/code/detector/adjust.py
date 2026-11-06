"""調整：哪些資料不學、從哪裡重新學、哪些時段放寬（configs/baseline/reference.md「定義的原則與限制」「調整」）。

統計公式（168 格中位數＋MAD）不動；baseline 學錯、報錯的情況，用「調整資料」修正。四種 kind：

  override            某條序列、某些對象固定粒度、改 z、min_abs（tune 指令寫的；舊資料沒有 kind 就是這種）
  exclude_window      start～end 這段時間的資料不學（事故、維護、壓測）；apply_to 加 detect 時這段也不報
  reset_point         reset_at 之前的歷史不學（換版、架構調整）；之後 warmup（預設 24 小時）內不判斷，
                      之後 baseline 是重置後重新學的才判斷（新歷史不夠就沒有 baseline，也不判斷）
  recurring_override  符合 schedule 的時段（例如每月最後一天 20:00 起 4 小時）放寬：z、min_abs 調高，或 mute 不報

來源（後面蓋前面，同 id 以後面的為準）：
  (1) 設定檔：series.yaml 的 overrides、adjustments_file（預設 configs/baseline/adjustments.yaml）、
     calendar_file（預設 configs/baseline/calendar.yaml：國定假日、維護日、大型活動，變成不學、不判斷的 exclude_window；
     來源可以是檔案、URL、台灣行事曆，見 sources.py）── 人寫、進版控
  (2) ES 的 aiops-tuning ── tune、adjust 指令寫的；之後 learning loop（值班回饋、CI/CD）也寫這裡

learning loop 的接口：status: proposed 的不生效，人用 `adjust approve` 改成 active 才生效；source 記誰寫的
（manual／feedback／cicd）；expires_at 到期自動失效。

match 的欄位可以用別名（ALIASES）：寫 service 就同時對到 HTTP 序列的 service、log 與
metrics 的 resource.attributes.service.name、trace 的 entry.service。

這裡只做計算（單元測試用）；讀 ES 的是 load。
"""
from __future__ import annotations

import calendar
import re

import numpy as np
import pandas as pd

KINDS = ("override", "exclude_window", "reset_point", "recurring_override")
DEFAULT_WARMUP = "24h"   # reset_point 之後多久內不判斷

# 同一個概念在不同序列的欄位名稱。match 寫其中任何一個，都會找序列裡有的那一個
# log、metrics、trace 的鑰匙都是 OTel 路徑（log 的是 alias；Java 的 HTTP 指標由 Collector 改名），所以只剩「短名字 ↔ 路徑」
ALIASES = [
    ("service", "resource.attributes.service.name", "entry.service"),
    ("namespace", "k8s.namespace.name", "resource.attributes.k8s.namespace.name"),
    ("pod", "k8s.pod.name", "resource.attributes.k8s.pod.name"),
    ("deployment", "k8s.deployment.name", "resource.attributes.k8s.deployment.name"),
    ("route", "http.route", "attributes.http.route"),
    ("method", "http.request.method", "attributes.http.request.method"),
]
_ALIAS_OF = {name: group for group in ALIASES for name in group}


def key_value(key: dict, field: str):
    """key 裡 field（或它的別名）的值；都沒有就是 None。"""
    if field in key:
        return key[field]
    for alias in _ALIAS_OF.get(field, ()):
        if alias in key:
            return key[alias]
    return None


def match_keys(key: dict, match: dict | None) -> bool:
    """match 的每個欄位都要符合（正規表示式，re.search；欄位可以用別名）；沒有 match 就是全部。"""
    for f, pat in (match or {}).items():
        v = key_value(key, f)
        if v is None or not re.search(str(pat), str(v)):
            return False
    return True


def kind_of(a: dict) -> str:
    return a.get("kind") or "override"


def _ts(x) -> pd.Timestamp | None:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t


TIME_FIELDS = ("start", "end", "reset_at", "expires_at")


def local_iso(x, tz: str = "Asia/Taipei") -> str | None:
    """人寫的時間 → ISO。沒寫時區的（2026-10-20T02:00）當成 tz 的當地時間，跟行事曆一樣；
    不轉的話 ES 和 pandas 都當成 UTC，台北的值班寫「凌晨 2 點」會差 8 小時。"""
    if x is None:
        return None
    t = pd.Timestamp(str(x))
    return (t.tz_localize(tz) if t.tzinfo is None else t).isoformat()


def localize(a: dict, tz: str = "Asia/Taipei") -> dict:
    """一筆調整裡沒寫時區的時間欄位補上當地時區（設定檔 adjustments.yaml 用）。"""
    return {**a, **{f: local_iso(a[f], tz) for f in TIME_FIELDS if a.get(f) is not None}}


def _td(s) -> pd.Timedelta:
    s = str(s)
    if s.endswith("m"):
        s = s[:-1] + "min"
    elif s.endswith("d"):
        s = s[:-1] + "D"
    return pd.Timedelta(s)


def active(adjs: list[dict], now: pd.Timestamp, kinds=KINDS) -> list[dict]:
    """生效中的：kind 符合、status 是 active（沒寫就是 active）、還沒過期。"""
    return [a for a in adjs if kind_of(a) in kinds and (a.get("status") or "active") == "active"
            and (_ts(a.get("expires_at")) is None or _ts(a.get("expires_at")) > now)]


def applies(a: dict, series: str) -> bool:
    return re.fullmatch(a.get("series") or ".*", series) is not None


def _key_mask(df: pd.DataFrame, keys: list[str], match: dict | None) -> np.ndarray:
    """df 每一列的對象符不符合 match（同一組 keys 只算一次）。"""
    if not match:
        return np.ones(len(df), dtype=bool)
    if df.empty:
        return np.zeros(0, dtype=bool)
    ks = df[keys].drop_duplicates()
    ks = ks.assign(_hit=[match_keys(k, match) for k in ks.to_dict("records")])
    return df[keys].merge(ks, on=keys, how="left")["_hit"].fillna(False).to_numpy(dtype=bool)


# ── 週期：cron 的開始時間＋duration ──

def _expand(field: str, lo: int, hi: int) -> list[int]:
    out = set()
    for part in field.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-"))
        else:
            a = b = int(rng)
        out.update(range(a, b + 1, int(step) if step else 1))
    return sorted(out)


def _day_matches(day: pd.Timestamp, f: list[str], days_of_month) -> bool:
    if day.month not in _expand(f[3], 1, 12):
        return False
    dom_ok = day.day in _expand(f[2], 1, 31)
    dow_ok = (day.day_of_week + 1) % 7 in {d % 7 for d in _expand(f[4], 0, 7)}   # cron：0、7 都是週日
    # cron 的規則：日期、星期兩個都有限制時，符合其中一個就算；只有一個有限制時看那一個
    if f[2] != "*" and f[4] != "*":
        ok = dom_ok or dow_ok
    else:
        ok = dom_ok and dow_ok
    if ok and days_of_month:
        last = calendar.monthrange(day.year, day.month)[1]
        ok = any(day.day == (d if d > 0 else last + 1 + d) for d in days_of_month)
    return ok


def in_schedule(ts: pd.Series, schedule: dict, tz: str = "Asia/Taipei") -> np.ndarray:
    """每個時間點是不是在週期時段裡。schedule：cron（開始時間，5 欄，tz 當地時間）、duration、
    days_of_month（選填，例如 [-1] 是每月最後一天；cron 寫不出「最後一天」）。"""
    if len(ts) == 0:
        return np.zeros(0, dtype=bool)
    f = str(schedule["cron"]).split()
    if len(f) != 5:
        raise ValueError(f"cron 要 5 欄：{schedule['cron']}")
    dur = _td(schedule.get("duration", "1h"))
    local = pd.to_datetime(ts, utc=True).dt.tz_convert(tz)
    days = pd.date_range(local.min().normalize() - pd.Timedelta(days=dur.days + 1), local.max().normalize(), freq="D")
    hm = [(h, m) for h in _expand(f[1], 0, 23) for m in _expand(f[0], 0, 59)]
    starts = [d + pd.Timedelta(hours=h, minutes=m) for d in days if _day_matches(d, f, schedule.get("days_of_month"))
              for h, m in hm]
    if not starts:
        return np.zeros(len(ts), dtype=bool)
    s = np.array(sorted(x.value for x in starts), dtype=np.int64)
    t = local.map(lambda x: x.value).to_numpy(dtype=np.int64)
    i = np.searchsorted(s, t, side="right") - 1     # 開始時間 ≤ t 的最後一個（duration 都一樣，它的結束最晚）
    return (i >= 0) & (t < s[np.clip(i, 0, None)] + dur.value)


# ── train：哪些點不學 ──

def train_filter(hist: pd.DataFrame, keys: list[str], series: str, adjs: list[dict], tz: str = "Asia/Taipei"
                 ) -> tuple[pd.DataFrame, int]:
    """hist：keys + ts + value…。拿掉 exclude_window（apply_to 含 train）、reset_point 之前、
    recurring_override（effect.train: exclude）的點。回傳（剩下的、拿掉幾個點）。"""
    if hist.empty:
        return hist, 0
    drop = np.zeros(len(hist), dtype=bool)
    ts = pd.to_datetime(hist["ts"], utc=True)
    for a in adjs:
        if not applies(a, series):
            continue
        k = kind_of(a)
        if k == "exclude_window" and "train" in (a.get("apply_to") or ["train"]):
            t = ((ts >= _ts(a["start"])) & (ts < _ts(a["end"]))).to_numpy()
        elif k == "reset_point":
            t = (ts < _ts(a["reset_at"])).to_numpy()
        elif k == "recurring_override" and (a.get("effect") or {}).get("train") == "exclude":
            t = in_schedule(ts, a["schedule"], tz)
        else:
            continue
        if t.any():
            drop |= t & _key_mask(hist, keys, a.get("match"))
    # 只數有值的點（服務上線前「不知道」的列本來就不學，不算排除）
    real = hist["value"].notna().to_numpy() if "value" in hist else np.ones(len(hist), dtype=bool)
    return hist[~drop], int((drop & real).sum())


# ── detect：哪些異常不算 ──

def detect_filter(scored: pd.DataFrame, keys: list[str], series: str, adjs: list[dict], tz: str = "Asia/Taipei",
                  baseline: pd.DataFrame | None = None) -> tuple[pd.DataFrame, int]:
    """scored：score／score_poisson 的結果。把下面這些點的 anomaly 改成 False（只會變少，不會多）：
      exclude_window（apply_to 含 detect）時段內
      recurring_override 時段內：mute；或 |z| < effect.z；或 |現在 − 平常| < effect.min_abs
      reset_point 之後 warmup 內；或之後、baseline 還是重置前學的（trained_at 早於 reset_at 或這筆重置寫進來的時間）：舊版的 baseline 不能拿來比新版
    回傳（結果、壓掉幾個）。"""
    if scored.empty or not scored["anomaly"].any():
        return scored, 0
    out = scored.copy()
    hit = out["anomaly"].to_numpy(dtype=bool)
    keep = hit.copy()
    ts = pd.to_datetime(out["ts"], utc=True)
    for a in adjs:
        if not applies(a, series):
            continue
        k = kind_of(a)
        if k == "exclude_window" and "detect" in (a.get("apply_to") or []):
            t = ((ts >= _ts(a["start"])) & (ts < _ts(a["end"]))).to_numpy()
            keep &= ~(t & _key_mask(out, keys, a.get("match")))
        elif k == "recurring_override":
            e = a.get("effect") or {}
            t = in_schedule(ts, a["schedule"], tz) & _key_mask(out, keys, a.get("match"))
            if e.get("mute"):
                keep &= ~t
                continue
            ok = np.ones(len(out), dtype=bool)
            if e.get("z") is not None and "z" in out:
                z = out["z"].to_numpy(dtype=float)
                ok &= np.isnan(z) | (np.abs(z) >= e["z"])
            if e.get("min_abs") is not None:
                ok &= np.abs(out["value"].to_numpy(dtype=float) - out["median"].to_numpy(dtype=float)) >= e["min_abs"]
            keep &= ~(t & ~ok)
        elif k == "reset_point":
            reset = _ts(a["reset_at"])
            km = _key_mask(out, keys, a.get("match"))
            # 暖機：重置後 warmup（預設 24 小時）內不判斷。新歷史太短（30 秒一點 25 分鐘就有 50 點）學出來的不可信
            warm = (ts >= reset) & (ts < reset + _td(a.get("warmup") or DEFAULT_WARMUP))
            keep &= ~(warm.to_numpy() & km)
            if baseline is None or baseline.empty:
                continue
            # baseline 要在「這筆重置寫進來之後」重新 train 過，才是用重置後的歷史學的。
            # 事後補標（reset_at 在過去）時，reset_at 之後、寫進來之前 train 的 baseline 還是舊的
            cutoff = max(reset, _ts(a.get("at")) or reset)
            b = baseline[keys + ["trained_at"]].copy()
            b["trained_at"] = pd.to_datetime(b["trained_at"], utc=True)
            stale = b[b["trained_at"].isna() | (b["trained_at"] < cutoff)][keys].drop_duplicates()
            if stale.empty:
                continue
            is_stale = out[keys].merge(stale.assign(_s=True), on=keys, how="left")["_s"].fillna(False).to_numpy(dtype=bool)
            keep &= ~(is_stale & (ts >= reset).to_numpy() & km)
    out["anomaly"] = keep
    return out, int((hit & ~keep).sum())


# ── 行事曆：國定假日、維護日、大型活動 → 不學、不判斷 ──

def _local(x, tz: str, end: bool = False) -> pd.Timestamp:
    """日期（2026-10-10）當成當地的一整天：開始是 00:00，end=True 時是隔天 00:00；有時間的（2026-11-01T02:00）照原樣。"""
    s = str(x)
    t = pd.Timestamp(s)
    if t.tzinfo is None:
        t = t.tz_localize(tz)
    if "T" not in s and " " not in s.strip():
        t = t.normalize() + (pd.Timedelta(days=1) if end else pd.Timedelta(0))
    return t.tz_convert("UTC")


def calendar_adjustments(entries: list[dict], tz: str = "Asia/Taipei") -> list[dict]:
    """行事曆的每一天（或一段）變成 exclude_window：預設不學也不判斷（detect: false 時只是不學）。

    entries：[{date: 2026-10-10, name: 國慶日, type: holiday}, {start: 2026-11-01T02:00, end: 2026-11-01T06:00,
    name: 機房維護, type: maintenance, match: {...}}, …]。沒有 match 就是全部。
    """
    out = []
    for e in entries or []:
        start = e.get("start", e.get("date"))
        end = e.get("end", e.get("date"))
        if start is None or end is None:
            raise ValueError(f"行事曆要有 date，或 start＋end：{e}")
        out.append({"id": f"calendar:{e.get('type', 'holiday')}:{start}:{e.get('name', '')}", "kind": "exclude_window",
                    "series": e.get("series"), "match": e.get("match"),
                    "start": _local(start, tz).isoformat(), "end": _local(end, tz, end=True).isoformat(),
                    "apply_to": ["train", "detect"] if e.get("detect", False) is False else ["train"],
                    "source": "calendar", "reason": f"{e.get('type', 'holiday')}：{e.get('name', '')}"})
    return out


# ── 讀 ──

def load(es, index: str, cfg: dict) -> list[dict]:
    """設定檔（adjustments_file、series.yaml 的 overrides）＋ Store 的 aiops-tuning（index 固定是它，參數留給既有呼叫），全部 kind。
    同 id 後面蓋前面。"""
    import os

    from core.config import load_config
    out = list(cfg.get("overrides") or [])
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    path = cfg.get("adjustments_file", "configs/baseline/adjustments.yaml")
    if path and os.path.exists(path):
        out += [localize(a, tz) for a in load_config(path, "adjustments").get("adjustments") or []]
    cal = cfg.get("calendar_file", "configs/baseline/calendar.yaml")
    if cal and os.path.exists(cal):
        from .sources import load_calendar
        entries, tz = load_calendar(load_config(cal, "calendar"))
        out += calendar_adjustments(entries, tz or cfg.get("train", {}).get("timezone", "Asia/Taipei"))
    from sources.base import as_backend
    out += as_backend(es).adjustments()   # ES 讀不到時 Store 印一行、回 []：這一輪只用設定檔的
    by_id = {}
    for a in out:
        by_id[a.get("id") or id(a)] = a
    good = []
    for a in by_id.values():
        why = problem(a)
        if why:
            print(f"  ⚠ 略過調整 {a.get('id') or '（設定檔）'}：{why}")
        else:
            good.append(a)
    return good


def problem(a: dict) -> str | None:
    """這筆調整能不能用；不能用回傳原因。learning loop、人手寫進 aiops-tuning 的壞資料（缺欄位、正規表示式錯）
    只略過那一筆，不讓整個 train／detect 失敗。"""
    try:
        re.compile(a.get("series") or ".*")
        for pat in (a.get("match") or {}).values():
            re.compile(str(pat))
        if a.get("expires_at") is not None:
            _ts(a["expires_at"])
        k = kind_of(a)
        if k == "exclude_window":
            if _ts(a.get("start")) is None or _ts(a.get("end")) is None:
                return "exclude_window 要有 start、end"
            if _ts(a["start"]) >= _ts(a["end"]):
                return "exclude_window 的 start 要早於 end"
        elif k == "reset_point":
            if _ts(a.get("reset_at")) is None:
                return "reset_point 要有 reset_at"
            _td(a.get("warmup") or DEFAULT_WARMUP)
        elif k == "recurring_override":
            sched = a.get("schedule") or {}
            if len(str(sched.get("cron", "")).split()) != 5:
                return f"cron 要 5 欄：{sched.get('cron')}"
            _td(sched.get("duration", "1h"))
        elif k == "override":
            for f in ("z", "min_abs", "instant_z"):
                if a.get(f) is not None:
                    float(a[f])
            if a.get("confirm_points") is not None and int(a["confirm_points"]) < 1:
                return "confirm_points 至少 1"
            if a.get("resolution") is not None and a["resolution"] not in ("30s", "1m", "5m", "15m", "30m", "1h"):
                return f"不認得的粒度：{a['resolution']}"
    except (re.error, ValueError, TypeError) as e:
        return f"{type(e).__name__}：{e}"
    return None


def describe(a: dict) -> str:
    k = kind_of(a)
    who = ", ".join(f"{f}={v}" for f, v in (a.get("match") or {}).items()) or "全部"
    what = {"exclude_window": lambda: f"{a.get('start')} ～ {a.get('end')} 不學"
                                      + ("、不報" if "detect" in (a.get("apply_to") or []) else ""),
            "reset_point": lambda: f"{a.get('reset_at')} 之前不學，之後 {a.get('warmup') or DEFAULT_WARMUP} 不判斷",
            "recurring_override": lambda: f"{a.get('schedule')} → {a.get('effect')}",
            "override": lambda: ", ".join(f"{f}={a[f]}" for f in ("resolution", "z", "min_abs", "confirm_points", "instant_z", "min_count") if a.get(f) is not None),
            "rule_suggestion": lambda: f"升級成規則：{a.get('service')} / {a.get('root_cause_category')}（{len(a.get('incident_ids') or [])} 件）"}[k]()
    return (f"[{a.get('status') or 'active'}] {k} {a.get('series') or '.*'} {who}：{what}"
            f"（{a.get('source') or 'manual'}；{a.get('reason') or ''}）")


# ── 從 aiops-tuning 讀生效中的調整（以前在 detector/tuning.py；寫入在 feedback/）──

def load_adjustments(es, cfg: dict) -> list[dict]:
    """全部調整（四種 kind）：設定檔（overrides、adjustments_file）＋ aiops-tuning（tune、adjust 指令、learning loop 提的）。"""
    from core.tuning import TUNING_INDEX
    return load(es, TUNING_INDEX, cfg)


def load_overrides(es, cfg: dict, now=None) -> list[dict]:
    """粒度、z、min_abs、confirm_points、instant_z 的調整（kind: override；舊資料沒有 kind 也是），生效中的，後面蓋前面。"""
    return active(load_adjustments(es, cfg), now if now is not None else pd.Timestamp.now(tz="UTC"), kinds=("override",))


def data_adjustments(es, cfg: dict, now) -> list[dict]:
    """排除時段、重置點、週期放寬，生效中的。"""
    return active(load_adjustments(es, cfg), now, kinds=("exclude_window", "reset_point", "recurring_override"))
