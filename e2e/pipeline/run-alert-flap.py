# 重現「件還開著、Grafana 卻發恢復」（docs/pdca/phase3-locate/2026-10-alert-flap.md）：
# 兩件假的 open 事故寫進 aiops-alerts（mapping 用 engine 的），Grafana 掛 grafana/code/ 的告警規則。
#   A（對照）：每分鐘在排定評估前 15 秒覆蓋寫入
#   B（實驗）：每分鐘在「排定評估時間」和「實際送查詢」之間覆蓋寫入
# 先用舊寫法（@timestamp = 寫入當下）跑幾分鐘，再用 engine 現在的寫法（cmd_alert.stamp_of）跑幾分鐘，看 B 有沒有被當成恢復。
#
# 用法：ES_URL=… ES_AUTH=elastic:… GRAFANA_URL=… GRAFANA_AUTH=admin:… python run-alert-flap.py [每段分鐘數，預設 3]
# 前提：Grafana 已經掛好 grafana/code/ 的 datasource、alert-rules（規則每 1 分鐘評估）。會刪掉重建 aiops-alerts。
import datetime as dt, functools, os, pathlib, sys, time
print = functools.partial(print, flush=True)
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "engine"))
from investigator.cmd_alert import ALERT_INDEX, ALERT_MAPPING, stamp_of  # noqa: E402

ES, GF = os.environ["ES_URL"].rstrip("/"), os.environ["GRAFANA_URL"].rstrip("/")
es = requests.Session(); es.auth = tuple(os.environ["ES_AUTH"].split(":", 1))
gf = requests.Session(); gf.auth = tuple(os.environ["GRAFANA_AUTH"].split(":", 1))
minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 3
utc = dt.timezone.utc
now = lambda: dt.datetime.now(utc)


def write(alert_id: str, stamp: dt.datetime):
    doc = {"@timestamp": stamp.isoformat(), "alert_id": alert_id, "category": "service", "service": f"flap-{alert_id[-1]}",
           "status": "open", "severity": "critical", "dept": None, "group": "unassigned", "summary": f"flap test {alert_id}",
           "started_at": stamp.isoformat(), "updated_at": now().isoformat()}
    r = es.put(f"{ES}/{ALERT_INDEX}/_doc/{alert_id}?refresh=true", json=doc, timeout=30)
    r.raise_for_status()


def history(since: dt.datetime):
    """[(時間, alert_id, 前, 後)]"""
    r = gf.get(f"{GF}/api/v1/rules/history", params={"ruleUID": "aiops-incident", "from": int(since.timestamp()), "to": int(now().timestamp()) + 60, "limit": 500}, timeout=30).json()
    v = r["data"]["values"]
    out = []
    for i in range(len(v[0])):
        aid = next((x.split("=", 1)[1] for x in v[1][i].split("{", 1)[-1].rstrip("}").split(", ") if x.startswith("alert_id=")), "?")
        out.append((dt.datetime.fromtimestamp(v[0][i] / 1e3 if v[0][i] < 1e14 else v[0][i] / 1e6, utc), aid, v[2][i], v[3][i]))
    return sorted(out)


def sleep_until(t: dt.datetime):
    time.sleep(max((t - now()).total_seconds(), 0))


es.delete(f"{ES}/{ALERT_INDEX}", timeout=30)
es.put(f"{ES}/{ALERT_INDEX}", json=ALERT_MAPPING, timeout=30).raise_for_status()
t0 = now()
print(f"{t0:%H:%M:%S} 建好 {ALERT_INDEX}，寫入 A、B，等 Grafana 評估到 Alerting")
def rule_state():
    """(最後一次排定評估的時間, {alert_id: 狀態})"""
    r = gf.get(f"{GF}/api/prometheus/grafana/api/v1/rules", timeout=30).json()
    rule = next(x for g in r["data"]["groups"] for x in g["rules"] if x.get("labels", {}).get("__alert_rule_uid__") == "aiops-incident" or x["name"] == "AIOps 事件")
    return dt.datetime.fromisoformat(rule["lastEvaluation"].replace("Z", "+00:00")), {a["labels"].get("alert_id"): a["state"] for a in rule.get("alerts", [])}


# 只寫一次：一直覆蓋寫「當下」的話，文件幾乎永遠落在排定評估與查詢之間，等不到 Alerting（就是要測的那個問題）
write("flap-A", now()); write("flap-B", now())
E = None
while (now() - t0).total_seconds() < 180:
    time.sleep(5)
    last, st = rule_state()
    if last > t0 and st.get("flap-A") == "Alerting" and st.get("flap-B") == "Alerting":
        E = last; break
if E is None: sys.exit("3 分鐘內 A、B 沒有都是 Alerting，規則或 datasource 有問題")
sec = E.second + E.microsecond / 1e6
print(f"{now():%H:%M:%S} 排定評估時間：每分鐘第 {sec:.1f} 秒（狀態歷史的時間）")
# 實際送查詢的時間：用 Grafana 的查詢 log 量不到時，假設在排定時間之後 0～5 秒（K8s 上量到 5 秒）
GAP = float(os.environ.get("QUERY_DELAY", "5"))


def run(label: str, stampf, n: int):
    start = now()
    print(f"\n== {label}：{n} 分鐘。B 每分鐘在第 {sec + GAP / 2:.1f} 秒寫入（排定評估之後、查詢之前），A 在第 {sec - 15:.1f} 秒")
    nxt = start.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(n):
        sleep_until(nxt + dt.timedelta(seconds=sec - 15)); write("flap-A", stampf(now()))
        sleep_until(nxt + dt.timedelta(seconds=sec + GAP / 2)); w = now(); write("flap-B", stampf(w))
        print(f"   {w:%H:%M:%S.%f}"[:-3] + f" 寫 B，@timestamp {stampf(w):%H:%M:%S}")
        nxt += dt.timedelta(minutes=1)
    time.sleep(GAP + 3)
    hs = [h for h in history(start) if h[0] >= start]
    for t, aid, a, b in hs: print(f"   {t:%H:%M:%S} {aid}: {a} → {b}")
    # 被當成恢復：Alerting → Normal。整個查詢 0 筆是 Normal (NoData)；只有這一件不見是 Normal (MissingSeries)
    # （Grafana 12 以上對 MissingSeries 預設連續 2 次評估才轉）
    dropped = lambda aid: [h for h in hs if h[1] == aid and h[2].startswith("Alerting") and h[3].startswith("Normal")]
    flips, ctrl = dropped("flap-B"), dropped("flap-A")
    print(f"   結果：B 被當成恢復 {len(flips)} 次；A（對照）{len(ctrl)} 次")
    return len(flips), len(ctrl)


old = run("舊寫法：@timestamp = 寫入當下", lambda w: w, minutes)
# 回到正常：兩件都在排定評估前寫入，等一輪
nxt = now().replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
sleep_until(nxt + dt.timedelta(seconds=sec - 15)); write("flap-A", now()); write("flap-B", now())
sleep_until(nxt + dt.timedelta(seconds=sec + GAP + 3))
new = run("engine 現在的寫法：cmd_alert.stamp_of", stamp_of, minutes)
ok = old[0] > 0 and old[1] == 0 and new[0] == 0 and new[1] == 0
print(f"\n{'PASS' if ok else 'FAIL'}  舊寫法 B 被當成恢復 {old[0]} 次、A {old[1]} 次；新寫法 B {new[0]} 次、A {new[1]} 次")
sys.exit(0 if ok else 1)
