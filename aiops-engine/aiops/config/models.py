"""設定檔的 pydantic 模型：一種設定檔（core/config.KINDS）一個最上層模型。

只做「鍵有沒有打錯、型別對不對、幾個明顯的範圍」；預設值仍在各指令的 cfg.get(..., 預設) 裡（集中到這裡是下一輪）。
所以每個欄位都是選填、不給預設（None），load_config 用 exclude_unset 倒回 dict，內容跟 yaml 一樣。

數字一律 int | float（smart union：yaml 寫 2 還是 2、寫 2.0 還是 2.0，不偷換型別）；時間點（行事曆、調整）可以是字串或 yaml 直接
解出來的 date／datetime；ES 的 query（filter）、聚合（value）、排程的 args 是自由格式（Any）。
欄位從哪來：configs/ 底下所有 yaml、tests/*.yaml，加上各指令讀的 cfg.get／spec.get／rule.get／c.get。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# ── 共用型別 ──
Num = int | float
NonNeg = Annotated[int | float, Field(ge=0)]
Ratio = Annotated[int | float, Field(ge=0, le=1)]
PosInt = Annotated[int, Field(ge=1)]
# 時間長度：30s、1m、5m、15m、1h、14d、90s；pandas 的寫法（10min、2h30m 不收）
Dur = Annotated[str, StringConstraints(pattern=r"^\d+(\.\d+)?(ms|s|m|h|d|w)$")]
When = str | datetime | date                 # yaml 的 2026-10-10 解成 date、2026-10-10T02:00 解成 datetime、帶時區的是字串
Query = dict[str, Any] | list[dict[str, Any]]  # ES 的 query 片段；規則的 filter 可以是清單
Value = Literal["count"] | dict[str, Any]      # count 或 {sum|avg|max|min|percentile|ratio|hist_count|hist_ratio|hist_ratio_above|div: …}
Direction = Literal["up", "down", "both"]
Severity = Literal["warning", "high", "critical"]
Show = Literal["pct", "seconds", "hours", "ns_as_ms", "number", "value", "cores", "bytes"]
Op = Literal[">=", ">", "<=", "<", "==", "!="]


class Strict(BaseModel):
    """多出來的鍵就是錯（打錯字要在啟動時擋下）；欄位名跟 yaml 的鍵一樣，少數 Python 關鍵字用 alias（for、from）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _same_length(keys, by, what: str = "keys") -> None:
    if keys is not None and by is not None and len(keys) != len(by):
        raise ValueError(f"{what} 有 {len(keys)} 個、by 有 {len(by)} 個，要一樣多（{what} 是 by 的每個欄位各取一個名字）")


# ── 共用的小段 ──

class Train(Strict):
    """detector/model.TrainOptions(**cfg["train"])：欄位要跟 dataclass 一樣。"""
    timezone: str | None = None
    min_weeks: PosInt | None = None
    min_days: PosInt | None = None
    min_points: PosInt | None = None
    min_slot_points: PosInt | None = None


class Detect(Strict):
    """detector/model.DetectOptions(**cfg["detect"])。"""
    z: NonNeg | None = None
    rel_floor: NonNeg | None = None
    abs_floor: NonNeg | None = None
    alpha: Ratio | None = None


class Status(Strict):
    """跳一次是 warning，連續幾窗才是 anomaly（detector/cmd_baseline.with_status）。"""
    confirm_points: PosInt | None = None
    confirm_span: Dur | None = None
    instant_z: NonNeg | None = None


class RecurringSchedule(Strict):
    cron: str | None = None
    duration: Dur | None = None
    days_of_month: list[int] | None = None


class RecurringEffect(Strict):
    z: NonNeg | None = None
    min_abs: NonNeg | None = None
    mute: bool | None = None
    train: Literal["keep", "exclude"] | None = None


