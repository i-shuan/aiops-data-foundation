"""HTTP 這一層：標準函式庫的 http.server，邏輯在 app.py。"""
from __future__ import annotations

import json
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.es import ES

from . import app as A


def _cors_origins() -> set[str]:
    """FEEDBACK_CORS_ORIGIN：允許哪些網頁從瀏覽器打 /api/*（例如 Grafana 的網址 https://grafana.example.internal）；逗號分隔；不設就不開。
    Grafana 儀表板裡的表單（Business Forms 外掛）是從瀏覽器直接打這裡，跟 Grafana 不同網域，要開。"""
    return {o.strip().rstrip("/") for o in os.environ.get("FEEDBACK_CORS_ORIGIN", "").split(",") if o.strip()}


class Handler(BaseHTTPRequestHandler):
    es: ES = None

    def _cors(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        allowed = _cors_origins()
        if origin and (origin in allowed or "*" in allowed):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.path.startswith("/api/"):
            self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):    # 瀏覽器跨網域送 JSON 之前會先問一次（preflight）
        self.send_response(204 if self.path.startswith("/api/") else 404)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if u.path == "/api/alert":    # Grafana 表單的預填（initial request）：一層的 JSON，key 對到表單元素的 id
            alert = A.load_alert(self.es, q.get("alert", ""))
            if not alert:
                return self._send(404, A.to_json({"error": f"找不到告警 {q.get('alert')}"}), "application/json")
            return self._send(200, A.to_json(A.form_json(A.form_data(alert))), "application/json")
        if u.path == "/api/missed-form":    # 漏報表單的預填：全空（讓外掛把上一次填的清掉）
            return self._send(200, A.to_json(A.missed_form_json()), "application/json")
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
        if u.path == "/api/missed":    # 漏報（Grafana 第二個表單面板）：只有 JSON，沒有網頁版
            try:
                return self._send(200, A.to_json(A.submit_missed(self.es, json.loads(raw or b"{}"))), "application/json")
            except (KeyError, ValueError) as e:
                return self._send(400, A.to_json({"error": str(e)}), "application/json")
            except Exception as e:
                sys.stderr.write(f"missed submit failed: {e!r}\n")
                return self._send(503, A.to_json({"error": f"沒有存成功（{type(e).__name__}：{str(e)[:120]}）。請稍後再送一次；重送不會重複記錄。"}), "application/json")
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
    srv = ThreadingHTTPServer(("", port), Handler)
    # 平常放在 Ingress 後面、由 Ingress 做 TLS，這裡走 http 就好。要直接對外（沒有 Ingress）就給憑證，改走 https：
    #   FEEDBACK_TLS_CERT=<cert.pem> FEEDBACK_TLS_KEY=<key.pem>
    cert, key = os.environ.get("FEEDBACK_TLS_CERT"), os.environ.get("FEEDBACK_TLS_KEY")
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"feedback_web 在 :{port}（{'https' if cert and key else 'http'}；GET /feedback?alert=<alert_id>；Grafana 表單用 /api/alert、/api/feedback、/api/missed-form、/api/missed）", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
