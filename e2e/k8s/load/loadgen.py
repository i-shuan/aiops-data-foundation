# 壓測用的資料產生器（run-stress.sh 用 Job 跑，一個 Pod 一份；Indexed Job 的 JOB_COMPLETION_INDEX 當 gen 編號）。
# MODE：
#   log    ：stdout 印 JSON log，每行帶 run、gen、seq（ES 裡是 app.run、app.gen、app.seq），LINE_BYTES 控制 message 長度
#   metric ：OTLP/HTTP 推 gauge loadtest.gauge 到節點 Collector，每 INTERVAL 秒一次、每次 SERIES 個資料點（維度 run、g、s）
#   trace  ：OTLP/HTTP 推 span 到節點 Collector，每個 trace SPANS 段，每次送最多 512 段（同 SDK 的 BatchSpanProcessor）
#   prom   ：開 :9464/metrics 給 Collector 抓，SERIES 條 loadtest_card，id 每 WINDOW 秒全部換新（高基數、序列一直換）
#   a+b    ：一個 Pod 同時送兩種（例如 log+trace），速率各用 PROFILE_a、PROFILE_b，run 是 <RUN>-a、<RUN>-b
#   proxy  ：ES 前面的代理，轉給 ES 後把回應壓 DELAY 秒才回（ES 慢）；GET /_loadproxy?delay=秒 改延遲
# 速率用 PROFILE：「秒數:每秒幾筆」逗號分開，例如 60:200,60:2000,60:200（突發）。metric 的「筆」是資料點、trace 是 span。
# 結束時 stdout 最後一行印 {"result": {...}}：送出幾筆、被 Collector 拒收幾筆（HTTP 非 2xx 或連不上，不重送）。
import base64, http.client, json, os, random, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ["MODE"]
RUN = os.environ.get("RUN", "")
GEN = int(os.environ.get("JOB_COMPLETION_INDEX", "0"))
OTLP = os.environ.get("OTLP_HOST", "otel-node-collector.otel-collector.svc")


def profile():
    segs = []
    for p in os.environ.get("PROFILE", "60:100").split(","):
        secs, rate = p.split(":")
        segs.append((float(secs), float(rate)))
    return segs


def paced(tick, emit):
    """依 PROFILE 每 tick 秒呼叫 emit(本次幾筆)，欠的筆數累積到下一次，整體速率準"""
    start = time.time()
    t_end = 0.0
    owed = 0.0
    for secs, rate in profile():
        t_end += secs
        while True:
            now = time.time()
            if now - start >= t_end:
                break
            owed += rate * tick
            n = int(owed)
            owed -= n
            if n:
                emit(n)
            nxt = start + (int((now - start) / tick) + 1) * tick
            if nxt > time.time():
                time.sleep(nxt - time.time())
    return time.time() - start


def done(**kw):
    print(json.dumps({"result": dict(mode=MODE, run=RUN, gen=GEN, **kw)}), flush=True)


# ---------------------------------------------------------------- log
def run_log():
    size = int(os.environ.get("LINE_BYTES", "250"))
    if size <= 250:
        bodies = ["x" * size]
    else:  # 大行：隨機內容，gzip 壓不小，最壞情況
        bodies = [base64.b64encode(os.urandom(size))[:size].decode() for _ in range(8)]
    out = sys.stdout
    st = {"seq": 0}

    def emit(n):
        now = time.time()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%03dZ" % int((now % 1) * 1000)
        buf = []
        for _ in range(n):
            st["seq"] += 1
            buf.append(json.dumps({"timestamp": ts, "level": "INFO", "message": bodies[st["seq"] % len(bodies)],
                                   "logger": "load.Gen", "thread": "main", "run": RUN, "gen": GEN,
                                   "seq": st["seq"], "len": size}))
        out.write("\n".join(buf) + "\n")
        out.flush()

    secs = paced(0.01 if size <= 4096 else 0.1, emit)
    # 結果寫 stderr：stdout 是被收的 log，不能混進去
    sys.stderr.write(json.dumps({"result": {"mode": "log", "run": RUN, "gen": GEN, "sent": st["seq"],
                                            "refused": 0, "seconds": round(secs, 1)}}) + "\n")


# ---------------------------------------------------------------- OTLP 推送
class Sender:
    def __init__(self, path):
        self.path = path
        self.conn = None
        self.sent = 0
        self.refused = 0
        self.errors = {}

    def post(self, body, n):
        self.sent += n
        try:
            if self.conn is None:
                self.conn = http.client.HTTPConnection(OTLP, 4318, timeout=30)
            self.conn.request("POST", self.path, body, {"Content-Type": "application/json"})
            r = self.conn.getresponse()
            r.read()
            if r.status // 100 != 2:
                self.refused += n
                self.errors[str(r.status)] = self.errors.get(str(r.status), 0) + 1
        except Exception as e:  # noqa: BLE001 連不上、逾時都算拒收
            self.refused += n
            k = type(e).__name__
            self.errors[k] = self.errors.get(k, 0) + 1
            self.conn = None


def res_attrs():
    return [{"key": "service.name", "value": {"stringValue": "load-gen"}}]


def kv(k, v):
    return {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}


