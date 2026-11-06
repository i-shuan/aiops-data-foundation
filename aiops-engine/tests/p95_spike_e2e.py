"""p95 一個點暴衝，會不會開事故（configs/baseline/rules.yaml 的 http.latency_p95_floor＋investigator.alerts.merge）。

問題：http.latency_p95_floor 是 severity warning，事件整合把 warning 規則當 weak（單獨一筆不開，同一個服務要有 2 筆以上）；
但這條規則是 window 5m，一次暴衝會讓連續好幾輪都命中。這裡用真的 ES、真的 detector rules，一分鐘一輪量出來。

做法：照 Collector 的 elasticsearch 寫入元件在 ES 8.19 存 metrics 的樣子（跟 tests/rules_edge_e2e.py 一樣）灌兩個服務 21 分鐘的 HTTP
請求直方圖（每 30 秒一點、每點 4 個請求都 0.1 秒），再各動一個點：
  A：那個點多 4 個 3 秒的請求（整個 5 分鐘窗 4/44 ≈ 9% 超過 2 秒 → p95 ≥ 2 秒）
  B：那個點多 1 個 3 秒的請求（1/41 ≈ 2.4%，p95 不到 2 秒）
暴衝後每分鐘跑一輪 detector rules --at <那一輪>，每輪把累積的規則異常餵給 alerts.merge，看哪一輪開出一件。

會建／清：資料流 metrics-generic.otel-p95spike、aiops-anomalies 裡這兩個服務的規則異常（只能對測試用的 ES 跑）。
  ES_URL=http://127.0.0.1:19200 ES_AUTH=elastic:<password> python -X utf8 -m tests.p95_spike_e2e      （在 aiops-engine 底下）
約 1 分鐘。每項印 PASS／FAIL；有 FAIL 時 exit 1。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta

import pandas as pd
import requests

import service.converge.merge as AL

ES_URL = os.environ["ES_URL"].rstrip("/")
AUTH = tuple(os.environ["ES_AUTH"].split(":", 1)) if os.environ.get("ES_AUTH") else None
ENGINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG = "configs/baseline/rules.yaml"
RULE = "http.latency_p95_floor"
STREAM = "metrics-generic.otel-p95spike"
SVC_A, SVC_B = "p95spike-a", "p95spike-b"
SERIES = f"rule.{RULE}"
CLOSE_AFTER = timedelta(minutes=30)             # configs/rca/rca.yaml 的 alerts.close_after
HIST = "metrics.http.server.request.duration"

s = requests.Session()
if AUTH:
    s.auth = AUTH
fails: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"：{detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def req(method, path, body=None, ok=(200, 201), **kw):
    r = s.request(method, ES_URL + path, json=body, timeout=300, **kw)
    if r.status_code not in ok:
        raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:600]}")
    return r.json() if r.content else {}


def hist(values: list[float]) -> dict:
    counts: dict[float, int] = {}
    for v in values:
        counts[float(v)] = counts.get(float(v), 0) + 1
    vals = sorted(counts)
    return {"values": vals, "counts": [counts[v] for v in vals]}


# 時間線：暴衝點在 T_SPIKE；之前 12 分鐘、之後 9 分鐘都是平常流量
T_SPIKE = pd.Timestamp.now(tz="UTC").floor("1min") - pd.Timedelta(minutes=25) + pd.Timedelta(seconds=20)


def load():
    if req("GET", f"/_data_stream/{STREAM}", ok=(200, 404)).get("data_streams"):
        req("POST", f"/{STREAM}/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})
    else:
        req("PUT", f"/_data_stream/{STREAM}?master_timeout=5m")
    req("POST", "/aiops-anomalies/_delete_by_query?refresh=true&conflicts=proceed&ignore_unavailable=true",
        {"query": {"term": {"series": SERIES}}}, ok=(200, 404))
    res_keys = ["service.name", "k8s.namespace.name", "k8s.pod.name"]
    attr_keys = ["http.request.method", "http.route", "http.response.status_code"]
    req("PUT", f"/{STREAM}/_mapping?master_timeout=5m", {"properties": {
        "metrics": {"type": "passthrough", "priority": 10, "properties": {
            "http.server.request.duration": {"type": "histogram", "meta": {"unit": ""}}}},
        "attributes": {"type": "passthrough", "priority": 20, "time_series_dimension": True, "properties": {
            k: {"type": "keyword", "ignore_above": 1024, "time_series_dimension": True} for k in attr_keys}},
        "resource": {"properties": {"attributes": {"type": "passthrough", "priority": 40, "time_series_dimension": True,
                                                     "properties": {k: {"type": "keyword", "ignore_above": 1024,
                                                                        "time_series_dimension": True} for k in res_keys}}}}}})
    lines = []
    for svc, extra in ((SVC_A, [3.0] * 4), (SVC_B, [3.0])):
        for i in range(-24, 19):                                        # -12 分鐘 ～ +9 分鐘，每 30 秒一點
            t = T_SPIKE + pd.Timedelta(seconds=30 * i)
            values = [0.1] * 4 + (extra if i == 0 else [])
            lines += [json.dumps({"create": {"_index": STREAM, "dynamic_templates": {HIST: "histogram"}}}),
                      json.dumps({"@timestamp": t.isoformat(), "attributes": {
                          "http.request.method": "GET", "http.route": "/orders/{id}", "http.response.status_code": "200"},
                          "resource": {"attributes": {"service.name": svc, "k8s.namespace.name": "p95spike", "k8s.pod.name": f"{svc}-0"}},
                          "metrics": {"http.server.request.duration": hist(values)}})]
    r = s.post(f"{ES_URL}/_bulk?refresh=true", data="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"}, timeout=300).json()
    bad = [it["create"] for it in r.get("items", []) if it["create"].get("error") and it["create"].get("status") != 409]
    if bad:
        print(f"寫入失敗 {len(bad)} 筆，例：{json.dumps(bad[:2], ensure_ascii=False)[:800]}")
        sys.exit(2)
    print(f"灌了 {len(lines) // 2} 份文件，暴衝點在 {T_SPIKE.isoformat()}")


def run_rules(at: pd.Timestamp):
    cmd = [sys.executable, "-X", "utf8", "-m", "main", "detector", "rules", "--config", CONFIG, "--at", at.isoformat(), "--only", RULE]
    r = subprocess.run(cmd, cwd=ENGINE, capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    if r.returncode != 0:
        print(r.stdout[-600:], r.stderr[-600:])
        sys.exit(2)


def rule_hits() -> dict[str, list[dict]]:
    body = {"size": 10000, "query": {"term": {"series": SERIES}}, "sort": [{"@timestamp": "asc"}]}
    out: dict[str, list[dict]] = {SVC_A: [], SVC_B: []}
    for h in req("POST", "/aiops-anomalies/_search?ignore_unavailable=true", body)["hits"]["hits"]:
        a = h["_source"] | {"_id": h["_id"]}
        svc = (a.get("keys") or {}).get("resource.attributes.service.name")
        if svc in out:
            out[svc].append(a)
    return out


def main() -> int:
    print("== 0. 灌資料")
    load()
    print("== 1. 暴衝後每分鐘一輪：detector rules → alerts.merge")
    stored: dict[str, list[dict]] = {SVC_A: [], SVC_B: []}
    first_hit: dict[str, int | None] = {SVC_A: None, SVC_B: None}
    opened: dict[str, int | None] = {SVC_A: None, SVC_B: None}
    n_hits: dict[str, int] = {}
    print("   第幾輪（暴衝後幾秒）  A 規則累計命中  A 有開件    B 規則累計命中  B 有開件")
    for k in range(9):
        at = T_SPIKE + pd.Timedelta(seconds=10 + 60 * k)
        run_rules(at)
        hits = rule_hits()
        row = []
        for svc in (SVC_A, SVC_B):
            n_hits[svc] = len(hits[svc])
            if hits[svc] and first_hit[svc] is None:
                first_hit[svc] = k
            out = AL.merge(stored[svc], hits[svc], at.to_pydatetime(), CLOSE_AFTER)
            stored[svc] = [{kk: v for kk, v in al.items() if not kk.startswith("_")} for al in out if al["status"] == "open"]
            if any(al["status"] == "open" for al in out) and opened[svc] is None:
                opened[svc] = k
            row += [f"{len(hits[svc]):>2}", "是" if opened[svc] is not None else "否"]
        print(f"   第 {k} 輪（+{10 + 60 * k:>3} 秒）        {row[0]:>10}          {row[1]}          {row[2]:>10}          {row[3]}")
    print("== 2. 結果")
    a_first, a_open = first_hit[SVC_A], opened[SVC_A]
    check("A（5 分鐘窗 9% 超過 2 秒）：暴衝後第一輪規則就命中", a_first == 0, f"第一次命中在第 {a_first} 輪")
    check("A：規則連續命中（窗口 5 分鐘，一個暴衝點會被連續好幾輪看到）", n_hits[SVC_A] >= 4, f"共 {n_hits[SVC_A]} 筆")
    check("A：開出一件事故（warning 規則 weak 要 2 筆，第 2 輪湊到）", a_open == 1, f"開件在第 {a_open} 輪")
    check("B（5 分鐘窗 2.4% 超過 2 秒）：p95 不到 2 秒，規則不命中", n_hits[SVC_B] == 0, f"{n_hits[SVC_B]} 筆")
    check("B：沒有開件", opened[SVC_B] is None, f"開件在第 {opened[SVC_B]} 輪")
    if a_open is not None:
        print(f"\n   A：暴衝點進 ES 後約 {10 + 60 * a_open} 秒（第 {a_open} 輪）開出事故；再加排程器每分鐘一輪、alert 再跑一輪，實際延遲再多 0～2 分鐘")
    print("\n== 結果：" + ("全部 PASS" if not fails else f"{len(fails)} 個 FAIL：{fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
