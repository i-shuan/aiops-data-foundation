"""python3 -m main api：用 uvicorn 跑 routes.py 的 FastAPI app（helm chart 的 args: ["api"]）。路由、CORS、ES 的注入點都在 routes.py。"""
from __future__ import annotations

import sys

import uvicorn

import interface.jobs.worker as tasks
from common.config.settings import settings
from common.infra.es import ES

from interface.web.routes import app, set_es, state    # noqa: F401  uvicorn api.__main__:app、舊的 import 路徑都還找得到


def main() -> int:
    set_es(ES())
    cfg = settings()
    port = cfg.api_port
    # 平常放在 Ingress 後面、由 Ingress 做 TLS，這裡走 http 就好。要直接對外（沒有 Ingress）就給憑證，改走 https：
    #   API_TLS_CERT=<cert.pem> API_TLS_KEY=<key.pem>
    cert, key = cfg.tls or (None, None)
    tls = cfg.tls is not None
    print(f"api 在 :{port}（{'https' if tls else 'http'}；POST /api/anomalies、/api/rca；GET /api/alerts；Grafana 表單用 /api/alert、/api/feedback、"
          f"/api/missed-form、/api/missed；備用網頁 GET /feedback?alert=<alert_id>；佇列：{'有' if tasks.configured() else '沒有（一隻 pod 的模式）'}）", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, ssl_certfile=cert if tls else None, ssl_keyfile=key if tls else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
