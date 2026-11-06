"""adjust：排除時段、重置點、週期放寬（detector/adjust.py 套用；status: proposed 的要 approve 才生效）。
四個子命令各一個函式：list 列出全部（含設定檔、tune 的）；approve 把 proposed 改成 active（learning loop 提的建議，人核准才生效）；
remove 刪掉；add 寫一筆進 aiops-tuning。"""
from __future__ import annotations

import pandas as pd

from aiops.core.entity.adjustment import TUNING_INDEX, TUNING_MAPPING, validated
import aiops.service.detect.adjustments as A
from aiops.service.detect.adjustments import load_adjustments
from aiops.service.detect.es_facade import ES, doc_id

# add 每種 kind 必填的參數
REQUIRED = {"exclude_window": ("start", "end"), "reset_point": ("at",), "recurring_override": ("cron", "duration")}


def _connect() -> ES:
    es = ES()
    es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
    return es


def adjust_list(cfg: dict, args) -> int:
    for a in load_adjustments(_connect(), cfg):
        bt = a.get("backtest")
        tail = f"  ｜回測：{'建議核准' if bt.get('recommended') else '不建議'}，{bt.get('reason')}" if bt else ("  ｜還沒回測" if a.get("status") == "proposed" else "")
        print(f"{a.get('id') or '（設定檔）'}  {A.describe(a)}{tail}")
    return 0


def adjust_remove(cfg: dict, args) -> int:
    if not args.id:
        print("要給 --id（adjust list 看得到）")
        return 2
    _connect().req("DELETE", f"/{TUNING_INDEX}/_doc/{args.id}?refresh=true", ok=(200, 404))
    print(f"已刪除：{args.id}")
    return 0


def _approve_blocked(src: dict, _id: str, force: bool) -> str | None:
    """不能核准的原因（None 就可以）：規則算不出門檻；override／recurring_override／rule_suggestion 沒回測、回測不建議、整組裡多餘的（--force 跳過回測）。
    規則一核准就每次直接報（不等 baseline），誤報的代價更高：一樣要回測過。"""
    bt = src.get("backtest")
    if src.get("kind") == "rule_suggestion" and not isinstance((src.get("rule_draft") or {}).get("threshold"), (int, float)):
        return f"這條規則算不出門檻（{(src.get('rule_draft') or {}).get('_note')}），不能核准；要的話自己寫進 rules.yaml"
    if src.get("kind", "override") not in ("override", "recurring_override", "rule_suggestion") or force:
        return None
    if not bt:
        return f"這筆還沒回測：先跑 backtest --id {_id}（要跳過用 --force）"
    if not bt.get("recommended"):
        return f"回測不建議核准：{bt.get('reason')}（硬要核准用 --force）"
    if (bt.get("ablation") or {}).get(_id) == "redundant":
        return f"回測整組有效，但這筆拿掉也一樣（多餘）：{bt.get('reason')}（硬要核准用 --force）"
    return None


def adjust_approve(cfg: dict, args) -> int:
    if not args.id:
        print("要給 --id（adjust list 看得到）")
        return 2
    es = _connect()
    src = es.req("GET", f"/{TUNING_INDEX}/_doc/{args.id}", ok=(200, 404)).get("_source") or {}
    why = _approve_blocked(src, args.id, bool(getattr(args, "force", False)))
    if why:
        print(why)
        return 2
    es.req("POST", f"/{TUNING_INDEX}/_update/{args.id}?refresh=true",
           {"doc": {"status": "active", "approved_by": args.by, "approved_at": pd.Timestamp.now(tz="UTC").isoformat()}})
    bt = src.get("backtest")
    if src.get("kind") == "rule_suggestion":
        print(f"已核准：{args.id}（規則 {src['rule_draft']['name']}，下一次 detector rules 就會載入）")
    else:
        print(f"已核准：{args.id}" + (f"（回測：{bt.get('reason')}）" if bt else "（沒回測，--force）"))
    return 0


def _kind_fields(args, tz: str) -> dict | str:
    """三種 kind 各自的欄位；回字串就是參數不對的訊息。"""
    if args.kind == "exclude_window":
        return {"start": A.local_iso(args.start, tz), "end": A.local_iso(args.end, tz), "apply_to": args.apply_to.split(",")}
    if args.kind == "reset_point":
        return {"reset_at": A.local_iso(args.at, tz), "warmup": args.warmup}
    sched = {"cron": args.cron, "duration": args.duration}
    if args.days_of_month:
        sched["days_of_month"] = [int(d) for d in args.days_of_month.split(",")]
    effect = {k: v for k, v in (("z", args.z), ("min_abs", args.min_abs), ("mute", args.mute or None), ("train", args.train)) if v is not None}
    if not ({"z", "min_abs", "mute"} & set(effect)):
        return "recurring_override 要給 --z、--min-abs 或 --mute"
    return {"schedule": sched, "effect": effect}


def adjust_add(cfg: dict, args) -> int:
    missing = [f"--{f.replace('_', '-')}" for f in REQUIRED[args.kind] if not getattr(args, f)]
    if missing:
        print(f"{args.kind} 要給 {'、'.join(missing)}")
        return 2
    match = dict(m.split("=", 1) for m in (args.match or []))
    # 沒寫時區的時間當成當地時間（A.local_iso）：值班寫「凌晨 2 點」不會變成 UTC 的 2 點
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    doc = {"kind": args.kind, "series": args.series, "match": match, "status": args.status, "source": args.source,
           "reason": args.reason, "by": args.by, "expires_at": A.local_iso(args.expires, tz),
           "at": pd.Timestamp.now(tz="UTC").isoformat()}
    fields = _kind_fields(args, tz)
    if isinstance(fields, str):
        print(fields)
        return 2
    doc.update(fields)
    doc = validated({k: v for k, v in doc.items() if v not in (None, {}, [])} | {"match": match})
    _id = args.id or doc_id(args.kind, args.series, match, doc.get("start"), doc.get("reset_at"), doc.get("schedule"))
    _connect().req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", doc)
    print(f"已寫入 {_id}：{A.describe(doc)}")
    if doc["status"] == "proposed":
        print("  status 是 proposed，要 adjust approve --id 之後才生效")
    else:
        print("  不學的部分下次 train 生效；不報的部分下次 detect 生效")
    return 0
