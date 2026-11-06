"""規則：狀態變化、離極限多遠（detector/rules.py、configs/baseline/rules.yaml）。"""
from __future__ import annotations

import pandas as pd

from core.es import get_field
from core.anomaly import Anomaly
from .base import Detector
from .es import ES, fetch
from . import rules as R
from .common import _freq
from . import adjust as A


def approved_rules(es: ES, cfg: dict, now: pd.Timestamp | None = None) -> list[dict]:
    """老師傅核准的「升級成規則」（aiops-tuning 的 rule_suggestion，status active、expires_at 還沒到、門檻是數字）：
    跟 rules.yaml 的一起跑。now 是這一輪的時間（--at），過期用它比。"""
    try:
        adjs = A.load_adjustments(es, cfg)   # 經模組取，測試才 mock 得到
    except Exception as e:
        print(f"  讀 aiops-tuning 的規則失敗（{e}），只跑 rules.yaml 的")
        return []
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    out = []
    for a in adjs:
        draft = a.get("rule_draft") or {}
        if a.get("kind") != "rule_suggestion" or a.get("status") != "active" or not isinstance(draft, dict) \
                or not isinstance(draft.get("threshold"), (int, float)) or not draft.get("name"):
            continue
        exp = A._ts(a.get("expires_at"))
        if exp is not None and exp <= now:
            continue
        out.append({k: v for k, v in draft.items() if not k.startswith("_")})
    return out


class RulesDetector(Detector):
    level = "rule"

    def checks(self) -> list[dict]:
        extra = approved_rules(self.es, self.cfg, self.now)
        if extra:
            print(f"  從 aiops-tuning 載入 {len(extra)} 條核准的規則：{'、'.join(r['name'] for r in extra)}")
        self.rules = list(self.cfg["rules"]) + extra
        return self.rules

    def evaluate(self, rule: dict) -> list[Anomaly]:
        now = self.now
        start = now - pd.Timedelta(_freq(rule.get("lookback") or self.cfg.get("lookback", "10m")))
        if rule["type"] == "window":   # 整段算一個值（錯誤率、p95）：跟 series.yaml 同一套查法
            hits = R.evaluate_window(rule, fetch(self.es, rule, start, now, None), start)
        else:
            hits = R.evaluate(rule, R.fetch(self.es, rule, start, now), now)
        out = []
        for h in hits:
            keys = {f: v for f, v in zip(rule["by"], h["key"])}
            # 附帶的資訊（服務名稱、節點），根因分析用來對到服務；不參與「同一個對象」的判斷
            keys.update({f: v for f in rule.get("context", []) if (v := get_field(h["last"], f)) is not None})
            out.append(Anomaly(ts=self.slot, series=f"rule.{rule['name']}", rule=rule["name"], keys=keys, value=h["value"],
                               expected_high=rule["threshold"], level=self.level, direction=rule.get("op", ">="),
                               severity=rule.get("severity", "warning"), message=R.describe(rule, h), since=h["since"]))
        return out

    def on_error(self, rule: dict, exc: Exception) -> None:
        # 一條規則壞掉（查詢錯、核准的草稿缺欄位）只跳過那一條，其他照跑、照寫
        self.failed.append(rule["name"])
        print(f"  規則 {rule['name']} 失敗，跳過：{type(exc).__name__}: {str(exc)[:300]}")

    def summary(self) -> str:
        return (f"{self.slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{len(self.rules)} 條規則，觸發 {self.fired} 個"
                + (f"，{len(self.failed)} 條失敗：{'、'.join(self.failed)}" if self.failed else ""))


def cmd_rules(cfg: dict, args, backend=None) -> int:
    return RulesDetector(cfg, args, backend).run()
