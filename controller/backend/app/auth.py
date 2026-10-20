"""權限（計畫 3.3）：

- agent 端點：`Authorization: Bearer <agent token>`（資料庫只存 SHA-256）
- 前端端點：帳號登入後的 session cookie；admin 才能重試、取消、重送
- 確認頁：只認一次性 token（在 engine 處理）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine import FlowEngine
from app.models import ROLE_ADMIN, Agent, AgentToken, User, UserSession
from app.security import new_token, sha256_hex


async def get_db(request: Request) -> AsyncIterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_engine(request: Request) -> FlowEngine:
    return request.app.state.engine


@dataclass
class Principal:
    kind: str  # agent／user
    agent: Agent | None = None
    token: AgentToken | None = None
    user: User | None = None

    @property
    def actor(self) -> str:
        if self.kind == "agent" and self.agent is not None:
            return f"agent:{self.agent.id}"
        if self.user is not None:
            return f"user:{self.user.username}"
        return "unknown"

    @property
    def is_admin(self) -> bool:
        return self.kind == "user" and self.user is not None and self.user.role == ROLE_ADMIN


def _unauthorized(message: str = "需要登入或 agent token") -> HTTPException:
    return HTTPException(status_code=401, detail={"code": "unauthorized", "message": message})


def _forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": "forbidden", "message": message})


def _agent_from_request(request: Request, db: Session) -> Principal | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    raw = header[7:].strip()
    if not raw:
        return None
    token = db.scalar(select(AgentToken).where(AgentToken.token_hash == sha256_hex(raw)))
    if token is None or token.revoked_at is not None or not token.agent.enabled:
        raise _unauthorized("agent token 無效或已停用")
    now = request.app.state.engine.clock()
    token.last_used_at = now
    db.commit()
    return Principal(kind="agent", agent=token.agent, token=token)


def _user_from_request(request: Request, db: Session) -> Principal | None:
    cookie = request.cookies.get(request.app.state.settings.cookie_name)
    if not cookie:
        return None
    session = db.scalar(select(UserSession).where(UserSession.token_hash == sha256_hex(cookie)))
    now = request.app.state.engine.clock()
    if session is None or session.expires_at <= now or session.user.disabled:
        return None
    return Principal(kind="user", user=session.user)


async def optional_principal(request: Request, db: Session = Depends(get_db)) -> Principal | None:
    agent = _agent_from_request(request, db)
    if agent is not None:
        return agent
    return _user_from_request(request, db)


async def require_agent(principal: Principal | None = Depends(optional_principal)) -> Principal:
    if principal is None or principal.kind != "agent":
        raise _unauthorized("需要 agent token")
    return principal


async def require_user(principal: Principal | None = Depends(optional_principal)) -> Principal:
    if principal is None or principal.kind != "user":
        raise _unauthorized("需要登入")
    return principal


async def require_admin(principal: Principal = Depends(require_user)) -> Principal:
    if not principal.is_admin:
        raise _forbidden("只有 admin 可以執行這個操作")
    return principal


async def require_any(principal: Principal | None = Depends(optional_principal)) -> Principal:
    if principal is None:
        raise _unauthorized()
    return principal


async def require_agent_or_admin(principal: Principal = Depends(require_any)) -> Principal:
    if principal.kind == "agent" or principal.is_admin:
        return principal
    raise _forbidden("需要 agent token 或 admin")


def create_session(db: Session, user: User, *, now, ttl_hours: float) -> str:  # noqa: ANN001
    raw = new_token("fcs")
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=sha256_hex(raw),
            created_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
        )
    )
    return raw


def delete_session(db: Session, raw: str) -> None:
    session = db.scalar(select(UserSession).where(UserSession.token_hash == sha256_hex(raw)))
    if session is not None:
        db.delete(session)