class Adjustment(Strict):
    """一筆調整（detector/adjust.py）：series.yaml 的 overrides、adjustments.yaml 的 adjustments 同一種。kind 各自用到的欄位不同，
    這裡全部列出；哪一種缺什麼由 adjust.problem 判（它也要擋 ES 裡的壞資料）。"""
    id: str | None = None
    kind: Literal["override", "exclude_window", "reset_point", "recurring_override", "rule_suggestion"] | None = None
    series: str | None = None
    match: dict[str, Any] | None = None
    status: str | None = None               # active（預設）| proposed
    source: str | None = None               # manual | feedback | cicd | calendar
    expires_at: When | None = None
    reason: str | None = None
    by: str | None = None
    # exclude_window
    start: When | None = None
    end: When | None = None
    apply_to: list[Literal["train", "detect"]] | None = None
    # reset_point
    reset_at: When | None = None
    warmup: Dur | None = None
    at: When | None = None
    # recurring_override
    schedule: RecurringSchedule | None = None
    effect: RecurringEffect | None = None
    # override（detector/model.OVERRIDE_FIELDS）
    resolution: Dur | None = None
    z: NonNeg | None = None
    min_abs: NonNeg | None = None
    confirm_points: PosInt | None = None
    instant_z: NonNeg | None = None
    min_count: NonNeg | None = None


class Provider(Strict):
    """外部資料來源（detector/sources.py）：行事曆的 sources、慢性病的 owners_source。match／series／detect 只有行事曆用。"""
    provider: Literal["file", "http", "taiwan"]
    path: str | None = None
    url: str | None = None
    years: list[int] | None = None
    enabled: bool | None = None
    cache_dir: str | None = None
    cache_ttl: Dur | None = None
    headers: dict[str, str] | None = None
    timeout: NonNeg | None = None
    match: dict[str, Any] | None = None
    series: str | None = None
    detect: bool | None = None


# ── series.yaml ──

class AdaptiveMinEvents(Strict):
    rate: NonNeg | None = None
    count: NonNeg | None = None
    gauge: NonNeg | None = None


class Adaptive(Strict):
    resolutions: list[Dur] | None = None
    min_events: AdaptiveMinEvents | None = None
    coverage: Ratio | None = None


class Group(Strict):
    name: str
    match: dict[str, Any]


class Borrowing(Strict):
    min_members: PosInt | None = None
    z: NonNeg | None = None
    resolution: Dur | None = None


class Shift(Strict):
    """detector/cmd_shift.DEFAULTS。"""
    window: Dur | None = None
    min_points: PosInt | None = None
    min_share: Ratio | None = None
    max_spread: NonNeg | None = None


class Series(Strict):
    """一條序列（detector/es.py 的查詢、cmd_baseline 的判斷）。"""
    name: str
    index: str
    by: list[str]
    value: Value
    keys: list[str] | None = None
    filter: Query | None = None
    time_field: str | None = None
    scale: NonNeg | None = None
    events: Literal["value"] | dict[str, Any] | None = None
    kind: Literal["rate", "count", "gauge"] | None = None
    fill_zero: bool | None = None
    direction: Direction | None = None
    min_abs: NonNeg | None = None
    min_count: NonNeg | None = None
    confirm_points: PosInt | None = None
    instant_z: NonNeg | None = None
    borrow: bool | None = None
    resolution: Dur | None = None
    min_resolution: Dur | None = None
    buckets: list[Num] | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.keys, self.by)
        return self


class SeriesConfig(Strict):
    interval: Dur | None = None
    history_weeks: PosInt | None = None
    fine_history: Dur | None = None
    lag: Dur | None = None
    timezone: str | None = None
    adaptive: Adaptive | None = None
    status: Status | None = None
    overrides: list[Adjustment] | None = None
    groups: list[Group] | None = None
    borrowing: Borrowing | None = None
    train: Train | None = None
    detect: Detect | None = None
    shift: Shift | None = None
    adjustments_file: str | None = None
    calendar_file: str | None = None
    series: list[Series] | None = None


# ── rules.yaml ──

class Join(Strict):
    field: str


