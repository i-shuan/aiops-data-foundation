"""M4 trace 分段：一個請求經過哪幾段、每段自己花多久、錯從哪一段開始；出事時跟平常比。原理 docs/rca/README.md「三種資料怎麼串」。

輸入 spans（sources/traces.py 的格式，一列一段）：
  {"trace_id", "span_id", "parent", "service", "name", "kind", "target", "start_ms", "dur_ms", "error", "exception", "attrs", "resource"}
  kind：Server／Client／Consumer／Producer／Internal；target：往外呼叫的對象（db、下游服務），沒有就 None；
  parent：上一層的 span_id，入口（Server、Consumer）是 None
  raw 的 ES 文件用 sources.traces.to_span 轉；合成一筆的用 from_merged

用法：
  traces = group_traces(spans)                                   # {trace_id: [span, …]}
  diffs = compare_segments(now_traces, ref_traces)                # 每段：出事時 vs 平常每個請求花多久、呼叫幾次、錯幾個
  df, skews = summarize(samples, traces)                          # 每 (時段, 入口, 段) 的平均（trace 的 baseline 用；samples 是抽到的入口）
  seg = segment(span)；st = self_times(trace)                     # 段的名字、每段自己的時間
"""
from __future__ import annotations

from sources.traces import error_origins, group_traces, root_of, segment, self_times  # noqa: F401
from detector.trace import COLUMNS, KEYS, METRICS, Sampled, fill_missing_segments, new_segments, summarize  # noqa: F401
from investigator.analyze import SegmentDiff, compare_segments, waterfall  # noqa: F401

SPAN_FIELDS = ("trace_id", "span_id", "parent", "service", "name", "kind", "start_ms", "dur_ms")


def check_spans(spans: list[dict]) -> list[dict]:
    """欄位齊不齊；缺的補預設（target、error、exception、attrs、resource）。"""
    out = []
    for i, s in enumerate(spans):
        missing = [f for f in SPAN_FIELDS if f not in s]
        if missing:
            raise ValueError(f"第 {i} 段缺欄位 {missing}：要有 {list(SPAN_FIELDS)}")
        out.append({"target": None, "error": False, "exception": None, "attrs": {}, "resource": {}, "links": [], **s})
    return out


def samples_of(spans: list[dict], bucket) -> list[Sampled]:
    """沒有抽樣時：每個入口（沒有 parent 的段）都當抽到的請求，時段都是 bucket。"""
    return [Sampled(bucket, s["trace_id"], s["span_id"]) for s in spans if not s.get("parent")]
