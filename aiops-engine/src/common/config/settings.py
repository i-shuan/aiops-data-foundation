"""環境變數 → 有型別的 Settings。**程式裡只有這個檔讀 os.environ**（tests/test_settings.py 鎖著）。

為什麼：環境變數原本散在 9 個檔、各自 os.environ.get、各自給預設值、沒有型別（API_PORT 要自己 int）；
打錯字、型別錯、忘了設，要到用的那一刻才炸。集中之後：每個變數的名字、型別、預設值只有一份，型別錯馬上有一行講清楚的錯誤。

用法：`from common.config.settings import settings`，要的地方呼叫 `settings()`；每次呼叫都重讀環境（很便宜），所以測試用
`mock.patch.dict(os.environ, …)` 照樣有效，沒有要手動清的快取。必填的（ES_URL、AIOPS_PG_DSN、LLM_API_KEY）只在真的要用的那一刻檢查
（require_*），不在 import 時檢查——單元測試與 check-docs 光 import 不該需要 ES。

預設值照程式原本的樣子（LLM_PROVIDER 預設 template、AIOPS_PLUGIN 預設 observability）。空字串視同沒設（chart 會給空字串）。
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, ValidationError


class SettingsError(ValueError):
    """環境變數的值不對（型別錯）。str() 講清楚是哪個變數。"""


class MissingSetting(KeyError):
    """要用的環境變數沒設。繼承 KeyError：原本沒設 ES_URL 就是 KeyError，抓 KeyError 的地方照舊。"""

    def __str__(self) -> str:
        return f"環境變數 {self.args[0]} 沒設"


class Settings(BaseModel):
    """欄位名 = 環境變數名的小寫（es_url ← ES_URL）。"""
    model_config = ConfigDict(frozen=True, extra="forbid")

    # ── ES ──
    es_url: str | None = None
    es_api_key: str | None = None
    es_auth: str | None = None           # "user:password"（本機、測試；正式環境用 es_api_key）
    es_ca: str | None = None             # 自簽憑證的 CA 檔
    # ── PostgreSQL（engine 自己的資料：異常、基準線、告警、事故、回饋、調整、對照覆寫）──
    aiops_pg_dsn: str | None = None      # postgresql://user:密碼@host:5432/db（密碼也可以用 libpq 的 PGPASSWORD）
    aiops_pg_schema: str | None = None   # 公司的 PostgreSQL 給的是某個 schema 時填；沒填用 public
    aiops_pg_connect_timeout: int = 10   # 連線最多等幾秒（資料庫連不上時，排程的工作不要卡住）
    aiops_pg_statement_timeout: int = 120   # 一個查詢最多跑幾秒（0 = 不限）；超過就取消，這一輪失敗、下一輪再跑
    aiops_pg_app_name: str = "aiops-engine"   # 連線名稱（DBA 在 pg_stat_activity 看得到是誰在連）
    aiops_archive_dir: str | None = None # detector prune 刪之前歸檔到哪（蓋過 prune.yaml 的 archive.dir）；off = 不歸檔、直接刪
    # ── embedding（相似事故的向量；沒設就只用欄位比對）──
    embedding_url: str | None = None     # OpenAI 相容的 API 根，例如 https://llm-gw.example.com/v1（POST {url}/embeddings）
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    # ── LLM（根因摘要）──
    llm_provider: str = "template"       # template（不呼叫模型）、anthropic、openai
    llm_chat_model: str | None = None    # 沒設就依 provider 用各自的預設（chat_model()）
    llm_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_base_url: str = "https://api.openai.com"
    # ── 情境 ──
    aiops_plugin: str = "observability"  # observability、factory、metrics_only、yaml 路徑、或 套件.模組:名字
    aiops_source: str = "otel"           # observability 的原始資料：otel、apm；並存用逗號隔開（otel,apm），第一個是主要的
    aiops_ingest_config: str | None = None
    # ── 排程 ──
    schedule_stage: int = 1
    schedule_alive_file: str | None = None
    aiops_broker_url: str | None = None  # 分散式的 Redis；沒設就是一隻 pod 的模式
    tz: str = "Asia/Taipei"              # 環境變數 TZ
    # ── api ──
    api_port: int = 8080
    api_cors_origin: str = ""            # 逗號分隔
    api_tls_cert: str | None = None
    api_tls_key: str | None = None
    aiops_api_token: str = ""            # 寫入類端點的 Bearer token；空＝那些端點關著
    # ── 版本 ──
    engine_version: str = "dev"          # chart 填 image tag

    # ── 要用的那一刻才檢查 ──
    def require_es_url(self) -> str:
        if not self.es_url:
            raise MissingSetting("ES_URL")
        return self.es_url

    def require_pg_dsn(self) -> str:
        if not self.aiops_pg_dsn:
            raise MissingSetting("AIOPS_PG_DSN")
        return self.aiops_pg_dsn

    def require_llm_api_key(self) -> str:
        if not self.llm_api_key:
            raise MissingSetting("LLM_API_KEY")
        return self.llm_api_key

    def chat_model(self, default: str) -> str:
        """LLM_CHAT_MODEL；沒設用 default（每個 provider 的預設不一樣）。"""
        return self.llm_chat_model or default

    @property
    def cors_origins(self) -> set[str]:
        """API_CORS_ORIGIN：允許哪些網頁從瀏覽器打 /api/*；逗號分隔；不設就不開。"""
        return {o.strip().rstrip("/") for o in self.api_cors_origin.split(",") if o.strip()}

    @property
    def tls(self) -> tuple[str, str] | None:
        """(cert, key)，兩個都有才走 https。"""
        return (self.api_tls_cert, self.api_tls_key) if self.api_tls_cert and self.api_tls_key else None


def settings(env: Mapping[str, str] | None = None) -> Settings:
    """讀環境變數（或給定的 env）→ Settings。空字串當沒設；型別錯拋 SettingsError。"""
    env = os.environ if env is None else env
    raw = {name: env[name.upper()] for name in Settings.model_fields if env.get(name.upper(), "") != ""}
    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        bad = "；".join(f"{str(err['loc'][0]).upper()} {err['msg']}（值是 {raw.get(str(err['loc'][0]))!r}）" for err in e.errors())
        raise SettingsError(f"環境變數不對：{bad}") from None


def engine_version(env: Mapping[str, str] | None = None) -> str:
    """ENGINE_VERSION 單獨讀（模組載入時就要，anomaly 寫入帶這個）：不驗其他變數，免得別的變數錯了連 import 都失敗。"""
    env = os.environ if env is None else env
    return env.get("ENGINE_VERSION") or "dev"