class Rule(Strict):
    """一條規則（detector/rules.py 檔頭的說明）。type window 用 value；其他用 field。"""
    name: str
    type: Literal["value", "compare", "ratio", "increase", "delta", "increase_ratio", "rate", "total", "window", "slope"]
    index: str
    by: list[str]
    threshold: Num
    title: str | None = None
    description: str | None = None
    severity: Severity | None = None
    field: str | None = None
    other: str | None = None
    den: list[str] | None = None
    value: Value | None = None
    filter: Query | None = None
    exists: str | None = None
    join: Join | None = None
    hist_count: bool | None = None
    context: list[str] | None = None
    op: Op | None = None
    for_: Dur | int | None = Field(default=None, alias="for")
    fresh: Dur | None = None
    lookback: Dur | None = None
    abs: bool | None = None
    report_gone: bool | None = None
    min_ratio: Ratio | None = None
    min_events: NonNeg | None = None
    min_points: PosInt | None = None
    step_s: NonNeg | None = None
    scale: NonNeg | None = None
    show: Show | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.type == "window":
            if self.value is None:
                raise ValueError("type window 要有 value（寫法跟 series.yaml 一樣）")
        elif self.field is None:
            raise ValueError(f"type {self.type} 要有 field")
        if self.type == "compare" and self.other is None:
            raise ValueError("type compare 要有 other（要跟 field 相減的欄位）")
        if self.type in ("ratio", "increase_ratio") and not self.den:
            raise ValueError(f"type {self.type} 要有 den（分母欄位）")
        return self


class RulesConfig(Strict):
    interval: Dur | None = None
    lookback: Dur | None = None
    rules: list[Rule] | None = None


# ── peer.yaml ──

class PeerCheck(Strict):
    """一個同儕比對（detector/peer.py、cmd_peer.py）：查法跟序列一樣，多了 min_volume、max_peer_median。"""
    name: str
    index: str
    by: list[str]
    value: Value
    title: str | None = None
    severity: Severity | None = None
    show: Show | None = None
    keys: list[str] | None = None
    filter: Query | None = None
    time_field: str | None = None
    scale: NonNeg | None = None
    direction: Direction | None = None
    min_abs: NonNeg | None = None
    min_ratio: NonNeg | None = None
    min_volume: NonNeg | None = None
    max_peer_median: NonNeg | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.keys, self.by)
        return self


class PeerConfig(Strict):
    window: Dur | None = None
    z: NonNeg | None = None
    z_by_peers: dict[int, NonNeg] | None = None
    rel_floor: NonNeg | None = None
    min_peers: PosInt | None = None
    fresh: Dur | None = None
    warmup: Dur | None = None
    checks: list[PeerCheck] | None = None


# ── quality.yaml ──

class QualityCheck(Strict):
    """一個資料品質檢查（detector/quality.py 檔頭）。type 不同用的欄位不同，這裡全列。"""
    name: str
    type: Literal["cardinality", "missing", "stopped", "share", "lag", "present"]
    index: str
    title: str | None = None
    severity: Severity | None = None
    filter: Query | None = None
    by: list[str] | None = None
    keys: list[str] | None = None
    time_field: str | None = None
    window: Dur | None = None
    min_docs: NonNeg | None = None
    # cardinality
    field: str | None = None
    max_values: NonNeg | None = None
    ratio: NonNeg | None = None
    min_increase: NonNeg | None = None
    id_share: Ratio | None = None
    sample_values: NonNeg | None = None
    # missing
    fields: list[str] | None = None
    missing_if_equals: dict[str, str] | None = None
    max_missing: Ratio | None = None
    max_increase: Ratio | None = None
    # stopped
    lookback: Dur | None = None
    silent: Dur | None = None
    # share
    part_index: str | None = None
    max_share: Ratio | None = None
    # lag
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    stat: Literal["p50", "p95"] | None = None
    max_seconds: NonNeg | None = None
    absolute: bool | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.keys, self.by)
        need = {"cardinality": ("field",), "missing": ("fields",), "share": ("part_index",), "lag": ("from_", "to")}
        for f in need.get(self.type, ()):
            if getattr(self, f) is None:
                raise ValueError(f"type {self.type} 要有 {f.rstrip('_')}")
        return self


class QualityConfig(Strict):
    interval: Dur | None = None
    window: Dur | None = None
    ref_offset: Dur | None = None
    checks: list[QualityCheck] | None = None


