"""欄位對照的覆寫：提、試、核准、退回、停用（② 清資料：讀進來時欄位叫什麼）。存在 aiops-mapping（common/scenario/mapping.py）。

流程跟調整（aiops-tuning）一樣：任何人都能**提**（網頁、命令列），status proposed，還沒生效；**核准**只能用命令列
（`detector mapping approve`，有權限進 pod／跑排程的人才能），核准前一定先對真的資料**試**一次：
新的欄位名在抽到的文件裡一次都沒出現，就不給核准（--force 才行）——對照改錯會讓偵測悄悄停擺，比沒改還糟。
"""
from __future__ import annotations

from common.clock import now_of
from common.infra.es import doc_id
from common.scenario.mapping import EDITABLE, LIST_FIELDS, MappingRepository, _cache, check_value, effective, profiles
from data.source import ADAPTERS, adapter_of, for_index
from data.source.check import Finding, check_fields, check_trace_index

Pairs = list[tuple[str, str | None]]      # trace.yaml 的 raw_indexes()：[(索引, 明講的來源)]


def yaml_raw(scenario, pairs: Pairs | None) -> dict[str, str]:
    """trace.yaml 的 raw 索引，照來源分：{來源: 索引}（沒明講來源的，用索引名與宣告的來源判斷；同一個來源多個索引用逗號接）。"""
    out: dict[str, list[str]] = {}
    for index, src in pairs or []:
        out.setdefault(src or for_index(index, scenario.source).name, []).append(index)
    return {k: ",".join(v) for k, v in out.items()}


def raw_indexes(scenario, pairs: Pairs) -> Pairs:
    """trace-merge 這一輪要讀的 raw 索引：trace.yaml 的，蓋上核准過的覆寫（scenario 要先過 effective）。
    覆寫是「這個來源的 raw 索引改成…」：那個來源原本的索引全部換掉；trace.yaml 沒列這個來源就加上去。"""
    over = dict(scenario.trace_raw)
    if not over:
        return list(pairs)
    out, done = [], set()
    for index, src in pairs:
        name = src or for_index(index, scenario.source).name
        if name in over:
            if name not in done:
                out.append((over[name], name))
                done.add(name)
        else:
            out.append((index, src))
    out += [(ix, name) for name, ix in over.items() if name not in done]
    return out


def _yaml_profiles(scenario, pairs: Pairs | None) -> dict:
    """profiles 的 trace 段換成 trace.yaml 的值（profiles 本身只有覆寫）。"""
    base, raw = profiles(scenario), yaml_raw(scenario, pairs)
    for src, p in base.items():
        p["trace"] = {"raw_index": raw[src]} if src in raw else {}
    return base


def _effective_profiles(es, scenario, pairs: Pairs | None) -> dict:
    eff, raw = profiles(effective(scenario, es)), yaml_raw(scenario, pairs)
    for src, p in eff.items():
        p["trace"] = p["trace"] or ({"raw_index": raw[src]} if src in raw else {})
    return eff


def current(es, scenario, pairs: Pairs | None = None) -> list[dict]:
    """每個來源、每個開放的欄位：yaml 的值、現在生效的值、是不是被覆寫了（網頁、命令列的清單用）。
    pairs：trace.yaml 的 raw_indexes()（沒給，trace 的 yaml 值顯示不出來）。"""
    base, eff = _yaml_profiles(scenario, pairs), _effective_profiles(es, scenario, pairs)
    active = {(o["source"], o["section"], o["field"]): o for o in MappingRepository(es).active()} if es is not None else {}
    rows = []
    for src in base:
        for section, fields in EDITABLE.items():
            for f in fields:
                o = active.get((src, section, f))
                rows.append({"source": src, "section": section, "field": f, "yaml": base[src][section].get(f),
                             "value": eff[src][section].get(f), "override": o["id"] if o else None})
    return rows


