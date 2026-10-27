# 收 Grafana 通知的假 webhook（grafana.yaml 的 notify-sink）：每一則印一行 時間 路徑 狀態，再逐個 alert 印名稱、服務、摘要
import datetime
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        print(datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%SZ"), self.path, body.get("status"), flush=True)
        for a in body.get("alerts", []):
            lb, an = a.get("labels", {}), a.get("annotations", {})
            print("   ", a.get("status"), "|", lb.get("alertname"), "|", lb.get("service"), "|", lb.get("dept"), "|",
                  (an.get("summary") or "").replace("\n", " ")[:300], flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
