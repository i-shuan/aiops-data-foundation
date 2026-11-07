"""rca.yaml：根因分析、事件整合、通知。預設值是 IT 情境（API、5 分鐘一輪）；工廠情境（configs/factory/rca.yaml）只寫跟它不同的鍵。"""
from __future__ import annotations

from typing import Any

from common.config.common import Dur, NonNeg, Num, PosInt, Ratio, Strict, Tz


# ── rca.yaml ──

class Indices(Strict):
    # ES 的索引（原始資料、合成的 trace）；engine 自己的資料在 PostgreSQL，不在這裡設
    traces: str | None = None          # metrics_only 沒有履歷
    logs: str | None = None


class Trend(Strict):
    windows: PosInt = 6   # 往前看幾個時間窗（往前看幾個時間窗）
    window: Dur = "10m"


class Commonality(Strict):
    resource: list[str] = ["k8s.pod.name", "service.version", "k8s.node.name"]   # 慢的請求共同的資源屬性
    attrs: list[str] = ["lot.id", "db.statement"]                              # 慢的請求共同的 span 屬性
    min_share: Ratio = 0.6   # 慢的請求裡至少六成有同一個值
    min_lift: NonNeg = 2   # 而且是平常比例的 2 倍以上（只有 1 個 Pod 時兩邊都 100%，不會列）
    min_traces: PosInt = 5


class Changes(Strict):
    lookback: Dur = "12h"
    query: dict[str, Any] | None = None


class SeverityClass(Strict):
    name: str
    patterns: list[str] | None = None


class SeverityCfg(Strict):
    """investigator/severity.GradeOptions(**severity)（classes 先 pop 掉）。"""
    window: PosInt | None = None
    min_coverage: Ratio | None = None
    min_duration: NonNeg | None = None
    min_span_critical: NonNeg | None = None
    min_points_per_class: PosInt | None = None
    deploy_grace: NonNeg | None = None
    classes: dict[str, list[str] | str] | list[SeverityClass] | None = None


class SimilarVector(Strict):
    weight: NonNeg = 5.0
    min_similarity: Ratio = 0.8
    k: PosInt = 10


class Alerts(Strict):
    """事件整合（incident/alert_service.py）＋ 情境策略讀的換版、前後站設定（scenario/it、scenario/factory）。"""
    close_after: Dur = "30m"                # 這麼久沒有新異常就結案
    lookback: Dur = "2h"   # 每次讀最近多久的異常
    severity: SeverityCfg | None = None
    # P0 服務（名字，或整個名字對得上的 regex）：這些服務的件嚴重度升一級（warning→high、high→critical）；門檻不動
    p0_services: list[str] = []
    critical_levels: list[str] | None = None    # 沒寫用情境的（Scenario.critical_levels）
    deploy_grace: bool = True
    deploy_index: str | None = None
    version_field: str | None = None
    deploy_entity_field: str | None = None
    deploy_time_field: str | None = None
    deploy_filter: dict[str, Any] | None = None
    rca: bool = True   # 新的一件跑一次根因分析，結論寫進摘要、全文存 report（失敗不擋這一件；相似事故、LLM 也一樣）
    rca_lead: Dur = "0s"                     # 根因分析的時間範圍從事故開始往前多少
    rca_retry: bool = True   # 開件太快、trace 還沒合成完（晚 1～2 分鐘）時第一份報告是空的：沒有 trace 的件隔一陣子重跑
    rca_retry_after: Dur = "2m"   # 隔多久重跑一次
    rca_retry_max: PosInt = 5   # 最多跑幾次（含第一次）；服務本來就沒埋點的，不會永遠重跑
    similar: bool = True   # 附上老師傅確認過的相似事故（incident 的 similar）
    similar_weights: dict[str, Num] | None = None
    # 向量輔助（要設 EMBEDDING_URL、EMBEDDING_MODEL）：相似度 ≥ min_similarity 加 weight × 相似度分；k 是向量最近的幾件當候選
    similar_vector: SimilarVector | None = None
    llm: bool = True   # LLM 摘要（llm/summary.py）：provider 由環境變數 LLM_PROVIDER 決定（template 不叫模型）
    llm_provider: str | None = None          # 沒寫用環境變數 LLM_PROVIDER（settings.llm_provider）
    chain: bool = True   # 跨服務（一層）：下游出事連帶上游異常時只開一件、留下游；呼叫關係從這段時間的 trace 算
    chain_sample: PosInt = 100   # 算呼叫關係時每個服務取最近幾個請求的 trace
    paths_lookback: Dur = "3d"   # 履歷往前找幾天的 lot（工廠）
    feedback_url: str | None = None
    report_url: str | None = None


class IncidentsDb(Strict):
    url: str | None = None
    sql: str | None = None


class RcaConfig(Strict):
    indices: Indices                         # 一定要有（anomalies；traces、logs 看情境）
    timezone: Tz = "Asia/Taipei"   # 報告裡的時間
    compare_with: Dur = "7d"   # 「平常」＝ 這麼久以前的同一段時間（跟多久以前的同一段比）
    ref_gap: Dur = "10m"   # 那段沒有資料時：改用出事前、同樣長的一段，中間空這麼久（避開剛開始變壞的時候）
    sample: PosInt = 200   # 出事時挑最慢的這麼多個（再加最多 100 個出錯的）請求來比
    interval: Dur = "5m"   # 跟 baseline 一樣：異常的時間是那個時間窗的開始
    entry_lag: Dur = "0s"                    # 入口比文件時間晚最多多久（工廠履歷：lot 開始到到這一站）
    trend: Trend = Trend()
    commonality: Commonality = Commonality()
    changes: Changes | None = None           # 沒寫 query 就不出「事故前的變更」那一段
    alerts: Alerts = Alerts()
    incidents_db: IncidentsDb | None = None
