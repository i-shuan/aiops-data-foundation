"""python3 -m api：用 uvicorn 跑 server.py 的 FastAPI app（helm chart 的 args: ["api"]）。路由、CORS、ES 的注入點都在 server.py。"""
from __future__ import annotations

import os
import sys

import uvicorn

import aiops.interface.jobs.worker as tasks
from aiops.core.connection import ES

from aiops.interface.web.routes import app, set_es, state    # noqa: F401  uvicorn api.__main__:app、舊的 import 路徑都還找得到


def main() -> int:
    set_es(ES())
    port = int(os.environ.get("API_PORT", "8080"))
    # 平常放在 Ingress 後面、由 Ingress 做 TLS，這裡走 http 就好。要直接對外（沒有 Ingress）就給憑證，改走 https：
    #   API_TLS_CERT=<cert.pem> API_TLS_KEY=<key.pem>
    cert, key = os.environ.get("API_TLS_CERT"), os.environ.get("API_TLS_KEY")
    tls = bool(cert and key)
    print(f"api 在 :{port}（{'https' if tls else 'http'}；POST /api/anomalies、/api/rca；GET /api/alerts；Grafana 表單用 /api/alert、/api/feedback、"
          f"/api/missed-form、/api/missed；備用網頁 GET /feedback?alert=<alert_id>；佇列：{'有' if tasks.configured() else '沒有（一隻 pod 的模式）'}）", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, ssl_certfile=cert if tls else None, ssl_keyfile=key if tls else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
