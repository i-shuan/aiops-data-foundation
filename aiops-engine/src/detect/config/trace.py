"""trace.yaml：trace 的合成、每一段的 baseline 與規則。"""
from __future__ import annotations

from common.config.common import Direction, Dur, NonNeg, PosInt, Ratio, Strict
from detect.config.types import Detect, Status, Train


# ── trace.yaml ──

class Merge(Strict):
    settle_seconds: NonNeg = 10   # 剛寫進 ES 的 span 等 refresh；合到「現在 − 這幾秒」（等這麼久才合（apm-server 批次送、ES refresh；APM 要比 Collector 久））


class TraceRules(Strict):
    repeated_calls: NonNeg | None = None
    swallowed_rate: Ratio | None = None


class MetricRule(Strict):
    direction: Direction = "both"
    min_abs: NonNeg = 0.0


class TraceMetrics(Strict):
    """detector/trace.METRICS 四種數字各自怎麼判（預設：呼叫次數兩邊都看，其他只看變大）。"""
    calls: MetricRule = MetricRule(direction="both", min_abs=0.5)           # 每個請求至少多（或少）0.5 次：N+1、重試、快取穿透；少了可能是功能沒跑
    time_ms: MetricRule = MetricRule(direction="up", min_abs=20)            # 至少多 20 毫秒：拖累、熱點鎖、慢查詢
    error_rate: MetricRule = MetricRule(direction="up", min_abs=0.05)       # 至少多 5 個百分點
    swallowed_rate: MetricRule = MetricRule(direction="up", min_abs=0.05)   # 吞掉的例外變多：至少多 5 個百分點


class TraceConfig(Strict):
    traces_index: str = "traces-merged-*"    # 合成後的索引
    interval: Dur = "5m"   # 每 5 分鐘一個點
    history_weeks: PosInt = 4
    sample_per_bucket: PosInt = 100   # 每個時間窗、每個 API 抽幾個請求來算。100 是 ES top_hits 的預設上限（index.max_inner_result_window），再大要改索引設定；20 個時平常 3% 的慢請求讓平均值飄太大（docs/pdca/phase2-anomaly/2026-11-trace-sample-100.md）
    min_requests: NonNeg = 5   # 這個時間窗抽到的請求少於這個數，不判斷（太少，平均不準）
    clock_skew_ms: NonNeg = 500   # 呼叫方與被呼叫方的時間差超過這個就算時鐘不同步（規則，不用學）
    sample_policy: str | None = None
    raw_index: str = "traces-generic.otel-*"   # 合成前的原始 span（APM 是 traces-apm-*）
    merge: Merge = Merge()
    rules: TraceRules = TraceRules()
    train: Train = Train()
    detect: Detect = Detect(rel_floor=0.1)    # 每個請求呼叫幾次這種數字很穩（MAD 常常是 0），波動至少當成平常值的 10%
    status: Status | None = None             # 沒寫的鍵不算有（with_status 用 .get）
    metrics: TraceMetrics = TraceMetrics()
