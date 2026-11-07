"""文字 → 向量（相似事故的向量檢索，docs/pdca/meta/2026-11-postgres-store.md）。

用設定的 OpenAI 相容 API：POST {EMBEDDING_URL}/embeddings，{"model": EMBEDDING_MODEL, "input": [文字…]}（公司的 LLM 閘道、
OpenAI、自架的 embedding 服務都是這個格式）。沒設 EMBEDDING_URL、EMBEDDING_MODEL 就不算向量，相似事故只用欄位比對。
事故的文字（incident_text）與新告警的文字（alert_text）都只用「看得到的」欄位：標題、根因、處置、怎麼看出來的、慢的段、錯的源頭、
log 樣式、服務、指標——跟欄位比對用的是同一批東西，向量只是補「說法不同但意思一樣」。
"""
from __future__ import annotations

import hashlib
import math

import requests

from common.config.settings import settings

BATCH = 64          # 一次送幾段文字


def configured() -> bool:
    env = settings()
    return bool(env.embedding_url and env.embedding_model)


def model_name() -> str | None:
    return settings().embedding_model


def embed(texts: list[str]) -> list[list[float]]:
    """每段文字一個向量（跟 texts 同順序）。API 回錯就丟 RuntimeError（呼叫端決定要不要退回只用欄位比對）。"""
    env = settings()
    if not configured():
        raise RuntimeError("沒設 EMBEDDING_URL、EMBEDDING_MODEL")
    headers = {"content-type": "application/json", **({"Authorization": f"Bearer {env.embedding_api_key}"} if env.embedding_api_key else {})}
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        part = texts[i:i + BATCH]
        r = requests.post(f"{env.embedding_url.rstrip('/')}/embeddings", headers=headers, timeout=60,
                          json={"model": env.embedding_model, "input": part})
        if r.status_code != 200:
            raise RuntimeError(f"embedding API 回 {r.status_code}：{r.text[:200]}")
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(part):
            raise RuntimeError(f"embedding API 回了 {len(data)} 個向量，送了 {len(part)} 段")
        out += [[float(x) for x in d["embedding"]] for d in data]
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _join(*parts) -> str:
    lines = []
    for label, v in parts:
        if isinstance(v, (list, tuple)):
            v = "、".join(str(x) for x in v if x)
        if v:
            lines.append(f"{label}：{v}")
    return "\n".join(lines)


def incident_text(doc: dict) -> str:
    """一件確認過的事故拿來算向量的文字。"""
    fp = doc.get("fingerprint") or {}
    return _join(("標題", doc.get("title")), ("服務", doc.get("services") or fp.get("services")), ("根因分類", doc.get("root_cause_category")),
                 ("根因", doc.get("root_cause")), ("處置", doc.get("action")), ("怎麼看出來的", doc.get("how_detected")),
                 ("慢的段", fp.get("slow_segments")), ("錯的源頭", fp.get("error_origins")), ("log 樣式", fp.get("patterns")),
                 ("指標", fp.get("series")))


def alert_text(title: str | None, fp: dict, summary: str | None = None) -> str:
    """新的一件拿來查的文字：標題、摘要＋這次根因分析的指紋。"""
    return _join(("標題", title), ("摘要", summary), ("服務", fp.get("services")), ("慢的段", fp.get("slow_segments")),
                 ("錯的源頭", fp.get("error_origins")), ("log 樣式", fp.get("patterns")), ("指標", fp.get("series")))


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()
