"""peer.yaml：Pod 同儕橫向比對。"""
from __future__ import annotations

from pydantic import model_validator

from shared.config.common import _same_length, Direction, Dur, NonNeg, PosInt, Query, Severity, Show, Strict, Value


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
