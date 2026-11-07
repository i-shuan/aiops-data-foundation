"""tests/test_blocks.py 用的「自己寫的積木」（情境資料檔填 tests.blocks_fixtures:名字）。"""
from __future__ import annotations

CALLS: list[str] = []


def register(app):
    """④ 自己寫的判斷：加一個子命令。"""
    @app.command("my-check")
    def my_check() -> int:
        print("my-check 跑了")
        return 0


def always_critical(ctx, g):
    """⑥ 嚴重度：不管前面評什麼，都改成 critical。"""
    return {**(g or {}), "severity": "critical", "why": ((g or {}).get("why", "") + "；自己寫的積木：一律 critical").lstrip("；")}


def merge(*args, **kwargs):
    """⑥ 併件：記下被叫了，回空的（這一輪不開件）。"""
    CALLS.append("merge")
    return []


def by_ticket(repo, fp, text, exclude_id, opts):
    """⑦ 相似事故：固定給一件（不看指紋），相似度 0.9。"""
    return [("t-1", {"status": "confirmed", "title": "工單系統來的", "root_cause_category": "network", "fingerprint": {}}, 0.9)]


def my_section(c):
    """⑦ 報告段落。"""
    c.out += ["## 自己寫的段落", ""]


def my_hint(findings, cases, cls, scenario):
    return ["## 自己寫的線索", ""]


def my_suggest(ctx):
    CALLS.append("suggest")
    return 7
