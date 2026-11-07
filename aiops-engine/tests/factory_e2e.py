"""工廠版（半導體）假資料的端到端測試（docs/pdca/meta/2026-11-factory-engine.md）。

假資料：5 台機台、一條路徑 LITHO-01 → ETCH-01（CH-A／CH-B）→ CVD-01 → CMP-01 → METRO-01，每 15 分鐘一個 lot、7 天。
  事故 A：ETCH-01 CH-B 壓力漂移（08:30～11:30 經過 CH-B 的 6 個 lot）：ML 報 fdc.pressure、fdc.rf_power，FDC 鳴 OOC→OOS，
          機台 alarm ALID 2041；那幾個 lot 到 METRO-01 量測 CD 偏（ML 報 fdc.cd_bias）、被 hold；事故前 08:35 ETCH-01 CH-B 剛做完 PM。
  事故 B：CVD-01 10:50 換 recipe v12→v13，之後 25 分鐘內溫度、壓力跳 → 應該是 warning（換版後上限）。
  雜訊：幾筆 weak 的 ML 跳點（不開件）、CMP-01 07:00 一筆單點。
期望：ETCH-01 一件 critical（FDC OOS），METRO-01 的異常併進它（upstream_services）；報告慢在 ETCH-01 › CH-B、共同點 chamber CH-B、
      alarm 新樣式、事故前的 PM；CVD-01 一件 warning（換版）；LITHO-01 沒有件。

執行（在 aiops-engine 底下，要有測試 ES）：
  ES_URL=http://localhost:19200 ES_AUTH=elastic:changeme-test-only python -m tests.factory_e2e [--plugin factory|metrics_only] [--dump 資料夾]
會建立／覆蓋 *-synth 的來源索引、eqp-master，寫 aiops-anomalies（source ml／fdc）、aiops-alerts（這 5 台機台的件）、lot-paths-fab1-*。
--plugin metrics_only：同一批假資料但**不給履歷**（不跑 paths-merge、configs/metrics_only/rca.yaml）：ETCH-01 仍要開 critical、報告只有異常／log／
事故前的變更；METRO-01 沒有履歷併不進前站，自己開一件（已知限制）。--dump 另外把假資料寫成 NDJSON（source: file 離線跑用）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import pandas as pd

os.environ.setdefault("AIOPS_PLUGIN", "factory")       # common/scenario/factory.yaml：機台、recipe、履歷、recipe 變更當換版

TZ = "Asia/Taipei"
TODAY = pd.Timestamp("2026-11-04", tz=TZ)
NOW = TODAY + pd.Timedelta(hours=14)
HISTORY_START = TODAY - pd.Timedelta(days=7)
EQUIPMENTS = {"LITHO-01": ("LITHO", "FAB1-LITHO"), "ETCH-01": ("ETCH", "FAB1-ETCH"), "CVD-01": ("CVD", "FAB1-THIN"),
              "CMP-01": ("CMP", "FAB1-CMP"), "METRO-01": ("METRO", "FAB1-METRO")}
# (機台, recipe, 加工分鐘, chamber 清單)；站與站之間搬運 10 分鐘
ROUTE = [("LITHO-01", "REC-LITHO-1", 30, ["CH-A"]), ("ETCH-01", "REC-ETCH-1", 30, ["CH-A", "CH-B"]), ("CVD-01", "REC-CVD-1", 45, ["CH-A"]),
         ("CMP-01", "REC-CMP-1", 20, ["CH-A"]), ("METRO-01", "REC-METRO-1", 15, ["CH-A"])]
TRANSFER = 10
LOT_EVERY = 15
INCIDENT_A = (TODAY + pd.Timedelta(hours=8, minutes=30), TODAY + pd.Timedelta(hours=11, minutes=30))   # ETCH-01 CH-B 開始加工的時間在這段
EXTRA_MIN = 20                                      # 壞 run 多花的分鐘（抽真空慢）
PM_AT = TODAY + pd.Timedelta(hours=8, minutes=35)
RECIPE_CHANGE_AT = TODAY + pd.Timedelta(hours=10, minutes=50)
INDEX = {"ml": "factory-ml-anomalies-synth", "fdc": "factory-fdc-alarms-synth", "mes": "mes-history-synth", "events": "eqp-events-synth",
         "logs": "eqp-logs-synth", "master": "eqp-master"}
PATHS_INDEX = "lot-paths-fab1-*"
# 來源索引的 mapping：字串都是 keyword（lot_id、equipment_id 要精確比對），訊息是 text
SOURCE_MAPPING = {"mappings": {"dynamic_templates": [{"strings": {"match_mapping_type": "string", "mapping": {"type": "keyword"}}}],
                               "properties": {"@timestamp": {"type": "date"}, "track_in": {"type": "date"}, "track_out": {"type": "date"},
                                              "start_time": {"type": "date"}, "end_time": {"type": "date"},
                                              "message": {"type": "text"}, "alarm_text": {"type": "text"}}}}


def _iso(t: pd.Timestamp) -> str:
    return t.tz_convert("UTC").isoformat()


def make() -> dict[str, list[dict]]:
    """回傳 {索引別名: 列}。"""
    import numpy as np
    rng = np.random.default_rng(7)
    mes, events, ml, fdc, logs = [], [], [], [], []
    starts = pd.date_range(HISTORY_START, NOW, freq=f"{LOT_EVERY}min", inclusive="left")
    affected: list[tuple[str, pd.Timestamp]] = []   # (lot, ETCH 出站時間)
    for i, t0 in enumerate(starts):
        lot = f"LOT-{t0:%Y%m%d}-{i % 96:03d}"
        t = t0
        bad_lot = False
        for step_no, (eq, recipe, minutes, chambers) in enumerate(ROUTE, 1):
            if t >= NOW:
                break
            chamber = chambers[i % len(chambers)] if len(chambers) > 1 else chambers[0]
            dur = minutes + float(rng.normal(0, 1.5))
            slow = eq == "ETCH-01" and chamber == "CH-B" and INCIDENT_A[0] <= t < INCIDENT_A[1]
            if slow:
                dur += EXTRA_MIN
                bad_lot = True
            t_in, t_out = t, t + pd.Timedelta(minutes=dur)
            in_progress = t_out > NOW
            result = "ok"
            if eq == "METRO-01" and bad_lot and not in_progress:
                result = "hold"
            rv = "v13" if (eq == "CVD-01" and t_in >= RECIPE_CHANGE_AT) else "v12"
            row = {"lot_id": lot, "step_no": step_no, "station": eq.split("-")[0], "equipment_id": eq, "chamber_id": chamber, "recipe_id": recipe,
                   "recipe_version": rv, "product_id": "P-100", "track_in": _iso(t_in), "result": result,
                   "operator": f"OP{(i % 5) + 1}", "carrier_id": f"FOUP{(i % 40):03d}", "material_lot": f"MAT-{(i // 20) % 7}",
                   "dept": EQUIPMENTS[eq][1], "@timestamp": _iso(t_in)}
            if not in_progress:
                row["track_out"] = _iso(t_out)
            mes.append(row)
            # S6F11：站裡面的加工段（chamber 的 ProcessStart → ProcessEnd），比站短一點
            ps, pe = t_in + pd.Timedelta(minutes=3), min(t_out, NOW) - pd.Timedelta(minutes=2)
            if pe > ps:
                events.append({"lot_id": lot, "equipment_id": eq, "chamber_id": chamber, "recipe_step": "MAIN", "ceid": "PROCESS",
                               "start_time": _iso(ps), "end_time": _iso(pe), "wafer_count": 25, "@timestamp": _iso(ps)})
            if slow and not in_progress:
                affected.append((lot, t_out))
                k = len(affected)
                run = f"R-{lot}-{step_no}"
                ml.append({"@timestamp": _iso(t_out), "run_id": f"{run}-pressure", "equipment_id": eq, "chamber_id": chamber, "recipe_id": recipe,
                           "step_id": "MAIN", "lot_id": lot, "parameter": "pressure", "value": round(12.0 + k * 0.8, 2), "expected": 9.5,
                           "score": round(6.5 + k * 0.5, 1), "is_anomaly": True, "grade": "strong", "direction": "up"})
                ml.append({"@timestamp": _iso(t_out), "run_id": f"{run}-rf_power", "equipment_id": eq, "chamber_id": chamber, "recipe_id": recipe,
                           "step_id": "MAIN", "lot_id": lot, "parameter": "rf_power", "value": round(310 + k * 4, 1), "expected": 300.0,
                           "score": 5.6, "is_anomaly": True, "grade": "strong", "direction": "up"})
                fdc.append({"@timestamp": _iso(t_out - pd.Timedelta(minutes=5)), "alarm_id": f"A-{lot}", "equipment_id": eq, "chamber_id": chamber,
                            "recipe_id": recipe, "step_id": "MAIN", "lot_id": lot, "parameter": "pressure", "value": round(12.0 + k * 0.8, 2),
                            "target": 9.5, "limit": 12.0, "alarm_level": "OOS" if k >= 3 else "OOC", "direction": "up"})
                logs.append({"@timestamp": _iso(t_in + pd.Timedelta(minutes=8)), "equipment_id": eq, "level": "alarm", "event_type": "alarm",
                             "lot_id": lot, "alarm_id": 2041, "message": f"ALID 2041 Chamber B pressure control deviation, lot {lot}, setpoint 9.5 actual {12.0 + k * 0.8:.1f}"})
            if eq == "METRO-01" and bad_lot and not in_progress:
                ml.append({"@timestamp": _iso(t_out), "run_id": f"R-{lot}-{step_no}-cd", "equipment_id": eq, "chamber_id": chamber, "recipe_id": recipe,
                           "step_id": "MEASURE", "lot_id": lot, "parameter": "cd_bias", "value": 3.8, "expected": 0.2, "score": 6.1,
                           "is_anomaly": True, "grade": "strong", "direction": "up"})
                logs.append({"@timestamp": _iso(t_out), "equipment_id": eq, "level": "warn", "event_type": "mes", "lot_id": lot,
                             "message": f"lot {lot} on hold: CD bias 3.8 nm out of spec"})
            t = t_out + pd.Timedelta(minutes=TRANSFER)
    # 事故 B：CVD-01 換 recipe 後 25 分鐘內溫度、壓力跳（兩類 → 本來是 high，換版上限 warning）
    logs.append({"@timestamp": _iso(RECIPE_CHANGE_AT), "equipment_id": "CVD-01", "level": "info", "event_type": "recipe_change",
                 "recipe_id": "REC-CVD-1", "recipe_version": "v13", "message": "recipe REC-CVD-1 v12 → v13 downloaded (PPID change)"})
    for m, param, val, exp in ((3, "temperature", 412.0, 400.0), (9, "pressure", 2.9, 2.5), (14, "temperature", 415.0, 400.0),
                               (21, "pressure", 3.1, 2.5), (25, "temperature", 418.0, 400.0)):
        at = RECIPE_CHANGE_AT + pd.Timedelta(minutes=m)
        ml.append({"@timestamp": _iso(at), "run_id": f"R-CVD-{m}", "equipment_id": "CVD-01", "chamber_id": "CH-A", "recipe_id": "REC-CVD-1",
                   "step_id": "MAIN", "lot_id": f"LOT-{TODAY:%Y%m%d}-{40 + m:03d}", "parameter": param, "value": val, "expected": exp,
                   "score": 5.8, "is_anomaly": True, "grade": "strong", "direction": "up"})
    # 事故前：ETCH-01 CH-B 剛做完 PM
    logs.append({"@timestamp": _iso(PM_AT), "equipment_id": "ETCH-01", "level": "info", "event_type": "pm", "chamber_id": "CH-B",
                 "message": "PM completed: CH-B throttle valve O-ring replaced, pump-down check skipped"})
    # 雜訊：weak 的跳點（不開件）、CMP-01 一筆單點（開一件 warning、兩小時後自己結束）
    for m, eq in enumerate(("LITHO-01", "LITHO-01", "CMP-01")):
        at = TODAY + pd.Timedelta(hours=9, minutes=10 * m)
        ml.append({"@timestamp": _iso(at), "run_id": f"R-weak-{m}", "equipment_id": eq, "chamber_id": "CH-A", "recipe_id": ROUTE[0][1] if eq == "LITHO-01" else "REC-CMP-1",
                   "step_id": "MAIN", "lot_id": f"LOT-{TODAY:%Y%m%d}-{30 + m:03d}", "parameter": "flow", "value": 101.0, "expected": 100.0,
                   "score": 3.2, "is_anomaly": True, "grade": "weak", "direction": "up"})
    ml.append({"@timestamp": _iso(TODAY + pd.Timedelta(hours=7)), "run_id": "R-cmp-single", "equipment_id": "CMP-01", "chamber_id": "CH-A",
               "recipe_id": "REC-CMP-1", "step_id": "MAIN", "lot_id": f"LOT-{TODAY:%Y%m%d}-{27:03d}", "parameter": "pressure", "value": 5.9,
               "expected": 5.0, "score": 5.3, "is_anomaly": True, "grade": "strong", "direction": "up"})
    # 每天幾筆平常的 info log（不是問題）
    for d in range(7):
        for eq in EQUIPMENTS:
            logs.append({"@timestamp": _iso(HISTORY_START + pd.Timedelta(days=d, hours=6)), "equipment_id": eq, "level": "info", "event_type": "state",
                         "message": f"{eq} daily self-check OK"})
    master = [{"equipment_id": eq, "area": area, "dept": dept, "group": area.lower(), "model": f"{area}-X1", "@timestamp": _iso(NOW)}
              for eq, (area, dept) in EQUIPMENTS.items()]
    return {"mes": mes, "events": events, "ml": ml, "fdc": fdc, "logs": logs, "master": master, "_affected": [l for l, _ in affected]}


def dump(data: dict, folder: str) -> None:
    os.makedirs(folder, exist_ok=True)
    for k, rows in data.items():
        if k.startswith("_"):
            continue
        with open(os.path.join(folder, f"{INDEX[k]}.ndjson"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _delete_index(es, name: str) -> None:
    if "*" in name:   # ES 8 預設不讓用萬用字元刪（destructive_requires_name）：先列出再一個一個刪
        r = es.s.get(f"{es.url}/_cat/indices/{name}?h=index&format=json", timeout=60)
        for row in (r.json() if r.ok else []):
            es.s.delete(f"{es.url}/{row['index']}", timeout=60)
        return
    es.s.delete(f"{es.url}/{name}", timeout=60)


def cleanup(es, equipments) -> None:
    for name in INDEX.values():
        _delete_index(es, name)
    _delete_index(es, PATHS_INDEX)
    es.req("POST", "/aiops-anomalies/_delete_by_query?conflicts=proceed&refresh=true", {"query": {"exists": {"field": "source"}}}, ok=(200, 404))
    svc = {"terms": {"service": sorted(equipments)}}
    es.req("POST", "/aiops-alerts/_delete_by_query?conflicts=proceed&refresh=true", {"query": svc}, ok=(200, 404))
    es.req("POST", "/aiops-incidents/_delete_by_query?conflicts=proceed&refresh=true", {"query": {"terms": {"services": sorted(equipments)}}}, ok=(200, 404))


def load(es, data: dict) -> None:
    for k, rows in data.items():
        if k.startswith("_"):
            continue
        name = INDEX[k]
        _delete_index(es, name)
        es.ensure_index(name, SOURCE_MAPPING)
        es.bulk_index(name, [(f"{k}-{i}", r) for i, r in enumerate(rows)])


def run(keep: bool = False, dump_dir: str | None = None, plugin_name: str = "factory") -> int:
    from common.infra.es import ES
    from tests.aiops_es import AiopsES     # engine 自己的資料在 PostgreSQL：aiops-* 的請求轉過去
    from interface.cli.detect import main as detector
    from interface.cli.rca import main as investigator
    os.environ["AIOPS_PLUGIN"] = plugin_name
    full = plugin_name == "factory"          # 有履歷；metrics_only 沒有
    es = AiopsES(ES())
    data = make()
    print(f"假資料：MES {len(data['mes'])} 列、S6F11 {len(data['events'])}、ML {len(data['ml'])}、FDC {len(data['fdc'])}、log {len(data['logs'])}；"
          f"事故 A 影響 {len(data['_affected'])} 個 lot")
    if dump_dir:
        dump(data, dump_dir)
        print(f"NDJSON 寫到 {dump_dir}")
    cleanup(es, EQUIPMENTS)
    load(es, data)
    at = _iso(NOW)
    with open("configs/factory/ingest.yaml", encoding="utf-8") as f, open("configs/factory/paths.yaml", encoding="utf-8") as g, \
            open(f"configs/{plugin_name}/rca.yaml", encoding="utf-8") as h:
        ingest_cfg, paths_cfg, rca_cfg = f.read(), g.read(), h.read()
    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "factory-e2e")
    os.makedirs(tmp, exist_ok=True)
    # 設定檔的索引換成 -synth 的（其他不動）
    for name, body, repl in (("ingest.yaml", ingest_cfg, {"factory-ml-anomalies-*": INDEX["ml"], "factory-fdc-alarms-*": INDEX["fdc"]}),
                             ("paths.yaml", paths_cfg, {"mes-history-*": INDEX["mes"], "eqp-events-*": INDEX["events"]}),
                             ("rca.yaml", rca_cfg, {"eqp-logs-*": INDEX["logs"]})):
        for a, b in repl.items():
            body = body.replace(a, b)
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(body)
    # 1. 履歷（過去 8 天有動的 lot 全部合）；metrics_only 沒有履歷（plugin 也沒有這個子命令）
    if full:
        detector(["paths-merge", "--config", os.path.join(tmp, "paths.yaml"), "--at", at, "--window", "8d"])
    # 2. 接收器（今天的異常全部接）
    detector(["ingest", "--config", os.path.join(tmp, "ingest.yaml"), "--at", at, "--window", "14h"])
    # 3. 事件整合＋根因分析
    investigator(["alert", "--config", os.path.join(tmp, "rca.yaml"), "--at", at])
    es.req("POST", "/aiops-alerts/_refresh")
    hits = es.search("aiops-alerts", {"size": 100, "query": {"terms": {"service": sorted(EQUIPMENTS)}}})["hits"]["hits"]
    alerts = {h["_source"]["service"]: h["_source"] for h in hits}
    paths = es.search(PATHS_INDEX, {"size": 0, "track_total_hits": True})["hits"]["total"]["value"]
    anoms = es.search("aiops-anomalies", {"size": 0, "track_total_hits": True, "query": {"exists": {"field": "source"}}})["hits"]["total"]["value"]
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok)))
        print(f"  {'✅' if ok else '❌'} {name}" + (f"：{detail}" if detail else ""))

    print(f"\n檢查（plugin {plugin_name}）：")
    if full:
        check("履歷：一個 lot 一筆", paths == len({r['lot_id'] for r in data['mes']}), f"{paths} 筆，MES 有 {len({r['lot_id'] for r in data['mes']})} 個 lot")
    else:
        from common.scenario import load_scenario
        check("沒有履歷（metrics_only 沒有 paths-merge、沒合成）", paths == 0 and "paths-merge" not in load_scenario().commands,
              f"{paths} 筆；子命令 {list(load_scenario().commands) or '無'}")
    check("接收器：ML＋FDC 全部進異常總表", anoms == len(data["ml"]) + len(data["fdc"]), f"{anoms} 筆（來源 {len(data['ml']) + len(data['fdc'])}）")
    e = alerts.get("ETCH-01")
    check("ETCH-01 開一件", e is not None and e["status"] == "open", (e or {}).get("title"))
    if e:
        check("ETCH-01 是 critical（FDC OOS）", e.get("severity") == "critical", f"{e.get('severity')}：{e.get('severity_why')}")
        f_ = e.get("findings") or {}
        segs = f_.get("segments") or []
        report = e.get("report") or ""
        if full:
            check("METRO-01 的異常併進 ETCH-01（前後站）", "METRO-01" in (e.get("upstream_services") or []), f"upstream_services={e.get('upstream_services')}")
            check("報告：慢在 ETCH-01 › CH-B", bool(segs) and "ETCH-01" in segs[0]["segment"] and "CH-B" in segs[0]["segment"],
                  f"{segs[0]['segment'] if segs else '—'} +{(segs[0]['delta_ms'] / 60000) if segs else 0:.1f} 分")
            com = f_.get("commonality") or []
            check("報告：壞 lot 的共同點是 chamber CH-B", any(c["dim"] == "chamber" and c["value"].endswith("CH-B") for c in com),
                  "、".join(f"{c['dim']}={c['value']} {c['now_share']:.0%}" for c in com) or "—")
        else:
            check("沒有履歷：METRO-01 併不進前站、自己開一件（已知限制）", "METRO-01" in alerts and not e.get("upstream_services"),
                  f"upstream_services={e.get('upstream_services')}；METRO-01 {'有件' if 'METRO-01' in alerts else '沒件'}")
            check("報告：沒有履歷那幾段、沒有錯誤訊息", "慢在哪一段" not in report and "根因分析失敗" not in report and not segs
                  and f_.get("requests") is None, "；".join(l for l in report.splitlines() if l.startswith("## ")))
        pats = f_.get("new_patterns") or []
        check("報告：新 alarm 樣式 ALID 2041", any("ALID" in p["pattern"] for p in pats), "；".join(p["pattern"][:60] for p in pats[:3]) or "—")
        chg = f_.get("changes") or []
        check("報告：事故前的 PM", any("PM" in c["message"] for c in chg), "；".join(c["message"][:60] for c in chg) or "—")
        if full:
            check("METRO-01 沒有另開一件", "METRO-01" not in alerts, f"有：{alerts['METRO-01']['title']}" if "METRO-01" in alerts else "")
    c = alerts.get("CVD-01")
    check("CVD-01 開一件", c is not None, (c or {}).get("title"))
    if c:
        check("CVD-01 是 warning（換 recipe 後 30 分鐘內）", c.get("severity") == "warning" and "換版" in (c.get("severity_why") or ""),
              f"{c.get('severity')}：{c.get('severity_why')}")
    check("LITHO-01（只有 weak）沒有件", "LITHO-01" not in alerts)
    cmp_ = alerts.get("CMP-01")
    check("CMP-01 單點：warning、已結束", cmp_ is not None and cmp_["severity"] == "warning" and cmp_["status"] == "resolved",
          f"{(cmp_ or {}).get('severity')} {(cmp_ or {}).get('status')}")
    # 4. 回饋 → 匯出給 ML 團隊：CMP-01 那筆標誤報（剛做完 PM），匯出的列要有來源識別碼（run_id）
    if cmp_ and cmp_.get("anomaly_ids"):
        aid = cmp_["anomaly_ids"][0]
        detector(["feedback", "add", "--anomaly", aid, "--verdict", "false_positive", "--by", "e2e", "--note", "pm_just_done", "--alert", cmp_["alert_id"]])
        out = os.path.join(tmp, "feedback-export.ndjson")
        es.req("POST", "/aiops-feedback/_refresh")
        detector(["feedback", "export", "--days", "1", "--out", out])
        with open(out, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        mine = [r for r in rows if r.get("anomaly_id") == aid]
        check("回饋匯出：誤報那筆帶 ML 的 run_id、對象、值", bool(mine) and mine[0]["source_id"] == "R-cmp-single" and mine[0]["keys"].get("equipment") == "CMP-01"
              and mine[0]["verdict"] == "false_positive", json.dumps(mine[0], ensure_ascii=False)[:160] if mine else "沒有那筆")
    if e:
        print("\n── ETCH-01 的通知摘要 ──\n" + (e.get("summary") or ""))
        print("\n── 報告結論 ──")
        import re
        m = re.search(r"## 結論[^\n]*\n(.*)", e.get("report") or "", re.S)
        print(m.group(1).strip() if m else "（沒有結論）")
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 項通過" + (f"；沒過：{'、'.join(bad)}" if bad else ""))
    if not keep:
        pass    # 來源索引留著方便看報告；要清：python -m tests.factory_e2e --clean
    return 1 if bad else 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--keep", action="store_true", help="跑完不清（預設也不清；留著給人看）")
    p.add_argument("--clean", action="store_true", help="只清掉假資料與結果，不跑")
    p.add_argument("--dump", help="假資料另外寫成 NDJSON 到這個資料夾")
    p.add_argument("--plugin", default="factory", choices=["factory", "metrics_only"], help="factory（有履歷，預設）或 metrics_only（只有指標）")
    a = p.parse_args(argv)
    if a.clean:
        from common.infra.es import ES
        from tests.aiops_es import AiopsES
        cleanup(AiopsES(ES()), EQUIPMENTS)
        print("清掉了")
        return 0
    return run(a.keep, a.dump, a.plugin)


if __name__ == "__main__":
    sys.exit(main())
