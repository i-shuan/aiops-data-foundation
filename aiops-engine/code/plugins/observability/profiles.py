"""IT 可觀測性的靜態表：原始資料兩種格式的欄位對照（PROFILES，AIOPS_SOURCE 選）、用語與回饋表單的分類表（DOMAIN）、
異常的 keys 裡哪個是服務（ENTITY_KEYS）、嚴重度的黃金訊號分類（SEVERITY_CLASSES）、預設排程表（SCHEDULES）。Plugin 類別在 __init__.py。"""
from __future__ import annotations

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

# 嚴重度的訊號分類（investigator/alerts/severity.py 的 classes）：黃金訊號。依序比對：流量先（dep.calls 是流量，不是下游變慢）、
# 再往外呼叫、延遲、錯誤、飽和。latency（延遲）、errors（錯誤）、downstream（往外呼叫：DB、下游、外部系統）、saturation（飽和：CPU、記憶體、
# 連線池、lag）、traffic（流量，不算廣度）。寫法跟 rca.yaml 的 alerts.severity.classes 一樣（severity.parse_classes），cfg 可覆蓋
SEVERITY_CLASSES = {
    "traffic": [r"requests", r"\.calls$", r"\.lines$", r"\.count$"],
    "downstream": [r"^dep\.", r"destination", r"downstream", r"upstream", r"external"],
    "latency": [r"latency", r"slow_ratio", r"duration", r"p9\d"],
    "errors": [r"error", r"exception", r"swallow", r"restart", r"failed", r"not_ready", r"pending"],
    "saturation": [r"cpu", r"memory", r"disk", r"pool", r"lag", r"throttl", r"heap", r"\bgc\b", r"thread", r"full"],
}
