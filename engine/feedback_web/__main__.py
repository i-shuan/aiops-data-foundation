"""HTTP 這一層：標準函式庫的 http.server，邏輯在 app.py。"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.es import ES

from . import app as A


class Handler(BaseHTTPRequestHandler):
    es: ES = None

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if u.path in ("/feedback", "/report"):
            alert = A.load_alert(self.es, q.get("alert", ""))
            if not alert:
                return self._send(404, f"找不到告警 {q.get('alert')}".encode(), "text/plain; charset=utf-8")
            if u.path == "/report":
                return self._send(200, A.render_report(alert).encode())
            return self._send(200, A.render_form(A.form_data(alert)).encode())
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if u.path not in ("/feedback", "/api/feedback"):
            return self._send(404, b"not found", "text/plain")
        if self.headers.get("Content-Type", "").startswith("application/json"):
            form = json.loads(raw or b"{}")
        else:
            form = {k: (v if k == "checked" else v[0]) for k, v in parse_qs(raw.decode("utf-8")).items()}
        try:
            done = A.submit(self.es, form)
        except (KeyError, ValueError) as e:
            if u.path == "/api/feedback":
                return self._send(400, A.to_json({"error": str(e)}), "application/json")
            alert = A.load_alert(self.es, form.get("alert_id", ""))
            return self._send(400, A.render_form(A.form_data(alert), error=str(e)).encode() if alert else str(e).encode())
        except Exception as e:    # ES 掛了、連不上：要回一頁說「沒存成功，請再送一次」，不能把連線關掉讓人猜
            msg = f"沒有存成功（{type(e).__name__}：{str(e)[:120]}）。請稍後再送一次；重送不會重複記錄。"
            sys.stderr.write(f"feedback submit failed: {e!r}\n")
            if u.path == "/api/feedback":
                return self._send(503, A.to_json({"error": msg}), "application/json")
            try:
                alert = A.load_alert(self.es, form.get("alert_id", ""))
            except Exception:
                alert = None
            return self._send(503, A.render_form(A.form_data(alert), error=msg).encode() if alert else msg.encode())
        if u.path == "/api/feedback":
            return self._send(200, A.to_json(done), "application/json")
        alert = A.load_alert(self.es, form["alert_id"])
        return self._send(200, A.render_form(A.form_data(alert), done=done).encode())

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def main() -> int:
    Handler.es = ES()
    port = int(os.environ.get("FEEDBACK_PORT", "8080"))
    print(f"feedback_web 在 :{port}（GET /feedback?alert=<alert_id>）", flush=True)
    ThreadingHTTPServer(("", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
