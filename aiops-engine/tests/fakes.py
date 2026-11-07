"""測試用、不連 ES 的 Repos（跟 common.infra.repos.Repos 同樣的屬性與方法）：
原始資料是 {index pattern: DataFrame}（整形在 tests/shaping.py，語意跟 ES 端的 aggregation 一樣），aiops-* 的文件存在 dict。

    repo = MemoryRepos({"metrics-apm.transaction.1m-*": df})
    cmd_train(cfg, args, repo); cmd_detect(cfg, args, repo)
    repo.docs("aiops-anomalies")   # [(id, 文件)]
"""
from __future__ import annotations

import fnmatch
import json

import pandas as pd

from common.infra.env import ENGINE_VERSION
from incident.store.models import ALERT_INDEX, Alert
from detect.store.models import ANOMALY_INDEX
from detect.store.models import BASELINE_COLUMNS, BASELINE_INDEX, GROUP_COLUMNS, RENAMED
from learn.store.models import FEEDBACK_INDEX
from common.types.incident import INCIDENT_INDEX
from learn.store.models import TUNING_INDEX, validated
from detect.store.segment_repo import SEGMENT_INDEX
from detect.store.state_repo import STATE_INDEX
from common.scenario import load_scenario
import tests.shaping as shape


def pick(mapping: dict, index: str, what: str) -> list:
    """mapping 的 key 是 index pattern；index 可以是逗號分隔的好幾個。完全一樣優先，再用 glob 比對。"""
    out = []
    for one in str(index).split(","):
        one = one.strip()
        if one in mapping:
            out.append(mapping[one])
            continue
        hit = [v for k, v in mapping.items() if fnmatch.fnmatchcase(one, k) or fnmatch.fnmatchcase(k, one)]
        if not hit:
            raise KeyError(f"{what} 沒有 index {one!r} 的資料（有：{list(mapping)}）")
        out += hit
    return out


class _Docs:
    """一個索引的文件（dict）。"""
    INDEX = ""

    def __init__(self, data: dict):
        self.data = data

    def ensure(self, *a, **k) -> None:
        self.data.setdefault(self.INDEX, {})

    def add_fields(self, *a, **k) -> None:
        pass

    def bulk(self, docs, index: str | None = None) -> None:
        d = self.data.setdefault(index or self.INDEX, {})
        for _id, doc in docs:
            d[_id] = json.loads(json.dumps(doc, default=str))   # 跟 ES 一樣：存的是 JSON，Timestamp 變字串

    def all(self) -> list[tuple[str, dict]]:
        return list(self.data.get(self.INDEX, {}).items())

    def get(self, _id: str) -> dict | None:
        return self.data.get(self.INDEX, {}).get(_id)

    def put(self, _id: str, doc: dict) -> None:
        self.bulk([(_id, doc)])

    def patch(self, _id: str, doc: dict, upsert: dict | None = None) -> None:
        d = self.data.setdefault(self.INDEX, {})
        d[_id] = {**d.get(_id, upsert or {}), **doc}

    def delete(self, _id: str) -> None:
        self.data.get(self.INDEX, {}).pop(_id, None)


def _in(ts, start, end) -> bool:
    t = pd.Timestamp(ts)
    return (start is None or t >= start) and (end is None or t < end)


def _plain(model) -> dict:
    """設定模型 → shaping.py 讀的 dict（沒寫的選填欄位不在裡面）。"""
    return model.model_dump(exclude_none=True, by_alias=True)


class MemorySeries:
    def __init__(self, frames: dict[str, pd.DataFrame] | None, fmt: str = "events"):
        self.frames, self.fmt = dict(frames or {}), fmt

    def _rows(self, index: str) -> pd.DataFrame:
        parts = pick(self.frames, index, "memory 來源")
        return parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)

    def fetch(self, spec, start, end, interval):
        return shape.series_from_rows(self._rows(spec.index), _plain(spec), start, end, interval, self.fmt)

    def alive_times(self, spec, start, end, interval):
        rows = self._rows(spec.index)
        tf = "ts" if self.fmt == "series" and "ts" in rows.columns else (getattr(spec, "time_field", None) or "@timestamp")
        return shape.alive_from_rows(rows, tf, start, end, interval)

    def points(self, rule, start, end):
        return shape.points_from_rows(self._rows(rule.index), _plain(rule), start, end)

    def rows(self, inp, start, end, time_field=None):
        df = shape.in_window(self._rows(inp.index), time_field or getattr(inp, "time_field", None) or "@timestamp", start, end)
        if df.empty:
            return []
        if inp.filter:
            df = df.loc[shape.mask(df, inp.filter)]
        rows = df.drop(columns=["ts"]).to_dict("records")
        return [{k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in r.items()} for r in rows]

    def rows_where(self, inp, field, values, time_field=None):
        rows = self._rows(inp.index).to_dict("records")
        return [r for r in rows if str(r.get(field)) in set(values)]


