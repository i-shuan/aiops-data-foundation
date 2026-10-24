"""資料來源的介面：要別人提供的資料（負責人對照、行事曆），程式只認「格式」，不管資料從哪來。

兩個介面（格式見 fake-data/README.md，假資料放 fake-data/，真的來源之後換 provider 就好）：

  owners    負責人對照：{"owners": [{"match": {"service": "^mes-"}, "owner": "MES 團隊"}, …]}
  calendar  行事曆：     {"timezone": "Asia/Taipei", "calendar": [{"date": "2026-10-10", "name": "國慶日", "type": "holiday"}, …]}
                         每一筆的欄位見 baseline/calendar.yaml

每個介面在設定裡寫一個或多個來源（source），provider 決定怎麼取：

  file     讀本機 JSON：path
  http     GET 一個 URL，回傳同樣格式的 JSON：url（可帶 headers、timeout）
  taiwan   （只有 calendar）抓台灣的行事曆 https://github.com/ruyut/TaiwanCalendar ：每年一個 JSON，
           放假的日子（國定假日、補假、彈性放假、春節…）和補班日各變成一筆；週六日不列（baseline 本來就依星期幾學）。
           years 不寫就是今年和明年；明年的還沒公告時（404）略過。抓失敗時用上次的快取（cache_dir），都沒有就空的，
           不讓行事曆壞掉擋住訓練。

多個來源的結果合在一起；某個來源失敗只印警告，不讓整個指令掛掉（行事曆、負責人是輔助資訊）。
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

TAIWAN_URL = "https://cdn.jsdelivr.net/gh/ruyut/TaiwanCalendar/data/{year}.json"


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}", file=sys.stderr)


def _http_json(url: str, headers: dict | None = None, timeout: float = 20):
    import requests
    r = requests.get(url, headers=headers or {}, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _file_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── 台灣行事曆 ──

def parse_taiwan(days: list[dict]) -> list[dict]:
    """TaiwanCalendar 一年的資料 → 行事曆的項目。

    原始：{"date": "20260101", "week": "四", "isHoliday": true, "description": "開國紀念日"}
      isHoliday 且有 description → 放假（holiday）
      isHoliday 沒有 description → 一般週六日，略過
      不是 isHoliday 但有 description → 補班日（makeup_workday）：週六上班，用「週六」的 baseline 比會誤報，所以也不學、不判斷
    """
    out = []
    for d in days:
        desc = (d.get("description") or "").strip()
        if not desc:
            continue
        date = str(d["date"])
        iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        out.append({"date": iso, "name": desc, "type": "holiday" if d.get("isHoliday") else "makeup_workday"})
    return out


def taiwan_calendar(years: list[int], *, url: str = TAIWAN_URL, cache_dir: str | None = ".cache/calendar",
                    fetch=_http_json) -> list[dict]:
    """抓指定年份。fetch(url) 回 None 表示那一年還沒有（404）；丟例外表示抓失敗（用快取）。"""
    out = []
    for y in years:
        cache = os.path.join(cache_dir, f"taiwan-{y}.json") if cache_dir else None
        try:
            data = fetch(url.format(year=y))
        except Exception as e:   # 網路、DNS、5xx
            data = None
            if cache and os.path.exists(cache):
                _warn(f"台灣行事曆 {y} 抓不到（{type(e).__name__}），用快取 {cache}")
                data = _file_json(cache)
            else:
                _warn(f"台灣行事曆 {y} 抓不到（{type(e).__name__}），也沒有快取：這一年沒有行事曆")
            out += parse_taiwan(data or [])
            continue
        if data is None:
            _warn(f"台灣行事曆 {y} 還沒公告（404），略過")
            continue
        if cache:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        out += parse_taiwan(data)
    return out


# ── 介面 ──

def _years(src: dict, now: pd.Timestamp | None = None) -> list[int]:
    if src.get("years"):
        return [int(y) for y in src["years"]]
    y = (now or pd.Timestamp.now(tz="UTC")).year
    return [y, y + 1]


def load_calendar(cfg: dict, *, fetch=_http_json, now: pd.Timestamp | None = None) -> tuple[list[dict], str | None]:
    """cfg：calendar.yaml 的內容 {timezone, sources: [...], calendar: [...]}。回傳（項目、時區）。
    calendar 直接寫的項目（人工補的維護日、活動）一起算；sources 沒寫也行。"""
    entries = list(cfg.get("calendar") or [])
    for src in cfg.get("sources") or []:
        if src.get("enabled") is False:
            continue
        p = src["provider"]
        try:
            if p == "file":
                data = _file_json(src["path"])
                entries += data.get("calendar", []) if isinstance(data, dict) else data
            elif p == "http":
                data = fetch(src["url"], src.get("headers"), src.get("timeout", 20))
                entries += (data or {}).get("calendar", []) if isinstance(data, dict) else (data or [])
            elif p == "taiwan":
                entries += taiwan_calendar(_years(src, now), url=src.get("url", TAIWAN_URL),
                                           cache_dir=src.get("cache_dir", ".cache/calendar"), fetch=fetch)
            else:
                raise ValueError(f"不認得的 provider：{p}")
        except Exception as e:
            _warn(f"行事曆來源 {p} 失敗：{type(e).__name__}: {e}")
    return entries, cfg.get("timezone")


def load_owners(cfg: dict, *, fetch=_http_json) -> list[dict]:
    """cfg：chronic.yaml 的內容。owners（直接寫的）加 owners_source（一個或多個來源）。先寫的優先（owner_of 取第一個符合的）。"""
    out = list(cfg.get("owners") or [])
    srcs = cfg.get("owners_source") or []
    for src in [srcs] if isinstance(srcs, dict) else srcs:
        p = src["provider"]
        try:
            if p == "file":
                data = _file_json(src["path"])
            elif p == "http":
                data = fetch(src["url"], src.get("headers"), src.get("timeout", 20))
            else:
                raise ValueError(f"不認得的 provider：{p}")
            out += (data or {}).get("owners", []) if isinstance(data, dict) else (data or [])
        except Exception as e:
            _warn(f"負責人來源 {p} 失敗：{type(e).__name__}: {e}")
    return out
