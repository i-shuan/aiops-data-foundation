"""設定檔的載入與驗證（pydantic）。指令只透過 load_config 讀 yaml，不自己 yaml.safe_load。

這一版是過渡的空殼：只讀 yaml、原樣回傳 dict。模型與驗證在同一條分支補上。"""
from __future__ import annotations

import yaml

KINDS = ("series", "rules", "peer", "quality", "trace", "rca", "schedule", "chronic", "prune", "calendar", "adjustments", "ingest", "paths")


def load_config(path: str, kind: str) -> dict:
    """讀一份設定檔。kind 是哪一種（KINDS），之後用它挑模型驗證。回傳 dict，鍵值跟 yaml 一樣。"""
    if kind not in KINDS:
        raise ValueError(f"不認得的設定種類 {kind!r}，只能是 {'、'.join(KINDS)}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
