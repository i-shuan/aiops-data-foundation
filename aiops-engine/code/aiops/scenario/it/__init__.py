"""IT 可觀測性（預設）：對象是服務、「API」是 method＋route、trace 的呼叫關係決定誰連帶誰、版號第一次出現當換版。
原始資料兩種格式，AIOPS_SOURCE 選：otel（我們的 Collector 寫的）、apm（Elastic APM／Fleet apm integration；docs/pdca/meta/2026-11-apm-source.md）。
"""
from __future__ import annotations

import os
from datetime import datetime

from aiops.scenario.base import Chain, Plugin as Base
import aiops.service.converge.merge as AL

from aiops.scenario.it.chain import call_edges
from aiops.scenario.it.change import deploy_at

SOURCE_ENV = "AIOPS_SOURCE"

PROFILES = {
    "otel": {
        "owner": {"index": "metrics-*.otel-*", "namespace": "resource.attributes.k8s.namespace.name",
                  "service": "resource.attributes.service.name", "dept": "resource.attributes.dept", "group": "resource.attributes.group"},
        # log 的 resource.attributes.service.name、trace_id 是 alias（component-mappings.json），_source 裡沒有，用 fields 取
        "log": {"service": "resource.attributes.service.name", "level": "log.level", "trace_id": "trace_id", "message": ["message"],
                "problem_levels": ["warn", "error", "fatal"], "problem_query": None},
    },
    "apm": {
        # dept、group 是 agent 設的 label（labels.dept、labels.group）；沒設就全部 unassigned
        "owner": {"index": "metrics-apm.transaction.1m-*", "namespace": "kubernetes.namespace",
                  "service": "service.name", "dept": "labels.dept", "group": "labels.group"},
        # log.level 是應用自己寫的，大小寫不一（ERROR、error、Error），keyword 比對分大小寫，全列。
        # logs-apm.error-* 的例外文件沒有 log.level，用 processor.event: error 一起撈；訊息在 message 或 error.exception.message
        "log": {"service": "service.name", "level": "log.level", "trace_id": "trace.id",
                "message": ["message", "error.exception.message", "error.log.message"],
                "problem_levels": ["warn", "warning", "error", "fatal", "critical", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL",
                                   "Warn", "Warning", "Error", "Fatal", "Critical"],
                "problem_query": {"term": {"processor.event": "error"}}},
    },
}

DOMAIN = {
    "entity": "服務", "unit": "請求", "trace": "Trace", "new_value_hint": "換版後的新 Pod、新版本",
    "categories": [("db", "DB（慢查詢、鎖、連線池、統計資訊）"), ("network", "網路（延遲、斷線、DNS）"), ("config_change", "設定變更"),
                   ("deploy", "版本更新"), ("external", "外部系統（供應商、其他部門的服務）"), ("resource", "資源不足（CPU、記憶體、磁碟）"),
                   ("bug", "程式錯誤"), ("data", "資料問題（髒資料、量暴增）"), ("other", "其他")],
    "fp_reasons": [("batch", "月底 / 定時批次本來就這樣"), ("loadtest", "壓測、演練"), ("known_slow", "這支 API 本來就慢"),
                   ("low_volume", "量太少，幾筆就跳"), ("deploy", "換版期間"), ("other", "其他")],
    "checked": ["Oracle AWR / 慢查詢", "DB 連線池", "主機 CPU / 記憶體 / 磁碟", "網路 / DNS", "Kibana log", "trace 瀑布圖",
                "Kafka lag", "近期的版本更新 / 設定變更", "上游 / 下游服務", "外部系統狀態"],
}

# 異常的 keys 裡哪個是服務（baseline、規則、peer、quality、trace 的寫法都不一樣）；caller／callee 是 trace 分段的
ENTITY_KEYS = ("resource.attributes.service.name", "service", "entry.service", "service.name", "caller", "callee")
SCHEDULES = {"otel": "configs/schedule/schedule.yaml", "apm": "configs/apm/schedule.yaml"}


def route_of(keys: dict) -> str | None:
    """哪支 API：trace 入口 span 的名稱、或 method＋route；APM 的 transaction.name 本來就是 "GET /api/x"，只有 route。"""
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

    def route_of(self, keys: dict) -> str | None:
        return route_of(keys)

    def chain(self, es, cfg, anomalies, open_alerts, start, now) -> Chain:
        """跨服務（一層）：這段時間 trace 裡「誰呼叫誰」，只看有件開著、或這次有異常的服務。"""

        acfg = cfg.get("alerts", {})
        svcs = {al.service for al in open_alerts if al.category == "service"}
        svcs |= {ent[1] for ent in map(AL.entity_of, anomalies) if ent[0] == "service"}
        return Chain(calls=call_edges(es, cfg, svcs, start, now, int(acfg.get("chain_sample", 100))))

    def change_at(self, es, cfg, entity, since, now) -> datetime | None:
        acfg = cfg.get("alerts", {})
        return deploy_at(es, entity, since, now, acfg.get("deploy_index", "metrics-apm.transaction.1m-*"), acfg.get("version_field", "service.version"))