# ── trace.yaml ──

class Merge(Strict):
    settle_seconds: NonNeg | None = None


class TraceRules(Strict):
    repeated_calls: NonNeg | None = None
    swallowed_rate: Ratio | None = None


class MetricRule(Strict):
    direction: Direction | None = None
    min_abs: NonNeg | None = None


class TraceMetrics(Strict):
    """detector/trace.METRICS 四種數字各自怎麼判。"""
    calls: MetricRule | None = None
    time_ms: MetricRule | None = None
    error_rate: MetricRule | None = None
    swallowed_rate: MetricRule | None = None


class TraceConfig(Strict):
    traces_index: str | None = None
    interval: Dur | None = None
    history_weeks: PosInt | None = None
    sample_per_bucket: PosInt | None = None
    min_requests: NonNeg | None = None
    clock_skew_ms: NonNeg | None = None
    sample_policy: str | None = None
    raw_index: str | None = None
    merge: Merge | None = None
    rules: TraceRules | None = None
    train: Train | None = None
    detect: Detect | None = None
    status: Status | None = None
    metrics: TraceMetrics | None = None


# ── rca.yaml ──

class Indices(Strict):
    anomalies: str
    traces: str | None = None          # metrics_only 沒有履歷
    logs: str | None = None


class Trend(Strict):
    windows: PosInt | None = None
    window: Dur | None = None


class Commonality(Strict):
    resource: list[str] | None = None
    attrs: list[str] | None = None
    min_share: Ratio | None = None
    min_lift: NonNeg | None = None
    min_traces: PosInt | None = None


class Changes(Strict):
    lookback: Dur | None = None
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


class Alerts(Strict):
    """事件整合（service/converge/alert_service.py）＋ 情境策略讀的換版、前後站設定（scenario/it、scenario/factory）。"""
    close_after: Dur | None = None
    lookback: Dur | None = None
    severity: SeverityCfg | None = None
    critical_levels: list[str] | None = None
    deploy_grace: bool | None = None
    deploy_index: str | None = None
    version_field: str | None = None
    deploy_entity_field: str | None = None
    deploy_time_field: str | None = None
    deploy_filter: dict[str, Any] | None = None
    rca: bool | None = None
    rca_lead: Dur | None = None
    rca_retry: bool | None = None
    rca_retry_after: Dur | None = None
    rca_retry_max: PosInt | None = None
    similar: bool | None = None
    similar_weights: dict[str, Num] | None = None
    llm: bool | None = None
    llm_provider: str | None = None
    chain: bool | None = None
    chain_sample: PosInt | None = None
    paths_lookback: Dur | None = None
    feedback_url: str | None = None
    report_url: str | None = None


class IncidentsDb(Strict):
    url: str | None = None
    sql: str | None = None


class RcaConfig(Strict):
    indices: Indices | None = None
    timezone: str | None = None
    compare_with: Dur | None = None
    ref_gap: Dur | None = None
    sample: PosInt | None = None
    interval: Dur | None = None
    entry_lag: Dur | None = None
    trend: Trend | None = None
    commonality: Commonality | None = None
    changes: Changes | None = None
    alerts: Alerts | None = None
    incidents_db: IncidentsDb | None = None


# ── schedule.yaml ──

class Job(Strict):
    """scheduler/runner.Job.from_dict 讀的欄位。"""
    name: str
    cron: str
    args: list[Any]
    timeout: PosInt | None = None
    retries: NonNeg | None = None
    stage: PosInt | None = None
    why: str | None = None
    queue: Literal["realtime", "alert", "batch"] | None = None


class ScheduleConfig(Strict):
    timezone: str | None = None
    jobs: list[Job] | None = None


# ── chronic.yaml ──

class ChronicSource(Strict):
    name: str
    type: Literal["log", "anomalies"]
    title: str | None = None
    index: str | None = None
    filter: Query | None = None
    by: list[str] | None = None
    keys: list[str] | None = None
    min_per_day: NonNeg | None = None
    min_days: PosInt | None = None
    top: PosInt | None = None
    levels: list[str] | None = None
    series: str | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.keys, self.by)
        if self.type == "log" and (self.index is None or not self.by):
            raise ValueError("type log 要有 index、by")
        return self


