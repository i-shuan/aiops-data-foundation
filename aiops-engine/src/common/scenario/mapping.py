"""欄位對照的覆寫（aiops-mapping）：人在網頁上提、核准後蓋在情境資料檔（it_otel.yaml、it_apm.yaml）的 owner、log 上。

yaml 是基準（在 git、image 裡）；ES 裡核准過（status active）的覆寫在**每次讀**時蓋上去，所以改了不用重新部署、下一輪工作就生效，
engine 換 image 也不會洗掉（ES 是另一個服務）。只開放「設定寫得出來」的欄位（EDITABLE）；來源適配器裡用程式寫的轉換不在這裡改。
讀不到 ES、覆寫寫錯：那一筆略過、印一行，照 yaml 的值跑——對照覆寫不能讓偵測停下來。
計畫與為什麼存 ES 不存容器裡的檔或內建資料庫：docs/pdca/meta/2026-11-mapping-ui.md。
"""
from __future__ import annotations

import dataclasses
import time

from common.infra.es import Repository

MAPPING_INDEX = "aiops-mapping"
MAPPING_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "source": {"type": "keyword"}, "section": {"type": "keyword"}, "field": {"type": "keyword"},
    "value": {"type": "keyword"}, "old_value": {"type": "keyword"}, "status": {"type": "keyword"},
    "by": {"type": "keyword"}, "reason": {"type": "text"}, "created_at": {"type": "date"},
    "approved_by": {"type": "keyword"}, "approved_at": {"type": "date"}, "check": {"type": "text", "index": False}}}}

# 開放在網頁上改的欄位（第一版：歸屬與 log）。值是字串；LIST 的是字串清單
EDITABLE: dict[str, tuple[str, ...]] = {
    "owner": ("index", "namespace", "service", "dept", "group"),
    "log": ("index", "service", "level", "trace_id", "message", "problem_levels"),
}
LIST_FIELDS = {("log", "message"), ("log", "problem_levels")}
STATUSES = ("proposed", "active", "rejected", "disabled")
CACHE_SECONDS = 60          # 常駐的程序（api）多久重讀一次；排程的工作每輪都是新程序，本來就每輪讀


def check_value(section: str, field: str, value) -> str | list[str]:
    """驗一個覆寫的值：欄位要開放、值不能空；清單欄位收清單或逗號分隔的字串。回整理過的值，不對丟 ValueError。"""
    if section not in EDITABLE or field not in EDITABLE[section]:
        allowed = "；".join(f"{s}：{'、'.join(f)}" for s, f in EDITABLE.items())
        raise ValueError(f"{section}.{field} 不能在這裡改（能改的：{allowed}）")
    if (section, field) in LIST_FIELDS:
        items = value if isinstance(value, list) else [v for v in str(value or "").split(",")]
        items = [str(v).strip() for v in items if str(v).strip()]
        if not items:
            raise ValueError(f"{section}.{field} 不能是空的")
        return items
    v = str(value or "").strip()
    if not v or isinstance(value, (list, dict)):
        raise ValueError(f"{section}.{field} 要是一個非空的欄位名")
    return v


def profiles(scenario) -> dict[str, dict[str, dict]]:
    """{來源名: {"owner": …, "log": …}}：主要來源與並存時的其他來源各一份（照 Scenario.sources 的順序）。"""
    names = list(scenario.sources) or [scenario.source]
    owners, logs = scenario.owners, scenario.logs
    return {n: {"owner": owners[i] if i < len(owners) else {}, "log": logs[i] if i < len(logs) else {}} for i, n in enumerate(names)}


def apply(scenario, overrides: list[dict]):
    """把生效的覆寫蓋到情境上，回新的 Scenario（原本的不動）。不認得的來源、寫錯的值：略過那一筆並印一行。"""
    prof = {n: {k: dict(v) for k, v in p.items()} for n, p in profiles(scenario).items()}
    for o in overrides:
        try:
            value = check_value(o.get("section", ""), o.get("field", ""), o.get("value"))
            if o.get("source") not in prof:
                raise ValueError(f"來源 {o.get('source')!r} 沒有在用（AIOPS_SOURCE 是 {','.join(prof)}）")
            prof[o["source"]][o["section"]][o["field"]] = value
        except ValueError as e:
            print(f"  ⚠ 欄位對照覆寫 {o.get('id', '?')} 略過：{e}")
    names = list(prof)
    return dataclasses.replace(scenario, owner=prof[names[0]]["owner"], log=prof[names[0]]["log"],
                               extra_owners=tuple(prof[n]["owner"] for n in names[1:]) if scenario.sources else scenario.extra_owners,
                               extra_logs=tuple(prof[n]["log"] for n in names[1:]) if scenario.sources else scenario.extra_logs)


class MappingRepository(Repository):
    INDEX, MAPPING = MAPPING_INDEX, MAPPING_MAPPING

    def all(self) -> list[dict]:
        """全部（帶 id），新的在前。索引還不存在（沒人提過）是正常的，回 []。"""
        res = self.es.req("POST", f"/{self.INDEX}/_search", {"size": 1000, "sort": [{"created_at": {"order": "desc", "unmapped_type": "date"}}]},
                          ok=(200, 404))
        return [{"id": h["_id"], **h["_source"]} for h in res.get("hits", {}).get("hits", [])]

    def active(self) -> list[dict]:
        """生效的覆寫，舊的在前（同一個欄位有兩筆生效時，後核准的蓋過先的；核准時其實會把舊的停用）。"""
        return sorted([d for d in self.all() if d.get("status") == "active"], key=lambda d: d.get("approved_at") or "")

    def get(self, _id: str) -> dict | None:
        got = self._get(_id)
        return {"id": _id, **got} if got else None

    def put(self, _id: str, doc: dict) -> None:
        if doc.get("status") not in STATUSES:
            raise ValueError(f"status 只能是 {'、'.join(STATUSES)}")
        self.ensure()
        self._put(_id, doc)

    def patch(self, _id: str, doc: dict) -> None:
        self._patch(_id, doc)


_cache: dict[int, tuple[float, list[dict]]] = {}


def effective(scenario, es):
    """這一輪要用的情境：yaml 的 owner、log 蓋上 ES 裡生效的覆寫。讀不到就照 yaml（印一行）。常駐的程序 CACHE_SECONDS 內重用。"""
    if scenario is None or es is None or not hasattr(es, "req"):
        return scenario
    now = time.monotonic()
    hit = _cache.get(id(es))
    if hit and now - hit[0] < CACHE_SECONDS:
        overrides = hit[1]
    else:
        try:
            overrides = MappingRepository(es).active()
        except Exception as e:  # noqa: BLE001  ES 回錯、連不上、假的 ES：照 yaml 跑
            print(f"  ⚠ 讀不到欄位對照的覆寫（{type(e).__name__}: {str(e)[:100]}），這一輪照設定檔的")
            overrides = []
        _cache[id(es)] = (now, overrides)
    return apply(scenario, overrides) if overrides else scenario
