"""peer.yaml：Pod 同儕橫向比對。"""
from __future__ import annotations

from pydantic import Field, model_validator

from common.config.common import _same_length, Direction, Dur, NonNeg, PosInt, Query, Severity, Show, Strict
from detect.config.types import Value


# ── peer.yaml ──

class PeerCheck(Strict):
    """一個同儕比對（detector/peer.py、cmd_peer.py）：查法跟序列一樣，多了 min_volume、max_peer_median。"""
    name: str
    index: str
    by: list[str]
    value: Value
    title: str | None = None
    severity: Severity = "warning"
    show: Show | None = None
    key_names: list[str] | None = Field(default=None, alias="keys")   # yaml 的鍵叫 keys；屬性不能叫 keys（會蓋掉過渡層 dict 介面的 keys()）
    filter: Query | None = None
    time_field: str | None = None
    scale: NonNeg | None = None
    direction: Direction = "up"
    min_abs: NonNeg = 0.0
    min_ratio: NonNeg | None = None
    min_volume: NonNeg | None = None
    max_peer_median: NonNeg | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.key_names, self.by)
        return self


class PeerConfig(Strict):
    window: Dur = "10m"                      # 比對看最近多久
    z: NonNeg = 5.0
    z_by_peers: dict[int, NonNeg] = {}       # {同儕數: z}，同儕少時放寬
    rel_floor: NonNeg = 0.1
    min_peers: PosInt = 3
    fresh: Dur = "2m"                        # 最近這麼久內還有資料的才算現在還在的 Pod
    warmup: Dur | None = None                # 剛起來的 Pod 不比
    checks: list[PeerCheck] = []
