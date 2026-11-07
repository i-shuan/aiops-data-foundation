"""PipelineMetadata（對應 @superset-ui/core 的 chart/models/ChartMetadata.ts）：外掛的自我描述，沒有 thumbnail。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineMetadata:
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    category: str = ""
    behaviors: tuple[str, ...] = ()        # 例如 "needs_deploy_time"
    credits: tuple[str, ...] = ()
    deprecated: bool = False
