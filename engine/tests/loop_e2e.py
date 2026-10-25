"""product-loop 的整合驗證（docker 的 es-t）：
第 1 次事故 → 告警 → 老師傅在回饋網頁確認（db）→ 第 2 次同樣症狀的告警附上相似事故＋LLM 摘要；
月底批次被誤報 3 次 → 都標誤報 → feedback suggest → backtest → adjust approve → 再跑不報；
同根因 3 次 → 升級成規則的建議；similar-eval、metrics。
用模擬資料（aiops-synth，跟 tests/synth_e2e.py 同一種），不經 Collector。

執行（在 engine 底下，對著測試用的 ES；會清掉 aiops-* 的資料）：
  ES_URL=http://<ip>:9200 ES_AUTH=elastic:... LLM_PROVIDER=template python3 -m tests.loop_e2e
約 2 分鐘。結果記在 docs/pdca/2026-10-product-loop.md 的 Check。
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
import numpy as np, pandas as pd
from detector.es import ES
TZ = "Asia/Taipei"; INDEX = "aiops-synth"; SYNTH = "tests/synth.yaml"; RCA = "tests/rca_loop.yaml"
D0 = pd.Timestamp("2026-10-05", tz=TZ)            # 週一；前面 4 週是歷史
H0 = D0 - pd.Timedelta(weeks=4)
SERVICES = {"mes-api": 100.0, "mes-report": 100.0, "eap-gw": 300.0}
PY = sys.executable
fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"：{detail}" if detail and not cond else "")); fails.append(name) if not cond else None
def run(*args, quiet=False):
    r = subprocess.run([PY, "-m", *args], capture_output=True, text=True)
    if not quiet: print("    $ " + " ".join(args)); print("    " + r.stdout.strip().replace("\n", "\n    ")[:1500])
    if r.returncode not in (0,) and "backtest" not in args and "approve" not in args: print("    stderr:", r.stderr[-800:])
    return r
def utc(day, hhmm): return (D0 + pd.Timedelta(days=day, hours=int(hhmm[:2]), minutes=int(hhmm[3:]))).tz_convert("UTC")
def iso(day, hhmm): return utc(day, hhmm).strftime("%Y-%m-%dT%H:%M:%SZ")
# 事件（台北時間）：(服務, 第幾天, 時間, 欄位, 幾個 5 分鐘, 怎麼改)
INCIDENTS = [("mes-api", 0, "10:00", "latency_ms", 4, lambda v: v * 4),      # 第 1 次：DB 慢
             ("mes-api", 1, "10:00", "latency_ms", 4, lambda v: v * 4),      # 第 2 次：同樣症狀
             ("mes-api", 1, "15:00", "latency_ms", 4, lambda v: v * 4)]      # 第 3 次 → 同根因 3 次
BATCH = [("mes-report", 0, "13:00"), ("mes-report", 0, "17:00"), ("mes-report", 1, "09:00")]   # 月底批次：請求 +50（z 約 6）

def make():
    rng = np.random.default_rng(7)
    ts = pd.date_range(H0, D0 + pd.Timedelta(days=2), freq="5min", inclusive="left")
    rows = []
    for svc, peak in SERVICES.items():
        day = (ts.hour >= 8) & (ts.hour < 20); weekend = ts.dayofweek >= 5
        req = np.where(day, peak, peak * 0.2) * np.where(weekend, 0.5, 1.0)
        req = np.maximum(0, req + rng.normal(0, 8, len(ts)))
        lat = np.where(day, 120.0, 80.0) + rng.normal(0, 8, len(ts))
        err = rng.poisson(np.where(day, 2.0, 0.5))
        rows.append(pd.DataFrame({"ts": ts, "service": svc, "requests": req.round(), "latency_ms": lat, "errors": err.astype(float)}))
    df = pd.concat(rows, ignore_index=True)
    for svc, d, hhmm, field, n, fn in INCIDENTS:
        for k in range(n):
            at = D0 + pd.Timedelta(days=d, hours=int(hhmm[:2]), minutes=int(hhmm[3:]) + 5 * k)
            i = df.index[(df.service == svc) & (df.ts == at)]; df.loc[i, field] = df.loc[i, field].map(fn)
    for svc, d, hhmm in BATCH:
        for k in range(2):
            at = D0 + pd.Timedelta(days=d, hours=int(hhmm[:2]), minutes=int(hhmm[3:]) + 5 * k)
            i = df.index[(df.service == svc) & (df.ts == at)]; df.loc[i, "requests"] = 150.0
    return df

def post(form):
    req = urllib.request.Request("http://localhost:18080/api/feedback", data=json.dumps(form).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())
def get(path):
    with urllib.request.urlopen("http://localhost:18080" + path, timeout=30) as r: return r.status, r.read().decode()
def alerts(es, **term):
    q = {"size": 50, "query": {"bool": {"filter": [{"term": {k: v}} for k, v in term.items()]}}, "sort": [{"started_at": "asc"}]}
    return [h["_source"] for h in es.search("aiops-alerts", q)["hits"]["hits"]]

def main():
    es = ES()
    print("== 0. 清掉上次的、寫入模擬資料（4 週歷史＋ 2 天）")
    for idx in ("aiops-synth", "aiops-baseline", "aiops-anomalies", "aiops-alerts", "aiops-incidents", "aiops-feedback", "aiops-tuning"):
        es.req("DELETE", f"/{idx}", ok=(200, 404))
    es.req("PUT", f"/{INDEX}", {"mappings": {"properties": {"@timestamp": {"type": "date"}, "service": {"type": "keyword"},
                                             "requests": {"type": "double"}, "latency_ms": {"type": "double"}, "errors": {"type": "double"}}}})
    df = make()
    lines = []
    for r in df.itertuples():
        lines.append(json.dumps({"index": {"_index": INDEX}})); lines.append(json.dumps({"@timestamp": r.ts.isoformat(), "service": r.service, "requests": r.requests, "latency_ms": r.latency_ms, "errors": r.errors}))
    for i in range(0, len(lines), 10000):
        es.s.post(f"{es.url}/_bulk?refresh=true", data="\n".join(lines[i:i+10000]) + "\n", headers={"Content-Type": "application/x-ndjson"}).raise_for_status()
    print(f"  {len(df)} 筆")
    print("== 1. train（只用前 4 週）")
    run("detector", "train", "--config", SYNTH, "--end", D0.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), quiet=True)
    web = subprocess.Popen([PY, "-m", "feedback_web"], env={**os.environ, "FEEDBACK_PORT": "18080"}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(30):
            try: get("/health"); break
            except Exception: time.sleep(0.5)
        # ── 第 1 次事故 ──
        print("== 2. 第 1 次事故：10-05 10:00 mes-api 延遲 ×4 → detect → alert")
        run("detector", "detect", "--config", SYNTH, "--at", iso(0, "10:20"), "--window", "30min", quiet=True)
        r = run("investigator", "alert", "--config", RCA, "--at", iso(0, "10:20"))
        a1 = alerts(es, service="mes-api")
        check("第 1 件開了（mes-api，open）", len(a1) == 1 and a1[0]["status"] == "open", str([x.get("status") for x in a1]))
        a1 = a1[0]
        check("告警帶回饋網頁連結", a1.get("feedback_url") == f"http://localhost:18080/feedback?alert={a1['alert_id']}", a1.get("feedback_url"))
        check("第 1 件沒有相似事故（知識庫是空的）", not a1.get("similar"))
        inc = es.req("GET", f"/aiops-incidents/_doc/{a1['alert_id']}", ok=(200, 404))
        check("aiops-incidents 有一筆 status auto、帶指紋", inc.get("found") and inc["_source"]["status"] == "auto" and inc["_source"]["fingerprint"]["services"] == ["mes-api"], json.dumps(inc.get("_source", {}).get("fingerprint")))
        hb = es.req("GET", "/aiops-alerts/_doc/heartbeat", ok=(200, 404))
        check("心跳寫了（_id heartbeat，status ok）", hb.get("found") and hb["_source"]["status"] == "ok")
        print("== 3. 老師傅開回饋網頁、確認（db）")
        st, html = get(f"/feedback?alert={a1['alert_id']}")
        check("表單預填標題與摘要", st == 200 and a1["title"] in html and "怎麼看出來的" in html)
        st, rep = get(f"/report?alert={a1['alert_id']}")
        check("報告全文頁", st == 200 and "根因" in rep)
        r = post({"alert_id": a1["alert_id"], "verdict": "true_positive", "by": "老王", "root_cause_category": "db", "root_cause": "Oracle 統計資訊過期，SELECT lots 走錯執行計畫",
                  "action": "重跑 analyze，10 分鐘後恢復", "how_detected": "只有 lots 那張表的查詢慢、其他 API 正常；AWR 看到執行計畫變了", "checked": ["Oracle AWR / 慢查詢", "trace 瀑布圖"], "improve": "Oracle 那段 p95 超過 1 秒就直接報"})
        check("回饋寫進去：每筆異常一筆", r["incident"] == "confirmed" and r["feedback"] == len(a1["anomaly_ids"]), str(r))
        inc = es.req("GET", f"/aiops-incidents/_doc/{a1['alert_id']}")["_source"]
        check("事故變 confirmed、存了根因分類與怎麼看出來的", inc["status"] == "confirmed" and inc["root_cause_category"] == "db" and "AWR" in inc["how_detected"] and inc["confirmed_by"] == "老王")
        run("investigator", "alert", "--config", RCA, "--at", iso(0, "10:45"), quiet=True)
        a1b = es.req("GET", f"/aiops-alerts/_doc/{a1['alert_id']}")["_source"]
        check("15 分鐘沒新異常 → 第 1 件 resolved", a1b["status"] == "resolved")
        inc = es.req("GET", f"/aiops-incidents/_doc/{a1['alert_id']}")["_source"]
        check("件結束時不覆蓋老師傅確認過的事故", inc["status"] == "confirmed" and inc.get("root_cause_category") == "db")
        # ── 月底批次 3 次誤報 ──
        print("== 4. 月底批次 mes-report 請求 +50，三次都被報、老師傅都標誤報")
        fp_ids = []
        for svc, d, hhmm in BATCH:
            at = utc(d, hhmm) + pd.Timedelta(minutes=12)
            run("detector", "detect", "--config", SYNTH, "--at", at.strftime("%Y-%m-%dT%H:%M:%SZ"), "--window", "30min", quiet=True)
            run("investigator", "alert", "--config", RCA, "--at", at.strftime("%Y-%m-%dT%H:%M:%SZ"), quiet=True)
            got = [a for a in alerts(es, service="mes-report") if a["alert_id"] not in fp_ids]
            check(f"批次 {d}/{hhmm} 開了一件（mes-report）", len(got) == 1, str([a["alert_id"] for a in alerts(es, service="mes-report")]))
            if got:
                fp_ids.append(got[0]["alert_id"])
                r = post({"alert_id": got[0]["alert_id"], "verdict": "false_positive", "by": "amy", "fp_reason": "batch", "fp_reason_text": "月底結帳批次"})
                check(f"  標誤報（{r['feedback']} 筆）", r["incident"] == "false_positive")
            run("investigator", "alert", "--config", RCA, "--at", (at + pd.Timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ"), quiet=True)
        # ── 第 2 次事故 ──
        print("== 5. 第 2 次事故：10-06 10:00 同樣症狀 → 報告附相似事故＋LLM 摘要（template）")
        run("detector", "detect", "--config", SYNTH, "--at", iso(1, "10:20"), "--window", "30min", quiet=True)
        r = run("investigator", "alert", "--config", RCA, "--at", iso(1, "10:20"))
        a2 = [a for a in alerts(es, service="mes-api") if a["alert_id"] != a1["alert_id"]]
        check("第 2 件開了", len(a2) == 1, str(len(a2)))
        a2 = a2[0]
        sim = a2.get("similar") or []
        check("第 2 件附上第 1 件（相似事故）", bool(sim) and sim[0]["incident_id"] == a1["alert_id"], json.dumps(sim, ensure_ascii=False)[:300])
        check("  為什麼像：同服務、同樣的指標異常", bool(sim) and any("同服務" in w for w in sim[0]["why"]) and any("指標" in w for w in sim[0]["why"]), str(sim and sim[0]["why"]))
        check("  帶當時的根因、處置、怎麼看出來的", bool(sim) and sim[0]["root_cause_category"] == "db" and "analyze" in sim[0]["action"] and "AWR" in sim[0]["how_detected"])
        check("摘要（Grafana 通知帶的）有「過去相似」", "過去相似：" in (a2.get("summary") or ""), a2.get("summary"))
        check("報告最前面有「過去相似的事故」", "## 過去相似的事故" in (a2.get("report") or ""))
        llm = a2.get("llm_summary") or ""
        check("LLM 摘要（template）有、每句附依據、引用了相似事故 [I1]", bool(llm) and "[I1]" in llm and all("[" in l for l in llm.splitlines()), llm)
        print("    LLM 摘要：\n      " + llm.replace("\n", "\n      "))
        print("    通知摘要：\n      " + (a2.get("summary") or "").replace("\n", "\n      "))
        r = post({"alert_id": a2["alert_id"], "verdict": "true_positive", "by": "老王", "root_cause_category": "db", "root_cause": "同上：統計資訊過期", "action": "重跑 analyze", "how_detected": "照上次：看 lots 查詢"})
        run("investigator", "alert", "--config", RCA, "--at", iso(1, "10:45"), quiet=True)
        print("== 6. 第 3 次（同根因）→ 確認 → 同根因 3 次")
        run("detector", "detect", "--config", SYNTH, "--at", iso(1, "15:20"), "--window", "30min", quiet=True)
        run("investigator", "alert", "--config", RCA, "--at", iso(1, "15:20"), quiet=True)
        a3 = [a for a in alerts(es, service="mes-api") if a["alert_id"] not in (a1["alert_id"], a2["alert_id"])]
        check("第 3 件開了、附 2 件相似", len(a3) == 1 and len(a3[0].get("similar") or []) == 2, str(len(a3) and len(a3[0].get("similar") or [])))
        post({"alert_id": a3[0]["alert_id"], "verdict": "true_positive", "by": "小陳", "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze"})
        run("investigator", "alert", "--config", RCA, "--at", iso(1, "15:45"), quiet=True)
        print("== 7. similar-eval（留一法）")
        r = run("investigator", "similar-eval", "--config", RCA)
        check("recall@3：3 件 db 互相查得到 → 3/3", "recall@3：3/3" in r.stdout, r.stdout[-200:])
        print("== 8. feedback suggest：誤報 3 次 → 放寬建議；同根因 3 次 → 升級成規則")
        r = run("detector", "feedback", "suggest", "--config", SYNTH, "--days", "60")
        tun = es.search("aiops-tuning", {"size": 50, "query": {"match_all": {}}})["hits"]["hits"]
        ov = [h for h in tun if h["_source"].get("kind") == "override"]; rule = [h for h in tun if h["_source"].get("kind") == "rule_suggestion"]
        check("一筆放寬 z 的建議（proposed，mes-report synth.requests）", len(ov) == 1 and ov[0]["_source"]["status"] == "proposed" and ov[0]["_source"]["series"] == "synth\\.requests", str([h["_source"].get("series") for h in ov]))
        check("一筆升級成規則的建議（mes-api db ×3），附規則草稿", len(rule) == 1 and rule[0]["_source"]["service"] == "mes-api" and rule[0]["_source"]["rule_draft"]["type"] == "window", str(len(rule)))
        if rule: print("    規則草稿：" + json.dumps(rule[0]["_source"]["rule_draft"], ensure_ascii=False)[:400])
        r = run("detector", "adjust", "approve", "--config", SYNTH, "--id", ov[0]["_id"], "--by", "amy")
        check("沒回測不能核准", r.returncode != 0 and "回測" in r.stdout, r.stdout[-200:])
        print("== 9. backtest（過去 1 週重跑，現行 vs 現行＋建議）")
        r = run("detector", "backtest", "--config", SYNTH, "--id", ov[0]["_id"], "--weeks", "1", "--at", iso(2, "00:00"))
        bt = es.req("GET", f"/aiops-tuning/_doc/{ov[0]['_id']}")["_source"].get("backtest") or {}
        check("回測結果寫回建議", bool(bt) and "anomalies_before" in bt, str(bt)[:300])
        check("  告警變少、已確認事故（mes-api）不受影響 → 建議核准", bt.get("recommended") is True and bt.get("anomalies_after", 99) < bt.get("anomalies_before", 0) and not bt.get("incidents_lost") and "不受影響" in bt.get("reason", ""), bt.get("reason"))
        r = run("detector", "adjust", "list", "--config", SYNTH)
        check("adjust list 顯示回測", "回測：建議核准" in r.stdout)
        r = run("detector", "adjust", "approve", "--config", SYNTH, "--id", ov[0]["_id"], "--by", "amy")
        check("核准（記誰、何時）", "已核准" in r.stdout and es.req("GET", f"/aiops-tuning/_doc/{ov[0]['_id']}")["_source"].get("approved_by") == "amy")
        print("== 10. 核准後：同樣的批次再 detect 一次 → 不再是 anomaly")
        svc, d, hhmm = BATCH[0]; at = utc(d, hhmm) + pd.Timedelta(minutes=12)
        es.req("POST", "/aiops-anomalies/_delete_by_query?refresh=true", {"query": {"bool": {"filter": [{"term": {"keys.service": "mes-report"}}, {"term": {"series": "synth.requests"}}]}}})
        run("detector", "detect", "--config", SYNTH, "--at", at.strftime("%Y-%m-%dT%H:%M:%SZ"), "--window", "30min", quiet=True)
        left = es.search("aiops-anomalies", {"size": 10, "query": {"bool": {"filter": [{"term": {"keys.service": "mes-report"}}, {"term": {"series": "synth.requests"}}, {"term": {"status": "anomaly"}}]}}})["hits"]["hits"]
        check("mes-report 的批次不再是 anomaly", not left, str([(h["_source"]["@timestamp"], h["_source"].get("z")) for h in left]))
        print("== 11. 第 2 個人用 --force 以外的路徑：漏報 → 建議降 z")
        run("detector", "feedback", "add", "--config", SYNTH, "--verdict", "missed", "--series", "synth.errors", "--match", "service=eap-gw", "--at", iso(0, "03:00"), "--by", "amy", "--note", "凌晨錯誤沒報", quiet=True)
        r = run("detector", "feedback", "suggest", "--config", SYNTH, "--days", "60", quiet=True)
        miss = [h for h in es.search("aiops-tuning", {"size": 50, "query": {"match_all": {}}})["hits"]["hits"] if "漏報" in (h["_source"].get("reason") or "")]
        check("漏報 → 一筆降 z 的建議（proposed）", len(miss) == 1 and miss[0]["_source"]["z"] == 4.0, str([h["_source"].get("reason") for h in miss]))
        # 之前被放寬過的對象（mes-report synth.requests，剛核准 z 7）漏報了：建議不能被吃掉，要從 7 往下降（pdca/2026-10-fix-feedback-ux.md 5）
        run("detector", "feedback", "add", "--config", SYNTH, "--verdict", "missed", "--series", "synth.requests", "--match", "service=mes-report", "--at", iso(1, "16:00"), "--by", "amy", "--note", "批次之外真的掉量沒報", quiet=True)
        r = run("detector", "feedback", "suggest", "--config", SYNTH, "--days", "60", quiet=True)
        miss2 = [h for h in es.search("aiops-tuning", {"size": 50, "query": {"match_all": {}}})["hits"]["hits"]
                 if "漏報" in (h["_source"].get("reason") or "") and "mes\\-report" in (h["_source"].get("series", "") + json.dumps(h["_source"].get("match")))]
        check("被放寬過的對象漏報 → 仍提建議，從現在的 z 7 降到 5.6", len(miss2) == 1 and miss2[0]["_source"]["z"] == 5.6 and "之前" not in r.stdout or "往下降" in r.stdout, str([(h["_source"].get("z"), h["_source"].get("reason")) for h in miss2]) + r.stdout[-300:])
        r = run("detector", "feedback", "suggest", "--config", SYNTH, "--days", "60", quiet=True)
        check("再跑一次不重複、有說已經有了", "漏報的建議已經有了" in r.stdout, r.stdout[-200:])
        print("== 12. metrics：兩段並排")
        r = run("detector", "metrics", "--config", SYNTH, "--from", "2026-10-05", "--to", "2026-10-26", "--compare-from", "2026-09-07", "--compare-to", "2026-10-05", "--out", os.path.join(tempfile.gettempdir(), "aiops-loop-metrics.md"))
        check("metrics 印出件數、誤報率、怎麼看出來的比例", "| 件數 | 0（每天 0.0） | 6（每天" in r.stdout and "誤報率（回饋裡標誤報的） | — | 50%" in r.stdout and "「怎麼看出來的」有填的比例 | — | 67%" in r.stdout, r.stdout)
    finally:
        web.terminate()
        err = web.stderr.read()[-600:] if web.stderr else ""
        if err: print("  feedback_web stderr（最後幾行）:\n    " + err.replace("\n", "\n    "))
    print("\n== 結果：" + ("全部 PASS" if not fails else f"{len(fails)} 個 FAIL：{fails}"))
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
