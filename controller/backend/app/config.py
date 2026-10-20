"""設定：一律從 FLOW_CONTROLLER_* 環境變數讀取。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FLOW_CONTROLLER_{name}", default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


@dataclass
class Settings:
    # 資料庫（SQLite）
    db_url: str = "sqlite:///./data/controller.db"
    # 確認連結、收件匣連結使用的對外網址（前端的位址）
    public_url: str = "http://localhost:18090"
    # 派工方式：openclaw（正式）或 fake（開發與測試）
    dispatcher: str = "openclaw"
    openclaw_bin: str = "openclaw"
    # 呼叫 openclaw agent 一輪的逾時（秒）
    dispatch_timeout: float = 600.0
    # 同時派工的上限（避免一次叫起太多 agent）
    max_concurrent_dispatch: int = 3
    # 逾時掃描間隔（秒）；0 代表不啟動背景掃描（測試用）
    scan_interval: float = 60.0
    # 一次性確認連結的預設有效期限（小時）
    approval_ttl_hours: float = 24.0
    # 登入 session 有效期限（小時）
    session_ttl_hours: float = 12.0
    cookie_secure: bool = False
    cookie_name: str = "fc_session"
    # 顯示用時區（卡住原因中的「18:00 截止」等）
    tz: str = "Asia/Taipei"
    cors_origins: list[str] = field(default_factory=list)
    # 模擬入口（/sim/messages）允許送達的 agent
    sim_targets: list[str] = field(default_factory=lambda: ["cs-agent"])
    # SSE keepalive 間隔（秒）
    sse_keepalive: float = 15.0
    # OpenClaw gateway 健康檢查（計畫 3.7）：每 gateway_check_interval 秒 GET <gateway_url>/readyz；
    # 0 代表不檢查（測試用）
    gateway_url: str = "http://127.0.0.1:18789"
    gateway_timeout: float = 3.0
    gateway_check_interval: float = 60.0
    # 課堂示範模式（流程 input 帶 demo=true，時限縮短）；設 0 就不接受示範流程
    demo_enabled: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_url=_env("DB_URL", cls.db_url),
            public_url=_env("PUBLIC_URL", cls.public_url).rstrip("/"),
            dispatcher=_env("DISPATCHER", cls.dispatcher),
            openclaw_bin=_env("OPENCLAW_BIN", cls.openclaw_bin),
            dispatch_timeout=float(_env("DISPATCH_TIMEOUT", str(cls.dispatch_timeout))),
            max_concurrent_dispatch=int(
                _env("MAX_CONCURRENT_DISPATCH", str(cls.max_concurrent_dispatch))
            ),
            scan_interval=float(_env("SCAN_INTERVAL", str(cls.scan_interval))),
            approval_ttl_hours=float(_env("APPROVAL_TTL_HOURS", str(cls.approval_ttl_hours))),
            session_ttl_hours=float(_env("SESSION_TTL_HOURS", str(cls.session_ttl_hours))),
            cookie_secure=_env_bool("COOKIE_SECURE", cls.cookie_secure),
            tz=_env("TZ", cls.tz),
            cors_origins=_env_list("CORS_ORIGINS", ""),
            sim_targets=_env_list("SIM_TARGETS", "cs-agent"),
            sse_keepalive=float(_env("SSE_KEEPALIVE", str(cls.sse_keepalive))),
            gateway_url=_env("GATEWAY_URL", cls.gateway_url).rstrip("/"),
            gateway_timeout=float(_env("GATEWAY_TIMEOUT", str(cls.gateway_timeout))),
            gateway_check_interval=float(
                _env("GATEWAY_CHECK_INTERVAL", str(cls.gateway_check_interval))
            ),
            demo_enabled=_env_bool("DEMO_ENABLED", cls.demo_enabled),
        )
