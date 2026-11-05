"""adjust：排除時段、重置點、週期放寬（detector/adjust/adjust.py；status: proposed 的要 approve 才生效）。"""
from __future__ import annotations

import pandas as pd

from core.es_client import ES, doc_id

from . import adjust as A
from .tuning import TUNING_INDEX, TUNING_MAPPING, load_adjustments


def cmd_adjust(cfg: dict, args) -> int:
    """排除時段、重置點、週期放寬（adjust.py）：add 寫一筆進 aiops-tuning；list 列出全部（含設定檔、tune 的）；
    approve 把 proposed 改成 active（learning loop 提的建議，人核准才生效）；remove 刪掉。"""
    es = ES()
    es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
    if args.action == "list":
        for a in load_adjustments(es, cfg):
            bt = a.get("backtest")
            tail = f"  ｜回測：{'建議核准' if bt.get('recommended') else '不建議'}，{bt.get('reason')}" if bt else ("  ｜還沒回測" if a.get("status") == "proposed" else "")
            print(f"{a.get('id') or '（設定檔）'}  {A.describe(a)}{tail}")
        return 0
    if args.action in ("approve", "remove"):
        if not args.id:
            print("要給 --id（adjust list 看得到）")
            return 2
        if args.action == "remove":
            es.req("DELETE", f"/{TUNING_INDEX}/_doc/{args.id}?refresh=true", ok=(200, 404))
            print(f"已刪除：{args.id}")
        else:
            got = es.req("GET", f"/{TUNING_INDEX}/_doc/{args.id}", ok=(200, 404))
            src = got.get("_source") or {}
            bt = src.get("backtest")
            if src.get("kind") == "rule_suggestion" and not isinstance((src.get("rule_draft") or {}).get("threshold"), (int, float)):
                print(f"這條規則算不出門檻（{(src.get('rule_draft') or {}).get('_note')}），不能核准；要的話自己寫進 rules.yaml")
                return 2
            # 規則一核准就每次直接報（不等 baseline），誤報的代價更高：一樣要回測過
            if src.get("kind", "override") in ("override", "recurring_override", "rule_suggestion") and not getattr(args, "force", False):
                if not bt:
                    print(f"這筆還沒回測：先跑 backtest --id {args.id}（要跳過用 --force）")
                    return 2
                if not bt.get("recommended"):
                    print(f"回測不建議核准：{bt.get('reason')}（硬要核准用 --force）")
                    return 2
                if (bt.get("ablation") or {}).get(args.id) == "redundant":
                    print(f"回測整組有效，但這筆拿掉也一樣（多餘）：{bt.get('reason')}（硬要核准用 --force）")
                    return 2
            es.req("POST", f"/{TUNING_INDEX}/_update/{args.id}?refresh=true",
                   {"doc": {"status": "active", "approved_by": args.by, "approved_at": pd.Timestamp.now(tz="UTC").isoformat()}})
            if src.get("kind") == "rule_suggestion":
                print(f"已核准：{args.id}（規則 {src['rule_draft']['name']}，下一次 detector rules 就會載入）")
            else:
                print(f"已核准：{args.id}" + (f"（回測：{bt.get('reason')}）" if bt else "（沒回測，--force）"))
        return 0
    need = {"exclude_window": ("start", "end"), "reset_point": ("at",), "recurring_override": ("cron", "duration")}
    missing = [f"--{f.replace('_', '-')}" for f in need[args.kind] if not getattr(args, f)]
    if missing:
        print(f"{args.kind} 要給 {'、'.join(missing)}")
        return 2
    match = dict(m.split("=", 1) for m in (args.match or []))
    # 沒寫時區的時間當成當地時間（A.local_iso）：值班寫「凌晨 2 點」不會變成 UTC 的 2 點
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    doc = {"kind": args.kind, "series": args.series, "match": match, "status": args.status, "source": args.source,
           "reason": args.reason, "by": args.by, "expires_at": A.local_iso(args.expires, tz),
           "at": pd.Timestamp.now(tz="UTC").isoformat()}
    if args.kind == "exclude_window":
        doc.update(start=A.local_iso(args.start, tz), end=A.local_iso(args.end, tz), apply_to=args.apply_to.split(","))
    elif args.kind == "reset_point":
        doc["reset_at"] = A.local_iso(args.at, tz)
        doc["warmup"] = args.warmup
    else:
        sched = {"cron": args.cron, "duration": args.duration}
        if args.days_of_month:
            sched["days_of_month"] = [int(d) for d in args.days_of_month.split(",")]
        effect = {k: v for k, v in (("z", args.z), ("min_abs", args.min_abs), ("mute", args.mute or None),
                                    ("train", args.train)) if v is not None}
        if not ({"z", "min_abs", "mute"} & set(effect)):
            print("recurring_override 要給 --z、--min-abs 或 --mute")
            return 2
        doc.update(schedule=sched, effect=effect)
    doc = {k: v for k, v in doc.items() if v not in (None, {}, [])} | {"match": match}
    _id = args.id or doc_id(args.kind, args.series, match, doc.get("start"), doc.get("reset_at"), doc.get("schedule"))
    es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", doc)
    print(f"已寫入 {_id}：{A.describe(doc)}")
    if doc["status"] == "proposed":
        print("  status 是 proposed，要 adjust approve --id 之後才生效")
    else:
        print("  不學的部分下次 train 生效；不報的部分下次 detect 生效")
    return 0
