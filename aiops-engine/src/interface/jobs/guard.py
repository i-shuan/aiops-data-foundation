"""排程器的看守（interface/jobs/runner.serve 每分鐘叫一次；docs/pdca/meta/2026-11-pg-resilience.md）。

  1. 資料庫連得上嗎（連一下、SELECT 1；不重試，每分鐘看一次就夠）。
  2. 連得上：補寫本機日誌裡別的程式留下的（common/infra/journal.py）；打外部看門狗（AIOPS_WATCHDOG_URL）。
  3. 連不上超過 AIOPS_FALLBACK_AFTER_MINUTES：直接打 AIOPS_FALLBACK_WEBHOOK（不經過 Grafana：Grafana 的告警也讀這個資料庫，
     資料庫掛了它也看不到）；之後每 60 分鐘提醒一次；恢復時再通知一次。
看守自己出錯只印一行，不影響排程。
"""
from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta

import psycopg

from common.config.settings import settings
from common.infra.pg import DB

REMIND = timedelta(minutes=60)


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def _get(url: str) -> None:
    urllib.request.urlopen(url, timeout=10).read()


def db_alive(dsn: str) -> bool:
    env = settings()
    try:
        with psycopg.connect(dsn, connect_timeout=min(env.aiops_pg_connect_timeout, 10), application_name=f"{env.aiops_pg_app_name}-guard") as c:
            c.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


class Guard:
    def __init__(self, log, post=_post, get=_get, alive=db_alive):
        self.log, self.post, self.get, self.alive = log, post, get, alive
        self.down_since: datetime | None = None
        self.notified_at: datetime | None = None
        self.db: DB | None = None
        self.thread: threading.Thread | None = None

    def kick(self, now: datetime) -> None:
        """背景跑一次 check（上一次還沒跑完就這次不跑）：連資料庫最多等幾秒，不拖到排程。"""
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.check, args=(now,), name="guard", daemon=True)
        self.thread.start()

    def check(self, now: datetime) -> None:
        env = settings()
        if not env.aiops_pg_dsn:
            return
        try:
            ok = self.alive(env.aiops_pg_dsn)
        except Exception as e:  # noqa: BLE001
            self.log(f"看守：檢查資料庫失敗（{e}）")
            return
        if ok:
            self._up(now, env)
        else:
            self._down(now, env)

    def _up(self, now: datetime, env) -> None:
        if self.down_since is not None:
            mins = int((now - self.down_since).total_seconds() // 60)
            self.log(f"看守：資料庫恢復（停了約 {mins} 分鐘）")
            if self.notified_at is not None and env.aiops_fallback_webhook:
                self._send(env.aiops_fallback_webhook, {"status": "db_recovered", "since": self.down_since.isoformat(),
                                                        "text": f"AIOps：PostgreSQL 恢復了（停了約 {mins} 分鐘）。停的期間 detect 會自動補算（最多 6 小時）"})
            self.down_since = self.notified_at = None
        try:
            if env.aiops_journal_dir:
                self.db = self.db or DB(env.aiops_pg_dsn)
                n = self.db.recover_journal()
                if n:
                    self.log(f"看守：本機日誌補寫 {n} 筆")
        except Exception as e:  # noqa: BLE001
            self.log(f"看守：本機日誌補寫失敗（{type(e).__name__}: {e}）；下一分鐘再試")
        if env.aiops_watchdog_url:
            try:
                self.get(env.aiops_watchdog_url)
            except Exception as e:  # noqa: BLE001
                self.log(f"看守：打外部看門狗失敗（{e}）")

    def _down(self, now: datetime, env) -> None:
        if self.down_since is None:
            self.down_since = now
            self.log("看守：資料庫連不上")
        late = now - self.down_since >= timedelta(minutes=env.aiops_fallback_after_minutes)
        due = self.notified_at is None or now - self.notified_at >= REMIND
        if late and due and env.aiops_fallback_webhook:
            mins = int((now - self.down_since).total_seconds() // 60)
            self._send(env.aiops_fallback_webhook, {
                "status": "db_down", "since": self.down_since.isoformat(),
                "text": f"AIOps：PostgreSQL 已經 {mins} 分鐘連不上。這段期間 engine 不判斷、Grafana 的告警與儀表板也看不到；不是沒事。"})
            self.notified_at = now

    def _send(self, url: str, payload: dict) -> None:
        try:
            self.post(url, payload)
            self.log(f"看守：已通知（{payload['status']}）")
        except Exception as e:  # noqa: BLE001
            self.log(f"看守：通知送不出去（{e}）")
