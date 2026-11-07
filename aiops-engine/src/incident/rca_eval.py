"""根因分析的考卷：改了根因分析的程式之後，拿已確認的事故重跑一次，看「慢在哪一段、錯從哪開始」還對不對得上
（docs/pdca/meta/2026-11-cases-tier-drop.md）。

答案是事故的指紋（aiops-incidents.fingerprint 的 slow_segments、error_origins）：自動開的事故是當時根因分析算的、老師傅確認過
（status confirmed）或改過（corrected）；匯入的是人填的。這次重跑的結果也做成指紋（Fingerprint.from_alert，同一套正規化），
答案的段有一個出現在這次的前三段就算對。
- 沒有答案（指紋裡沒有段）的事故不考；重跑時已經沒有 trace（過了保留期）的列成「略過」，不算錯。
- 結果可以存成檔（--save），下次 --compare 比：上次對、這次錯的列出來（程式改壞了的地方）。
"""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timedelta

from common.clock import parse_instant
from common.types.incident import Fingerprint, seg_name
from llm.similar import CONFIRMED


def _when(v) -> datetime | None:
    try:
        return parse_instant(v).to_pydatetime() if v else None
    except (ValueError, TypeError):
        return None


def exam(cases: list[tuple[str, dict]], investigate, rca_config, *, pad: timedelta = timedelta(minutes=5),
         default_len: timedelta = timedelta(minutes=30)) -> dict:
    """cases：已確認的事故 [(id, 文件)]；investigate(rca_config, args) → (報告, findings)（rca_report.investigate_full；測試給假的）。
    每件用事故的第一個服務、start − pad ～ end + pad（沒有 end 用 start + default_len）重跑。
    回傳 {"n", "scored", "right", "skipped", "accuracy", "rows": [{id, title, status: right／wrong／skipped／no_answer, want, got, why}]}。"""
    rows = []
    for _id, doc in cases:
        fp = Fingerprint.of(doc.get("fingerprint"))
        want = {"slow": [seg_name(s) for s in fp.slow_segments], "error": [seg_name(s) for s in fp.error_origins]}   # 匯入的、舊的可能沒去數字
        row = {"id": _id, "title": doc.get("title"), "want": want, "got": None}
        start, end = _when(doc.get("start")), _when(doc.get("end"))
        services = doc.get("services") or fp.services
        if not (want["slow"] or want["error"]):
            rows.append({**row, "status": "no_answer", "why": "指紋裡沒有慢的段、錯的源頭（不考）"})
            continue
        if not start or not services:
            rows.append({**row, "status": "skipped", "why": "事故沒有開始時間或服務"})
            continue
        try:
            _, findings = investigate(rca_config, Namespace(service=services[0], route=None, start=start - pad,
                                                            end=(end or start + default_len) + pad, at=None, window=None))
        except Exception as e:  # noqa: BLE001  一件跑壞不擋整份考卷
            rows.append({**row, "status": "skipped", "why": f"根因分析失敗：{type(e).__name__}: {str(e)[:100]}"})
            continue
        if not ((findings or {}).get("requests") or {}).get("requests"):
            rows.append({**row, "status": "skipped", "why": "這段時間已經沒有 trace（過了保留期？）"})
            continue
        got_fp = Fingerprint.from_alert({"service": services[0]}, findings)
        got = {"slow": got_fp.slow_segments, "error": got_fp.error_origins}
        hit = [s for k in ("slow", "error") for s in want[k] if s in got[k]]
        rows.append({**row, "got": got, "status": "right" if hit else "wrong",
                     "why": f"對到 {'、'.join(hit)}" if hit else "答案的段不在這次的前三段"})
    scored = [r for r in rows if r["status"] in ("right", "wrong")]
    right = sum(r["status"] == "right" for r in scored)
    return {"n": len(rows), "scored": len(scored), "right": right, "skipped": sum(r["status"] == "skipped" for r in rows),
            "accuracy": (right / len(scored)) if scored else None, "rows": rows}


def compare(before: dict, now: dict) -> dict:
    """上次的考卷（存的檔）vs 這次：{"worse": [上次對、這次錯], "better": [上次錯、這次對]}。略過的不比。"""
    old = {r["id"]: r["status"] for r in before.get("rows") or []}
    worse = [r for r in now["rows"] if old.get(r["id"]) == "right" and r["status"] == "wrong"]
    better = [r for r in now["rows"] if old.get(r["id"]) == "wrong" and r["status"] == "right"]
    return {"worse": worse, "better": better}


def run(repo, rca_config, investigate, save: str | None = None, against: str | None = None) -> tuple[int, list[str]]:
    """讀已確認的事故、考一次、印出來；save：存成檔；against：跟上次的檔比，有變差的回 1。回 (結束碼, 印出來的行)。"""
    r = exam(repo.incidents.find(CONFIRMED, having=("fingerprint",)), investigate, rca_config)
    mark = {"right": "✓", "wrong": "✗", "skipped": "－", "no_answer": "·"}
    lines = [f"  {mark[x['status']]} {x['id'][:24]:<24} {x['title'] or ''}：{x['why']}" for x in r["rows"]]
    acc = f"{r['right']}/{r['scored']}（{r['accuracy']:.0%}）" if r["scored"] else "沒有能考的事故"
    lines.append(f"定位對得上：{acc}；略過 {r['skipped']} 件（沒有 trace、資料不在了），共 {r['n']} 件已確認的事故")
    code = 0
    if against:
        with open(against, encoding="utf-8") as f:
            diff = compare(json.load(f), r)
        lines += [f"  變差：{x['id'][:24]} {x['title'] or ''}（上次對、這次 {x['why']}）" for x in diff["worse"]]
        lines += [f"  變好：{x['id'][:24]} {x['title'] or ''}" for x in diff["better"]]
        lines.append(f"跟 {against} 比：變差 {len(diff['worse'])} 件、變好 {len(diff['better'])} 件")
        code = 1 if diff["worse"] else 0
    if save:
        with open(save, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1, default=str)
        lines.append(f"存到 {save}（下次用 --compare {save} 比）")
    return code, lines