def try_value(es, scenario, source: str, section: str, field: str, value) -> list[Finding]:
    """新的值對真的資料試一下：抽 20 筆，看這個欄位有沒有出現（換索引的話，看服務、namespace 等主要欄位在新索引有沒有）。
    回空清單＝試過、沒問題；problem_levels 是值不是欄位名，抽樣看不出來，回一筆 unchecked。"""
    value = check_value(section, field, value)
    prof = dict(profiles(scenario).get(source, {}).get(section) or {})
    if not prof and source not in profiles(scenario):
        return [Finding(f"{source}.{section}.{field}", f"來源 {source} 沒有在用（AIOPS_SOURCE）", "mismatch")]
    if (section, field) == ("trace", "raw_index"):     # 索引裡要有資料，而且是這個來源的格式
        return [Finding(f"{source}.trace.raw_index", f.problem, f.kind) for f in check_trace_index(es, value, adapter_of(source), ADAPTERS)]
    if (section, field) == ("log", "problem_levels"):
        return [Finding(f"{source}.log.problem_levels", "這是 log 等級的值（ERROR、warn…），不是欄位名，抽樣看不出對不對；核准前自己確認", "unchecked")]
    prof[field] = value
    index = prof.get("index")
    if not index:
        return [Finding(f"{source}.{section}", "這份對照沒有 index，不知道去哪裡抽樣（log 的主要來源索引在 rca.yaml 的 indices.logs；可以先提一筆 log.index）", "unchecked")]
    if field == "index":
        names = {"owner": ("service", "namespace"), "log": ("service", "trace_id")}[section]
        want = [prof[n] for n in names if prof.get(n)]
    elif (section, field) in LIST_FIELDS:            # message：清單裡至少一個欄位要出現
        found = [f for f in value if not check_fields(es, "", index, None, [f])]
        return [] if found else [Finding(f"{source}.{section}.{field}", f"{index} 抽到的文件裡，{'、'.join(value)} 一個都沒出現", "mismatch")]
    else:
        want = [value]
    return [Finding(f"{source}.{section}.{field}", f.problem, f.kind) for f in check_fields(es, f"{source}.{section}.{field}", index, None, want)]


def propose(es, scenario, source: str, section: str, field: str, value, by: str, reason: str = "",
            pairs: Pairs | None = None) -> tuple[str, list[Finding]]:
    """提一筆（status proposed，還沒生效），順便試一次、把結果存起來。回 (id, 試的結果)。"""
    if not (by or "").strip():
        raise ValueError("要寫是誰提的（by）")
    value = check_value(section, field, value)
    if source not in profiles(scenario):
        raise ValueError(f"來源 {source!r} 沒有在用（AIOPS_SOURCE 是 {','.join(profiles(scenario))}）")
    old = _effective_profiles(es, scenario, pairs)[source][section].get(field)
    if old == value:
        raise ValueError(f"{source}.{section}.{field} 現在就是 {value}，不用改")
    findings = try_value(es, scenario, source, section, field, value)
    now = now_of().isoformat()
    _id = doc_id("mapping", source, section, field, str(value), now)
    MappingRepository(es).put(_id, {"source": source, "section": section, "field": field, "value": value, "old_value": old,
                                    "status": "proposed", "by": by.strip(), "reason": reason, "created_at": now,
                                    "check": "\n".join(str(f) for f in findings) or "試過：欄位在資料裡有出現"})
    return _id, findings


def approve(es, scenario, _id: str, by: str, force: bool = False) -> tuple[bool, str]:
    """proposed → active。核准前再試一次：對不上（或抽不到資料、試不了）就不給，除非 force。同一個欄位原本生效的那筆改成 disabled。"""
    repo = MappingRepository(es)
    doc = repo.get(_id)
    if not doc:
        return False, f"找不到 {_id}（mapping list 看得到）"
    if doc.get("status") != "proposed":
        return False, f"{_id} 是 {doc.get('status')}，只有 proposed 的能核准"
    findings = try_value(es, scenario, doc["source"], doc["section"], doc["field"], doc["value"])
    if findings and not force:
        return False, "不核准：\n" + "\n".join(f"  {f}" for f in findings) + "\n確定要用（例如資料還沒進來）就加 --force"
    now = now_of().isoformat()
    for o in repo.active():
        if (o["source"], o["section"], o["field"]) == (doc["source"], doc["section"], doc["field"]):
            repo.patch(o["id"], {"status": "disabled", "disabled_by": by, "disabled_at": now, "replaced_by": _id})
    repo.patch(_id, {"status": "active", "approved_by": by, "approved_at": now})
    _cache.clear()
    note = "" if not findings else "（--force：" + "；".join(str(f) for f in findings) + "）"
    return True, f"已核准 {_id}：{doc['source']}.{doc['section']}.{doc['field']} {doc.get('old_value')} → {doc['value']}，下一輪工作就用新的{note}"


def set_status(es, _id: str, status: str, by: str) -> tuple[bool, str]:
    """reject（proposed → rejected）、disable（active → disabled，退回 yaml 的值）。"""
    repo = MappingRepository(es)
    doc = repo.get(_id)
    allowed = {"rejected": "proposed", "disabled": "active"}
    if not doc:
        return False, f"找不到 {_id}"
    if doc.get("status") != allowed[status]:
        return False, f"{_id} 是 {doc.get('status')}，{status} 只能用在 {allowed[status]} 的"
    repo.patch(_id, {"status": status, f"{status}_by": by, f"{status}_at": now_of().isoformat()})
    _cache.clear()
    return True, f"{_id} → {status}" + ("（退回 yaml 的值，下一輪工作生效）" if status == "disabled" else "")
