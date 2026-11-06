"""HTTP 這一層：FastAPI 的 app（uvicorn 跑，入口 __main__.py）。邏輯在 app.py，端點清單在 __init__.py。
路徑、方法、狀態碼、回應內容、Content-Type 跟原本 http.server 版一樣（Grafana 儀表板直接打這些 URL）：
JSON 一律 A.to_json 的 bytes 原樣回、不走 FastAPI 的序列化；表單用 parse_qs 自己讀、不裝 python-multipart；
沒有的路、方法不對一律 404 的 not found；不開 /docs、/openapi.json。"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

import tasks

from . import app as A

state = SimpleNamespace(es=None)    # ES 連線的單一注入點：main() 放 ES()，測試放假的（set_es）
HTML, JSON, TEXT = "text/html; charset=utf-8", "application/json", "text/plain; charset=utf-8"


def set_es(es) -> None:
    state.es = es


def _cors_origins() -> set[str]:
    """API_CORS_ORIGIN：允許哪些網頁從瀏覽器打 /api/*（例如 Grafana 的網址 https://grafana.example.internal）；逗號分隔；不設就不開。
    Grafana 儀表板裡的表單（Business Forms 外掛）是從瀏覽器直接打這裡，跟 Grafana 不同網域，要開。"""
    return {o.strip().rstrip("/") for o in os.environ.get("API_CORS_ORIGIN", "").split(",") if o.strip()}


def _authorized(headers) -> tuple[bool, str]:
    """寫入類端點（/api/anomalies）要 Authorization: Bearer <AIOPS_API_TOKEN>。沒設 token 一律擋（不會因為忘了設就全開）。"""
    token = os.environ.get("AIOPS_API_TOKEN", "")
    if not token:
        return False, "伺服器沒設 AIOPS_API_TOKEN，這個端點關著"
    got = (headers.get("Authorization") or "").strip()
    if got != f"Bearer {token}":
        return False, "token 不對（Authorization: Bearer <AIOPS_API_TOKEN>）"
    return True, ""


def _cors_headers(request: Request) -> dict[str, str]:
    """Origin 在 API_CORS_ORIGIN 裡才給；每次請求讀環境變數（跟原本一樣，改了不用重啟）。"""
    origin = (request.headers.get("Origin") or "").rstrip("/")
    allowed = _cors_origins()
    if origin and (origin in allowed or "*" in allowed):
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"}
    return {}


def _send(code: int, body: bytes, ctype: str = HTML) -> Response:
    """Content-Type 照給的字串原樣送：走 headers 而不是 media_type，Starlette 才不會自己補 charset（/health 是純 text/plain）。"""
    return Response(content=body, status_code=code, headers={"Content-Type": ctype})


def _query(request: Request) -> dict[str, str]:
    """跟原本一樣用 parse_qs：重複的 key 取第一個、空值不算。"""
    return {k: v[0] for k, v in parse_qs(request.scope.get("query_string", b"").decode("latin-1")).items()}


def _form(raw: bytes, lists: tuple[str, ...] = ()) -> dict:
    """application/x-www-form-urlencoded：lists 裡的欄位保留成 list（checked 複選），其餘取第一個。"""
    return {k: (v if k in lists else v[0]) for k, v in parse_qs(raw.decode("utf-8")).items()}


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)


@app.middleware("http")
async def cors(request: Request, call_next):
    """CORS 只開在 /api/*（網頁本身不開）。瀏覽器跨網域送 JSON 之前會先問一次（preflight）：/api/* 回 204、其他 404。"""
    if request.method == "OPTIONS":
        return Response(status_code=204 if request.url.path.startswith("/api/") else 404, headers=_cors_headers(request))
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers.update(_cors_headers(request))
    return resp


@app.exception_handler(StarletteHTTPException)
async def not_found(request: Request, exc: StarletteHTTPException):
    """沒有這條路、方法不對（GET 打 POST 的端點）：跟原本一樣回 404 的 not found，不回 FastAPI 的 JSON。"""
    if exc.status_code in (404, 405):
        return _send(404, b"not found", "text/plain")
    return _send(exc.status_code, str(exc.detail or "").encode(), TEXT)


# ── GET ──

@app.get("/health")
def health():
    return _send(200, b"ok", "text/plain")


@app.get("/api/alert")    # Grafana 表單的預填（initial request）：一層的 JSON，key 對到表單元素的 id
def api_alert(request: Request):
    q = _query(request)
    alert = A.load_alert(state.es, q.get("alert", ""))
    if not alert:
        return _send(404, A.to_json({"error": f"找不到告警 {q.get('alert')}"}), JSON)
    return _send(200, A.to_json(A.form_json(A.form_data(alert))), JSON)


@app.get("/api/missed-form")    # 漏報表單的預填：全空（讓外掛把上一次填的清掉）
def api_missed_form():
    return _send(200, A.to_json(A.missed_form_json()), JSON)


@app.get("/api/alerts")    # 件的清單（給別的系統：工單、入口網站）
def api_alerts(request: Request):
    q = _query(request)
    try:
        return _send(200, A.to_json(A.list_alerts(state.es, q.get("status", "open"), int(q.get("size", "50")))), JSON)
    except ValueError as e:
        return _send(400, A.to_json({"error": str(e)}), JSON)


@app.get("/missed")    # 漏報的網頁版
def missed_page():
    return _send(200, A.render_missed().encode())


@app.get("/feedback")
@app.get("/report")
def feedback_page(request: Request):
    q = _query(request)
    alert = A.load_alert(state.es, q.get("alert", ""))
    if not alert:
        return _send(404, f"找不到告警 {q.get('alert')}".encode(), TEXT)
    if request.url.path == "/report":
        return _send(200, A.render_report(alert).encode())
    return _send(200, A.render_form(A.form_data(alert), full=q.get("full") == "1", notice=q.get("notice")).encode())


# ── POST：先 await 讀 body，再丟 threadpool 跑（ES 是同步的，不能卡住 event loop；原本是一個請求一條執行緒）──

@app.post("/api/anomalies")    # ML／FDC 推異常進 ⑤（plugins/factory/ingest 同一套對照），有佇列就立刻丟 alert
async def api_anomalies(request: Request):
    return await run_in_threadpool(_anomalies, request, await request.body())


def _anomalies(request: Request, raw: bytes) -> Response:
    ok, why = _authorized(request.headers)
    if not ok:
        return _send(401, A.to_json({"error": why}), JSON)
    try:
        got = A.ingest(state.es, json.loads(raw or b"{}"))
    except LookupError as e:
        return _send(404, A.to_json({"error": str(e)}), JSON)
    except (KeyError, ValueError, TypeError) as e:
        return _send(400, A.to_json({"error": str(e)}), JSON)
    except Exception as e:
        sys.stderr.write(f"anomalies ingest failed: {e!r}\n")
        return _send(503, A.to_json({"error": f"沒有寫進去（{type(e).__name__}：{str(e)[:120]}），請再送一次；重送不會重複"}), JSON)
    got["alert_queued"] = tasks.trigger("alert") if got["written"] else False
    return _send(200, A.to_json(got), JSON)


@app.post("/api/rca")    # Grafana 的「重跑根因分析」按鈕：標在那件上，下一輪 alert 強制重跑
async def api_rca(request: Request):
    return await run_in_threadpool(_api_rca, await request.body())


def _api_rca(raw: bytes) -> Response:
    try:
        got = A.request_rca(state.es, json.loads(raw or b"{}").get("alert_id", ""))
    except LookupError as e:
        return _send(404, A.to_json({"error": str(e)}), JSON)
    except ValueError as e:
        return _send(409, A.to_json({"error": str(e)}), JSON)
    except Exception as e:
        sys.stderr.write(f"rca request failed: {e!r}\n")
        return _send(503, A.to_json({"error": f"沒有記下來（{type(e).__name__}：{str(e)[:120]}），請再按一次"}), JSON)
    got["queued"] = tasks.trigger("alert")
    return _send(200, A.to_json(got), JSON)


@app.post("/missed")    # 網頁版的漏報（表單送出，form-urlencoded）
async def missed_submit(request: Request):
    return await run_in_threadpool(_missed_submit, await request.body())


def _missed_submit(raw: bytes) -> Response:
    form = _form(raw)
    try:
        return _send(200, A.render_missed(done=A.submit_missed(state.es, form)).encode())
    except (KeyError, ValueError) as e:
        return _send(400, A.render_missed(error=str(e), form=form).encode())
    except Exception as e:
        sys.stderr.write(f"missed submit failed: {e!r}\n")
        return _send(503, A.render_missed(error=f"沒有存成功（{type(e).__name__}），請再送一次；重送不會重複記錄。", form=form).encode())


@app.post("/rca")    # 網頁版的重跑根因分析：標完 303 轉回 /feedback，提示放在 notice
async def rca_submit(request: Request):
    return await run_in_threadpool(_rca_submit, await request.body())


def _rca_submit(raw: bytes) -> Response:
    form = _form(raw)
    aid = form.get("alert_id", "")
    try:
        A.request_rca(state.es, aid)
        notice = "已排進佇列，幾秒到一分鐘後重新整理這頁，報告會換新。" if tasks.trigger("alert") else "已記下，下一分鐘的 alert 會重跑，之後重新整理這頁。"
    except LookupError as e:
        return _send(404, str(e).encode(), TEXT)
    except ValueError as e:
        notice = str(e)
    except Exception as e:
        sys.stderr.write(f"rca request failed: {e!r}\n")
        notice = f"沒有記下來（{type(e).__name__}），請再按一次"
    back = f"/feedback?alert={quote(aid)}&notice={quote(notice)}" + ("&full=1" if form.get("full") == "1" else "")
    return Response(content=b"", status_code=303, headers={"Location": back})


@app.post("/api/missed")    # 漏報（Grafana 第二個表單面板）：只有 JSON，沒有網頁版
async def api_missed(request: Request):
    return await run_in_threadpool(_api_missed, await request.body())


def _api_missed(raw: bytes) -> Response:
    try:
        return _send(200, A.to_json(A.submit_missed(state.es, json.loads(raw or b"{}"))), JSON)
    except (KeyError, ValueError) as e:
        return _send(400, A.to_json({"error": str(e)}), JSON)
    except Exception as e:
        sys.stderr.write(f"missed submit failed: {e!r}\n")
        return _send(503, A.to_json({"error": f"沒有存成功（{type(e).__name__}：{str(e)[:120]}）。請稍後再送一次；重送不會重複記錄。"}), JSON)


@app.post("/feedback")        # 網頁版：form-urlencoded，回表單那一頁
@app.post("/api/feedback")    # Grafana 表單：JSON，回 JSON
async def feedback_submit(request: Request):
    return await run_in_threadpool(_feedback_submit, request, await request.body())


def _feedback_submit(request: Request, raw: bytes) -> Response:
    is_api = request.url.path == "/api/feedback"
    if request.headers.get("Content-Type", "").startswith("application/json"):
        form = json.loads(raw or b"{}")
    else:
        form = _form(raw, lists=("checked",))
    try:
        done = A.submit(state.es, form)
    except (KeyError, ValueError) as e:
        if is_api:
            return _send(400, A.to_json({"error": str(e)}), JSON)
        alert = A.load_alert(state.es, form.get("alert_id", ""))
        return _send(400, A.render_form(A.form_data(alert), error=str(e), full=form.get("full") == "1").encode() if alert else str(e).encode())
    except Exception as e:    # ES 掛了、連不上：要回一頁說「沒存成功，請再送一次」，不能把連線關掉讓人猜
        msg = f"沒有存成功（{type(e).__name__}：{str(e)[:120]}）。請稍後再送一次；重送不會重複記錄。"
        sys.stderr.write(f"feedback submit failed: {e!r}\n")
        if is_api:
            return _send(503, A.to_json({"error": msg}), JSON)
        try:
            alert = A.load_alert(state.es, form.get("alert_id", ""))
        except Exception:
            alert = None
        return _send(503, A.render_form(A.form_data(alert), error=msg, full=form.get("full") == "1").encode() if alert else msg.encode())
    if is_api:
        return _send(200, A.to_json(done), JSON)
    alert = A.load_alert(state.es, form["alert_id"])
    return _send(200, A.render_form(A.form_data(alert), done=done, full=form.get("full") == "1").encode())
