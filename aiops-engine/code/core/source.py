"""資料來源 profile：engine 讀的原始資料是誰寫的，決定程式裡寫死的那幾個欄位名與索引名。

序列、規則、Pod 同儕比對的欄位全在 YAML（configs/<來源>/*.yaml），不經過這裡；這裡只管 YAML 碰不到的：
  owner   dept／group 歸屬查哪個索引、哪幾個欄位（detector/owner.py）
  log     根因分析讀 log 的欄位（investigator/es.py）
  traces  raw trace 的 trace id 欄位（detector/trace_merge.py；span 的轉換在 core/traces.py 依索引名選）
環境變數 AIOPS_SOURCE 選：otel（預設，我們的 Collector 寫的）、apm（Elastic APM／Fleet apm integration）。
chart 的 values `source` 會設這個變數，並把排程表換成 configs/<來源>/schedule.yaml（docs/pdca/meta/2026-11-apm-source.md）。
"""
from __future__ import annotations

import os

PROFILES = {
    "otel": {
        "owner": {"index": "metrics-*.otel-*", "namespace": "resource.attributes.k8s.namespace.name",
                  "service": "resource.attributes.service.name", "dept": "resource.attributes.dept", "group": "resource.attributes.group"},
        # log 的 resource.attributes.service.name、trace_id 是 alias（component-mappings.json），_source 裡沒有，用 fields 取
        "log": {"service": "resource.attributes.service.name", "level": "log.level", "trace_id": "trace_id", "message": ["message"],
                "problem_levels": ["warn", "error", "fatal"], "problem_query": None},
        "traces": {"trace_id": "trace_id"},
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
        "traces": {"trace_id": "trace.id"},
    },
}


def name() -> str:
    s = os.environ.get("AIOPS_SOURCE", "otel")
    if s not in PROFILES:
        raise ValueError(f"AIOPS_SOURCE 只能是 {'、'.join(PROFILES)}，不是 {s!r}")
    return s


def profile(section: str) -> dict:
    return PROFILES[name()][section]