class ChronicConfig(Strict):
    days: PosInt | None = None
    min_days: PosInt | None = None
    top: PosInt | None = None
    timezone: str | None = None
    sources: list[ChronicSource] | None = None
    owners: list[dict[str, Any]] | None = None
    owners_source: list[Provider] | Provider | None = None


# ── prune.yaml ──

class PruneIndex(Strict):
    index: str
    keep_days: NonNeg
    time_field: str | None = None
    filter: dict[str, Any] | None = None


class PruneConfig(Strict):
    timezone: str | None = None
    indices: list[PruneIndex] | None = None


# ── calendar.yaml ──

class CalendarEntry(Strict):
    """一天（date）或一段（start＋end）；detector/adjust.calendar_adjustments。"""
    date: When | None = None
    start: When | None = None
    end: When | None = None
    name: str | None = None
    type: str | None = None          # holiday | maintenance | event（只是記錄用）
    match: dict[str, Any] | None = None
    series: str | None = None
    detect: bool | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.date is None and (self.start is None or self.end is None):
            raise ValueError("要有 date，或 start＋end")
        return self


class CalendarConfig(Strict):
    timezone: str | None = None
    sources: list[Provider] | None = None
    calendar: list[CalendarEntry] | None = None


# ── adjustments.yaml ──

class AdjustmentsConfig(Strict):
    adjustments: list[Adjustment] | None = None


# ── ingest.yaml ──

class Pick(Strict):
    """{field, map, default}：讀該列的欄位、查表對應（detector/ingest._pick）。"""
    field: str
    map: dict[Any, Any] | None = None
    default: str | None = None


class Input(Strict):
    """一個外部來源（detector/ingest.py）；validate() 另外擋 name／index／series／keys 缺的。"""
    name: str
    index: str
    series: str
    keys: dict[str, str]
    time_field: str | None = None
    filter: Query | None = None
    id_field: str | None = None
    value: str | None = None
    expected: str | None = None
    score: str | None = None
    level: Literal["ml", "fdc", "external"] | None = None
    status: str | Pick | None = None
    severity: str | Pick | None = None
    direction: str | Pick | None = None
    message: str | None = None
    extra: list[str] | None = None


class IngestConfig(Strict):
    timezone: str | None = None
    inputs: list[Input] | None = None


# ── paths.yaml（工廠 plugin）──

class MesFields(Strict):
    lot: str
    equipment: str
    start: str
    step: str | None = None
    station: str | None = None
    chamber: str | None = None
    recipe: str | None = None
    recipe_version: str | None = None
    product: str | None = None
    end: str | None = None
    result: str | None = None


class Mes(Strict):
    index: str
    fields: MesFields
    time_field: str | None = None
    filter: Query | None = None
    bad_results: list[str] | None = None
    attrs: list[str] | None = None


class EventFields(Strict):
    lot: str
    start: str
    equipment: str | None = None
    chamber: str | None = None
    step: str | None = None
    end: str | None = None
    event: str | None = None


class Events(Strict):
    index: str
    fields: EventFields
    time_field: str | None = None
    filter: Query | None = None
    attrs: list[str] | None = None


class PathsConfig(Strict):
    timezone: str | None = None
    group: str | None = None
    group_field: str | None = None
    dept_field: str | None = None
    mes: Mes | None = None
    events: Events | None = None


# kind → 模型（鍵跟 core/config.KINDS 一樣）
MODELS: dict[str, type[Strict]] = {
    "series": SeriesConfig, "rules": RulesConfig, "peer": PeerConfig, "quality": QualityConfig, "trace": TraceConfig,
    "rca": RcaConfig, "schedule": ScheduleConfig, "chronic": ChronicConfig, "prune": PruneConfig, "calendar": CalendarConfig,
    "adjustments": AdjustmentsConfig, "ingest": IngestConfig, "paths": PathsConfig,
}