def run_metric():
    interval = float(os.environ.get("INTERVAL", "1"))
    s = Sender("/v1/metrics")
    st = {"tick": 0}

    # PROFILE 的速率是「每秒資料點」，換算成每次送幾條序列
    def emit(n):
        st["tick"] += 1
        ts = str(time.time_ns())
        pts = ",".join('{"timeUnixNano":"%s","asInt":"%d","attributes":[{"key":"run","value":{"stringValue":"%s"}},'
                       '{"key":"g","value":{"intValue":"%d"}},{"key":"s","value":{"intValue":"%d"}}]}'
                       % (ts, st["tick"], RUN, GEN, i) for i in range(n))
        body = ('{"resourceMetrics":[{"resource":{"attributes":%s},"scopeMetrics":[{"scope":{"name":"load-gen"},'
                '"metrics":[{"name":"loadtest.gauge","gauge":{"dataPoints":[%s]}}]}]}]}') % (json.dumps(res_attrs()), pts)
        s.post(body, n)

    secs = paced(interval, emit)
    done(sent=s.sent, refused=s.refused, errors=s.errors, seconds=round(secs, 1))


def run_trace():
    per = int(os.environ.get("SPANS", "5"))
    s = Sender("/v1/traces")
    st = {"seq": 0, "trace": None, "left": 0, "root": None}
    pending = []

    def flush():
        if not pending:
            return
        body = ('{"resourceSpans":[{"resource":{"attributes":%s},"scopeSpans":[{"scope":{"name":"load-gen"},"spans":[%s]}]}]}'
                % (json.dumps(res_attrs()), ",".join(pending)))
        s.post(body, len(pending))
        pending.clear()

    def emit(n):
        now = time.time_ns()
        for _ in range(n):
            if st["left"] == 0:
                st["trace"] = os.urandom(16).hex()
                st["root"] = os.urandom(8).hex()
                st["left"] = per
            sid = st["root"] if st["left"] == per else os.urandom(8).hex()
            parent = "" if st["left"] == per else st["root"]
            st["left"] -= 1
            st["seq"] += 1
            pending.append('{"traceId":"%s","spanId":"%s","parentSpanId":"%s","name":"GET /load","kind":%d,'
                           '"startTimeUnixNano":"%d","endTimeUnixNano":"%d","attributes":[{"key":"run","value":{"stringValue":"%s"}},'
                           '{"key":"seq","value":{"intValue":"%d"}},{"key":"http.response.status_code","value":{"intValue":"200"}}],"status":{}}'
                           % (st["trace"], sid, parent, 2 if not parent else 1, now - 50_000_000, now, RUN, st["seq"]))
            if len(pending) >= 512:
                flush()
        flush()

    secs = paced(0.1, emit)
    done(sent=s.sent, refused=s.refused, errors=s.errors, seconds=round(secs, 1))


# ---------------------------------------------------------------- Prometheus 高基數
def run_prom():
    series = int(os.environ.get("SERIES", "20000"))
    window = int(os.environ.get("WINDOW", "30"))
    duration = float(os.environ.get("DURATION", "300"))
    st = {"scrapes": 0, "points": 0, "ids": set()}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            w = int(time.time() // window)
            body = "# TYPE loadtest_card gauge\n" + "".join(
                'loadtest_card{run="%s",id="%d-%d"} %d\n' % (RUN, w, i, i) for i in range(series))
            st["scrapes"] += 1
            st["points"] += series
            st["ids"].add(w)
            b = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", 9464), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(duration)
    srv.shutdown()
    done(sent=st["points"], refused=0, scrapes=st["scrapes"], distinct_series=series * len(st["ids"]))


# ---------------------------------------------------------------- ES 慢（代理）
def run_proxy():
    upstream = os.environ.get("UPSTREAM", "elasticsearch.es-test.svc")
    cfg = {"delay": float(os.environ.get("DELAY", "0"))}
    local = threading.local()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _fwd(self):
            if self.path.startswith("/_loadproxy"):
                if "delay=" in self.path:
                    cfg["delay"] = float(self.path.split("delay=")[1])
                b = json.dumps(cfg).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            hdr = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length")}
            for _ in range(2):
                try:
                    if getattr(local, "c", None) is None:
                        local.c = http.client.HTTPConnection(upstream, 9200, timeout=120)
                    local.c.request(self.command, self.path, body, hdr)
                    r = local.c.getresponse()
                    data = r.read()
                    break
                except Exception:  # noqa: BLE001 連線斷了換一條
                    local.c = None
            else:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            time.sleep(cfg["delay"])
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in ("content-length", "connection", "transfer-encoding"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_HEAD = _fwd

        def log_message(self, *a):
            pass

    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", 9200), H).serve_forever()


if "+" in MODE:  # 一個 Pod 同時送幾種（例如 log+trace）：各開一個子程序，run 加上種類，速率用 PROFILE_<種類>
    import subprocess
    ps = [subprocess.Popen([sys.executable, "-u", __file__], env={**os.environ, "MODE": m, "RUN": "%s-%s" % (RUN, m),
                                                                 "PROFILE": os.environ["PROFILE_" + m]})
          for m in MODE.split("+")]
    sys.exit(max(p.wait() for p in ps))
{"log": run_log, "metric": run_metric, "trace": run_trace, "prom": run_prom, "proxy": run_proxy}[MODE]()
