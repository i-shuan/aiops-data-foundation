"""領域用語的門面：對象叫什麼（服務／機台）、一個單位叫什麼（請求／lot）、回饋表單的分類表、誤報原因、常查的地方。
內容在 plugin 的 domain()（core/plugin.py；plugins/observability、plugins/factory）。報告、LLM 範本、feedback_web 都從這裡拿。
"""
from __future__ import annotations

from . import plugin


def name() -> str:
    return plugin.current().name


def profile(section: str):
    return plugin.current().domain()[section]


def categories() -> list[tuple[str, str]]:
    return list(profile("categories"))


def category_codes() -> tuple[str, ...]:
    return tuple(c for c, _ in categories())


def fp_reasons() -> list[tuple[str, str]]:
    return list(profile("fp_reasons"))


def checked() -> list[str]:
    return list(profile("checked"))


def entity_label() -> str:
    """對象叫什麼：服務／機台。"""
    return profile("entity")


def unit_label() -> str:
    """被追蹤的一個單位叫什麼：請求／lot（報告、摘要的措辭）。"""
    return profile("unit")


def unit_text() -> str:
    """unit_label 放進中文句子用：英文的（lot）前後補空格，「每個 lot 多花」。"""
    u = unit_label()
    return f" {u} " if u.isascii() else u


def trace_label() -> str:
    """把一個單位串起來的那份資料叫什麼：Trace／履歷。"""
    return profile("trace")


def new_value_hint() -> str:
    """共同點是「平常沒見過的值」時，提示可能是什麼。"""
    return profile("new_value_hint")
