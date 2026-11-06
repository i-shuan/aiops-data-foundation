"""IT 可觀測性（預設）：對象是服務、「API」是 method＋route、trace 的呼叫關係決定誰連帶誰、版號第一次出現當換版。
原始資料兩種格式，AIOPS_SOURCE 選：otel（我們的 Collector 寫的）、apm（Elastic APM／Fleet apm integration；docs/pdca/meta/2026-11-apm-source.md）。
"""
from __future__ import annotations

import os
from datetime import datetime

from core.plugin import Chain, Plugin as Base
from investigator.alerts import merge as AL
from investigator.rca import path_sections

from .chain import call_edges
from .changes import deploy_at
from .chronic.command import register as _register_chronic
from .peer.command import register as _register_peer
from .profiles import DOMAIN, ENTITY_KEYS, PROFILES, SCHEDULES, SEVERITY_CLASSES
from .quality.command import register as _register_quality
from .trace.command import commands as _trace_commands

SOURCE_ENV = "AIOPS_SOURCE"


def where_of(keys: dict) -> str | None:
    """「在哪裡」＝哪支 API：trace 入口 span 的名稱、或 method＋route；APM 的 transaction.name 本來就是 "GET /api/x"，只有 route。"""
    if keys.get("entry.name"):
        return keys["entry.name"]
    for m, r in (("attributes.http.request.method", "attributes.http.route"), ("method", "route")):
        if keys.get(m) and keys.get(r):
            return f"{keys[m]} {keys[r]}"
    if keys.get("route"):
        return str(keys["route"])
    return None


class Plugin(Base):
    name = "observability"
    entity_keys = ENTITY_KEYS
    severity_classes = SEVERITY_CLASSES                  # 黃金訊號
    sections = ("anomalies", "entries", "segments", "slowest", "logs", "changes", "topology", "conclusion")

    def source_name(self) -> str:
        s = os.environ.get(SOURCE_ENV, "otel")
        if s not in PROFILES:
            raise ValueError(f"{SOURCE_ENV} 只能是 {'、'.join(PROFILES)}，不是 {s!r}")
        return s

    def source_profile(self) -> dict:
        return PROFILES[self.source_name()]

    @property
    def schedule(self) -> str:  # type: ignore[override]
        return SCHEDULES[self.source_name()]

    def domain(self) -> dict:
        return DOMAIN

    def where_of(self, keys: dict) -> str | None:
        return where_of(keys)

    def report_sections(self) -> dict:
        """有 trace：報告多 entries／segments／slowest／topology 四段。"""
        return path_sections.SECTIONS

    def chain(self, es, cfg, anomalies, open_alerts, start, now) -> Chain:
        """跨服務（一層）：這段時間 trace 裡「誰呼叫誰」，只看有件開著、或這次有異常的服務。"""
        acfg = cfg.get("alerts", {})
        svcs = {al["service"] for al in open_alerts if al.get("category") == "service"}
        svcs |= {ent[1] for ent in map(AL.entity_of, anomalies) if ent[0] == "service"}
        return Chain(calls=call_edges(es, cfg, svcs, start, now, int(acfg.get("chain_sample", 100))))

    def change_at(self, es, cfg, entity, since, now) -> datetime | None:
        acfg = cfg.get("alerts", {})
        return deploy_at(es, entity, since, now, acfg.get("deploy_index", "metrics-apm.transaction.1m-*"), acfg.get("version_field", "service.version"))

    def commands(self) -> dict:
        """IT 專用的 detector 子命令：peer、quality、chronic、trace-collect／train／detect／merge。"""
        return {"peer": _register_peer, "quality": _register_quality, "chronic": _register_chronic, **_trace_commands()}