class MemoryAnomalies(_Docs):
    INDEX = ANOMALY_INDEX

    def write(self, docs) -> None:
        """記憶體來源沒有歸屬可查：全部 unassigned；engine_version 跟 ES 版一樣每筆都補。"""
        for _, d in docs:
            d["engine_version"] = ENGINE_VERSION
            d.setdefault("dept", None)
            d.setdefault("group", "unassigned")
        self.bulk(docs)

    def between(self, start, end, fields=None, **kw) -> list[dict]:
        return [{**s, "_id": i} for i, s in self.all() if _in(s["@timestamp"], start, end)]

    def get_many(self, ids, fields=None) -> dict[str, dict]:
        return {i: s for i, s in self.all() if i in set(ids)}

    def status_of(self, ids) -> dict[str, dict]:
        return {i: {"status": s.get("status"), "streak": s.get("streak")} for i, s in self.get_many(ids).items() if s.get("status") == "anomaly"}

    def tag_alert(self, tags) -> int:
        for _id, aid in tags:
            if _id in self.data.get(self.INDEX, {}):
                self.data[self.INDEX][_id]["alert_id"] = aid
        return len(tags)


class MemoryBaseline(_Docs):
    INDEX = BASELINE_INDEX

    def load(self, series, by):
        rows = [{**{f: s.get("keys", {}).get(f) for f in by}, **{k: s.get(RENAMED.get(k, k)) for k in BASELINE_COLUMNS}}
                for _, s in self.all() if s.get("series") == series and not str(s.get("baseline_source") or "").startswith("group:")]
        return pd.DataFrame(rows, columns=by + list(BASELINE_COLUMNS))

    def load_group(self, series):
        rows = [{"group": s["baseline_source"][len("group:"):], **{k: s.get(k) for k in GROUP_COLUMNS}}
                for _, s in self.all() if s.get("series") == series and str(s.get("baseline_source") or "").startswith("group:")]
        return pd.DataFrame(rows, columns=["group", *GROUP_COLUMNS])

    def save(self, series, docs, trained_at, had_data):
        self.bulk(docs)
        if docs or had_data:
            d = self.data.get(self.INDEX, {})
            for _id in [i for i, s in d.items() if s.get("series") == series and str(s.get("trained_at")) < trained_at]:
                del d[_id]


class MemoryTuning(_Docs):
    INDEX = TUNING_INDEX

    def all(self) -> list[dict]:   # noqa: D102  跟 TuningRepository.all 一樣回帶 id 的文件
        return sorted([{"id": i, **s} for i, s in self.data.get(self.INDEX, {}).items()], key=lambda a: str(a.get("at") or ""))

    def proposed(self, size: int = 50) -> list[dict]:
        return [a for a in self.all() if a.get("status") == "proposed"][:size]

    def exists(self, _id) -> bool:
        return self.get(_id) is not None

    def put(self, _id, doc) -> None:
        self.bulk([(_id, validated(doc))])


class MemoryAlerts(_Docs):
    INDEX = ALERT_INDEX

    def open(self) -> list[Alert]:
        return [Alert.from_doc(s) for _, s in self.all() if s.get("status") == "open"]

    def resolved_anomaly_ids(self, since) -> set[str]:
        return {i for _, s in self.all() if s.get("status") == "resolved" and _in(s.get("resolved_at"), since, None) for i in s.get("anomaly_ids") or []}

    def list(self, status="open", size=50) -> list[dict]:
        return [s for _, s in self.all() if status == "all" or s.get("status") == status][:size]

    def started_between(self, start, end) -> list[dict]:
        return [{**s, "_id": i} for i, s in self.all() if _in(s.get("started_at"), start, end)]

    def request_rca(self, alert_id, at) -> None:
        self.patch(alert_id, {"rca_requested": True, "rca_requested_at": at})


