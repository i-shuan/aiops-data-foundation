"""對應 chart/registries/ChartMetadataRegistrySingleton.ts。key＝`<段>.<名字>`。"""
from __future__ import annotations

from core.models.registry import Registry
from core.pipeline.models.metadata import PipelineMetadata

_registry: Registry[PipelineMetadata] = Registry("PipelineMetadataRegistry")


def get_pipeline_metadata_registry() -> Registry[PipelineMetadata]:
    return _registry
