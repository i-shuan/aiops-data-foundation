"""tune：調整某條序列、某些對象的粒度、z、min_abs、confirm_points、instant_z（寫進 aiops-tuning；下次 train／detect 生效）；
--show 看最終生效的值與來源。"""
from __future__ import annotations

import pandas as pd

from core.es_client import ES, doc_id
from sources import open_backend
from sources.schema import TUNING_INDEX, TUNING_MAPPING
from detector.adjust import adjust as A
from detector.adjust.tuning import load_overrides

from .detect import score_params, status_params
from .model import DetectOptions, effective_for


def effective_lines(cfg: dict, spec: dict, key: dict, overrides) -> list[str]:
    """tune --show 印的每一行：這條序列、這個對象最終生效的粒度、z、min_abs、confirm_points、instant_z，各自來自哪一筆。
    值一律從 detect 實際用的函式拿（score_params、status_params），來源從 effective_for 拿，兩邊不會各寫一套。"""
    eff = effective_for(spec["name"], key, overrides)
    z, min_abs = score_params(spec, key, DetectOptions(**cfg.get("detect", {})), overrides)
    cp, iz = status_params(cfg, spec, key, overrides)
    # 粒度在 train 時套（apply_resolution_overrides），這裡只能說「有 override 就是它，沒有就是序列寫死的或自動選的」
    res = eff["resolution"]["value"] if "resolution" in eff else spec.get("resolution") or "自動（train 依事件數選，看 aiops-baseline 的 resolution）"
    values = {"resolution": res, "z": z, "min_abs": min_abs, "confirm_points": cp, "instant_z": iz}
    lines = []
    for k, v in values.items():
        if k in eff:
            e = eff[k]
            who = ", ".join(f"{f}={p}" for f, p in (e.get("match") or {}).items()) or "全部"
            src = f"來自 {e['source']}：{e.get('series') or '.*'} {who}"
        else:
            src = "series.yaml 預設"
        lines.append(f"  {k} = {'無' if v is None else v}（{src}）")
    return lines


def cmd_tune(cfg: dict, args) -> int:
    """寫一筆調整進 aiops-tuning（同一條序列＋同一組 match 會覆蓋）；--remove 刪掉；--list 列出；
    --show 看某條序列、某個對象最終生效的值與來源（多筆都符合時後面蓋前面，不然看不出是哪一筆定的）。"""
    es = open_backend(cfg, ES)
    if es.store.kind != "es" and not (args.list or args.show):
        print(f"tune 要寫 aiops-tuning，store 是 {es.store.kind}（不是 ES）：直接改 series.yaml 的 overrides")
        return 2
    es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
    if args.list:
        for o in load_overrides(es, cfg):
            print(A.describe(o))
        return 0
    match = dict(m.split("=", 1) for m in (args.match or []))
    if args.show:
        if not args.series or not match:
            print("--show 要給 --series（series.yaml 的名稱）和 --match（對象的實際值，例如 service=mes-api）")
            return 2
        spec = next((s for s in cfg["series"] if s["name"] == args.series), None)
        if spec is None:
            print(f"series.yaml 沒有這條序列：{args.series}")
            return 2
        print(f"{args.series} {match} 現在生效的值：")
        for line in effective_lines(cfg, spec, match, load_overrides(es, cfg)):
            print(line)
        return 0
    _id = doc_id("tune", args.series, match)
    if args.remove:
        es.req("DELETE", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", ok=(200, 404))
        print(f"已刪除：{args.series} {match}")
        return 0
    doc = {"kind": "override", "series": args.series, "match": match, "resolution": args.resolution, "z": args.z, "min_abs": args.min_abs,
           "confirm_points": args.confirm_points, "instant_z": args.instant_z,
           "reason": args.reason, "by": args.by, "at": pd.Timestamp.now(tz="UTC").isoformat()}
    es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", {k: v for k, v in doc.items() if v is not None})
    print(f"已調整：{args.series} {match} → " + "，".join(f"{k}={doc[k]}" for k in ("resolution", "z", "min_abs", "confirm_points", "instant_z") if doc[k] is not None)
          + "（粒度下次 train 生效；z、min_abs、confirm_points、instant_z 下次 detect 生效）")
    return 0