class MemoryIncidents(_Docs):
    INDEX = INCIDENT_INDEX

    def get_many(self, ids) -> list[dict]:
        d = self.data.get(self.INDEX, {})
        return [{"id": i, **d[i]} for i in ids if i in d]

    def find(self, statuses, since=None, having=(), exclude_id=None, size=1000) -> list[tuple[str, dict]]:
        return [(i, s) for i, s in self.all() if s.get("status") in statuses and i != exclude_id and all(s.get(f) for f in having)
                and (since is None or _in(s.get("start"), since, None))][:size]

    def similar(self, fp, statuses, exclude_id=None, size=200) -> list[tuple[str, dict]]:
        return self.find(statuses, exclude_id=exclude_id)[:size]

    def recent(self, size=200) -> list[tuple[str, dict]]:
        return sorted(self.all(), key=lambda x: str(x[1].get("start") or ""), reverse=True)[:size]

    def started_between(self, start, end) -> list[dict]:
        return [{**s, "_id": i} for i, s in self.all() if _in(s.get("start"), start, end)]

    def upsert_auto(self, incidents) -> int:
        d = self.data.setdefault(self.INDEX, {})
        n = 0
        for _id, doc in incidents:
            if d.get(_id, {}).get("status") in (None, "auto"):
                d[_id] = {**d.get(_id, {}), **json.loads(json.dumps(doc, default=str))}
                n += 1
        return n


class MemoryFeedback(_Docs):
    INDEX = FEEDBACK_INDEX

    def since(self, start, end=None) -> list[dict]:
        return sorted([{**s, "_id": i} for i, s in self.all() if _in(s["@timestamp"], start, end)], key=lambda s: s["@timestamp"])

    def missed_since(self, start, end=None, without_alert=False) -> list[dict]:
        return [s for s in self.since(start, end) if s.get("verdict") == "missed" and not (without_alert and s.get("alert_id"))]

    def by_alert_ids(self, alert_ids) -> list[dict]:
        want = {i for i in alert_ids if i}
        return [{**s, "_id": i} for i, s in self.all() if s.get("alert_id") in want]


class MemoryTraces:
    def __init__(self, spans: list[dict] | None = None):
        self._spans = spans or []

    def spans(self, index, trace_ids, start, end):
        want = set(trace_ids)
        return [s for s in self._spans if not want or s.get("trace_id") in want]

    def spans_by_id(self, index, span_ids, start, end):
        want = set(span_ids)
        return {s["span_id"]: s for s in self._spans if s.get("span_id") in want}


class MemorySegments(_Docs):
    INDEX = SEGMENT_INDEX

    def segments(self, start, end) -> list[dict]:
        return [s for _, s in self.all() if s.get("kind") == "segment" and _in(s["@timestamp"], start, end)]


class MemoryState(_Docs):
    INDEX = STATE_INDEX

    def set(self, key, doc) -> None:
        self.put(key, doc)


class MemoryRepos:
    """跟 Repos 同樣的屬性。frames：{index pattern: DataFrame}；incidents：[(id, 文件)] 預先放進 aiops-incidents；scenario 沒給就依環境變數。"""
    es = None

    def __init__(self, frames: dict[str, pd.DataFrame] | None = None, fmt: str = "events", spans: list[dict] | None = None,
                 incidents: list[tuple[str, dict]] | None = None, scenario=None):
        self.scenario = scenario or load_scenario()
        self.data: dict[str, dict[str, dict]] = {}
        self.series = MemorySeries(frames, fmt)
        self.anomalies = MemoryAnomalies(self.data)
        self.baseline = MemoryBaseline(self.data)
        self.tuning = MemoryTuning(self.data)
        self.alerts = MemoryAlerts(self.data)
        self.incidents = MemoryIncidents(self.data)
        self.feedback = MemoryFeedback(self.data)
        self.traces = MemoryTraces(spans)
        self.segments = MemorySegments(self.data)
        self.state = MemoryState(self.data)
        if incidents:
            self.incidents.bulk(incidents)

    def docs(self, index: str) -> list[tuple[str, dict]]:
        return list(self.data.get(index, {}).items())
