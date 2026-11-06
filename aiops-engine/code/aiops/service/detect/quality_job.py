"""資料品質檢查（detector/quality.py、configs/baseline/quality.yaml）。"""
from __future__ import annotations

import pandas as pd

from aiops.core.entity.anomaly import Anomaly
from aiops.service.detect.base import Detector
import aiops.service.detect.quality as Q
from aiops.service.detect._util import _freq, _v


def quality_hits(es, c: dict, now, window, ref_offset) -> list[dict]:
    """跑一個檢查，回傳觸發的對象（Q.eval_* 的結果）。"""
    start, rs, re_ = now - window, now - window - ref_offset, now - ref_offset
    keys, t = Q._keys(c), c["type"]
    if t == "cardinality":
        return Q.eval_cardinality(Q.fetch_cardinality(es, c, start, now, c.get("sample_values", 500)),
                                  Q.fetch_cardinality(es, c, rs, re_, 0), keys,
                                  max_values=c.get("max_values"), ratio=c.get("ratio", 3.0),
                                  min_increase=c.get("min_increase", 50), id_share_min=c.get("id_share", 0.5))
    if t == "missing":
        return Q.eval_missing(Q.fetch_missing(es, c, start, now), Q.fetch_missing(es, c, rs, re_), keys, c["fields"],
                              min_docs=c.get("min_docs", 100), max_missing=c.get("max_missing"),
                              max_increase=c.get("max_increase"))
    if t == "stopped":
        silent = pd.Timedelta(_freq(c.get("silent", "10m")))
        lookback = pd.Timedelta(_freq(c.get("lookback", "1h")))
        return Q.eval_stopped(Q.fetch_counts(es, c, now - lookback, now - silent), Q.fetch_counts(es, c, now - silent, now),
                              keys, min_docs=c.get("min_docs", 10))
    if t == "present":
        return Q.eval_present(Q.fetch_counts(es, c, start, now), keys, min_docs=c.get("min_docs", 1))
    if t == "share":
        return Q.eval_share(Q.count(es, c["part_index"], c, start, now), Q.count(es, c["index"], c, start, now),
                            Q.count(es, c["part_index"], c, rs, re_), Q.count(es, c["index"], c, rs, re_),
                            min_docs=c.get("min_docs", 100), max_share=c.get("max_share"),
                            min_increase=c.get("min_increase", 0.1))
    if t == "lag":
        return Q.eval_lag(Q.fetch_lag(es, c, start, now), keys, stat=c.get("stat", "p95"),
                          max_seconds=c.get("max_seconds", 60), min_docs=c.get("min_docs", 50),
                          absolute=c.get("absolute", False))
    raise ValueError(f"不認得的檢查種類：{t}")


class QualityDetector(Detector):
    level = "quality"
    interval_default = "10m"

    def checks(self) -> list[dict]:
        self.ref_offset = pd.Timedelta(_freq(self.cfg.get("ref_offset", "24h")))
        return self.cfg["checks"]

    def evaluate(self, c: dict) -> list[Anomaly]:
        window = pd.Timedelta(_freq(c.get("window", self.cfg.get("window", "1h"))))
        out = []
        for h in quality_hits(self.es, c, self.now, window, self.ref_offset):
            keys = {**{k: v for k, v in h["key"].items() if _v(v) is not None},   # NaN 寫進 ES 會整批被拒
                    **({"field": h["field"]} if "field" in h else {})}
            out.append(Anomaly(ts=self.slot, series=c["name"], rule=c["name"], keys=keys, value=h["value"],
                               median=_v(h.get("ref")), level=self.level, direction="up",
                               severity=c.get("severity", "warning"), message=Q.describe(c, h)))
        return out


def cmd_quality(cfg: dict, args, backend=None) -> int:
    return QualityDetector(cfg, args, backend).run()
