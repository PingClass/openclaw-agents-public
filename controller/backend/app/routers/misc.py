"""登入／登出、通知收件匣、模擬入口、SSE、健康檢查。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import (
    Principal,
    create_session,
    delete_session,
    get_db,
    get_engine,
    require_admin,
    require_agent,
    require_any,
    require_user,
)
from app.channels import conversation_id, record_inbound
from app.dispatch import MessageJob, build_sim_message
from app.engine import Forbidden, FlowEngine, Invalid, NotFound
from app.models import CONFIRM_AGENT, CONFIRM_HUMAN, Agent, Approval, ChannelMessage, Identity, Notification, User
from app.schemas import ConfirmModeBody, Login, SimMessage
from app.security import verify_password
from app.views import channel_message_view, identity_brief, iso, notification_view

router = APIRouter()


# ---------------------------------------------------------------- 登入


@router.post("/auth/login", tags=["auth"])
async def login(
    body: Login,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or user.disabled or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail={"code": "bad_credentials",
                                                     "message": "帳號或密碼錯誤"})
    settings = request.app.state.settings
    ctx = engine.context(db, f"user:{user.username}")
    raw = create_session(db, user, now=ctx.now, ttl_hours=settings.session_ttl_hours)
    ctx.audit("user.login", {"role": user.role})
    await engine.commit(ctx)
    response.set_cookie(
        settings.cookie_name,
        raw,
        max_age=int(settings.session_ttl_hours * 3600),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return _me(user)


@router.post("/auth/logout", tags=["auth"])
async def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    raw = request.cookies.get(settings.cookie_name)
    if raw:
        delete_session(db, raw)
        db.commit()
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


def _me(user: User) -> dict:
    return {
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "identity_id": user.identity_id,
    }


@router.get("/auth/me", tags=["auth"])
async def me(principal: Principal = Depends(require_user)):
    return _me(principal.user)


# ---------------------------------------------------------------- 收件匣


def _resolve_recipient(db: Session, principal: Principal, recipient: str | None, *,
                       write: bool = False) -> Identity:
    identity: Identity | None = None
    if recipient:
        filters = [Identity.hrm_employee_id == recipient, Identity.employee_number == recipient,
                   Identity.agent_id == recipient]
        if recipient.isdigit():
            filters.append(Identity.id == int(recipient))
        identity = db.scalar(select(Identity).where(or_(*filters)))
    elif principal.kind == "user" and principal.user.identity_id:
        identity = db.get(Identity, principal.user.identity_id)
    elif principal.kind == "agent":
        identity = db.scalar(select(Identity).where(Identity.agent_id == principal.agent.id))
    if identity is None:
        raise NotFound("找不到收件人", code="recipient_not_found")
    _check_inbox_access(principal, identity, write=write)
    return identity


def _check_inbox_access(principal: Principal, identity: Identity, *, write: bool = False) -> None:
    if principal.kind == "agent" and identity.agent_id != principal.agent.id:
        raise Forbidden("agent 只能讀自己扮演的人的收件匣", code="not_your_inbox")
    # 標已讀是寫入：viewer 只能看，只有 admin 或收件人本人能標
    if write and principal.kind == "user" and not principal.is_admin \
            and principal.user.identity_id != identity.id:
        raise Forbidden("只有收件人本人或 admin 可以標為已讀", code="not_your_inbox")


def _can_see_links(principal: Principal, identity: Identity) -> bool:
    """確認連結等同確認權：只給收件人本人（或扮演他的 agent）與 admin 看。

    確認者被切成「真人確認」（confirm_mode=human）時，扮演他的 agent 也看不到連結，
    即使 agent-inbox 沒有先查詢確認方式，也無法代為確認。
    """
    if principal.kind == "agent":
        return identity.agent_id == principal.agent.id and identity.confirm_mode != CONFIRM_HUMAN
    return principal.is_admin or principal.user.identity_id == identity.id


@router.get("/inbox", tags=["inbox"])
async def inbox(
    recipient: str | None = None,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    identity = _resolve_recipient(db, principal, recipient)
    query = select(Notification).where(Notification.recipient_id == identity.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    unread = db.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.recipient_id == identity.id, Notification.read_at.is_(None)
        )
    )
    show_link = _can_see_links(principal, identity)
    items = db.scalars(query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)).all()
    approval_ids = {n.approval_id for n in items if n.approval_id}
    approvals = {
        a.id: a for a in db.scalars(select(Approval).where(Approval.id.in_(approval_ids)))
    } if approval_ids else {}
    now = engine.clock()
    return {
        "recipient": identity_brief(identity),
        "unread_count": unread or 0,
        "items": [notification_view(n, show_link=show_link, approval=approvals.get(n.approval_id), now=now)
                  for n in items],
    }


@router.get("/inbox/recipients", tags=["inbox"])
async def inbox_recipients(principal: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """有收件匣的人（前端切換收件人用），附未讀數。"""
    rows = db.execute(
        select(Identity, func.count(Notification.id))
        .outerjoin(Notification, (Notification.recipient_id == Identity.id) & Notification.read_at.is_(None))
        .group_by(Identity.id)
        .order_by(Identity.display_name)
    ).all()
    return {"items": [{**identity_brief(identity), "unread_count": count} for identity, count in rows]}


@router.post("/inbox/{notification_id}/read", tags=["inbox"])
async def mark_read(
    notification_id: int,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise NotFound("找不到通知", code="notification_not_found")
    _check_inbox_access(principal, notification.recipient, write=True)
    if notification.read_at is None:
        notification.read_at = engine.clock()
        db.commit()
    return {"id": notification.id, "read_at": iso(notification.read_at)}


@router.post("/inbox/read-all", tags=["inbox"])
async def mark_all_read(
    recipient: str | None = None,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    identity = _resolve_recipient(db, principal, recipient, write=True)
    now = engine.clock()
    count = 0
    for notification in db.scalars(
        select(Notification).where(Notification.recipient_id == identity.id,
                                   Notification.read_at.is_(None))
    ):
        notification.read_at = now
        count += 1
    db.commit()
    return {"marked": count}


# ---------------------------------------------------------------- 確認方式：agent 代理／真人（計畫 9.4）


def _confirm_mode_view(identity: Identity) -> dict:
    mode = identity.confirm_mode or CONFIRM_AGENT
    return {"recipient": identity_brief(identity), "confirm_mode": mode, "human": mode == CONFIRM_HUMAN,
            "agent_id": identity.agent_id}


@router.get("/inbox/confirm-mode", tags=["inbox"])
async def get_confirm_mode(
    recipient: str | None = None,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    """這位確認者目前由誰確認。員工 agent（agent-inbox）用自己的 token 查，human 時不處理確認。"""
    identity = _resolve_recipient(db, principal, recipient)
    return _confirm_mode_view(identity)


@router.put("/inbox/confirm-mode", tags=["inbox"])
async def set_confirm_mode(
    body: ConfirmModeBody,
    recipient: str = Query(min_length=1, max_length=64,
                           description="hrm_employee_id、員工編號、Identity id 或扮演他的 agent id"),
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    """admin 把確認者切成真人確認或 agent 代理（課堂 demo 由講師親自確認），寫入稽核。"""
    identity = _resolve_recipient(db, principal, recipient)
    previous = identity.confirm_mode or CONFIRM_AGENT
    ctx = engine.context(db, principal.actor)
    if previous != body.mode:
        identity.confirm_mode = body.mode
        identity.updated_at = ctx.now
        ctx.audit("identity.confirm_mode", {"hrm_employee_id": identity.hrm_employee_id,
                                            "display_name": identity.display_name,
                                            "agent_id": identity.agent_id,
                                            "from": previous, "to": body.mode})
        ctx.emit("identity.updated", {"identity_id": identity.id, "confirm_mode": body.mode})
    await engine.commit(ctx)
    return {**_confirm_mode_view(identity), "changed": previous != body.mode}


# ---------------------------------------------------------------- 模擬入口（計畫 9.5）


@router.post("/sim/messages", status_code=202, tags=["sim"])
async def sim_message(
    body: SimMessage,
    principal: Principal = Depends(require_agent),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    if not principal.token.can_simulate:
        raise Forbidden("這個 token 沒有模擬權限", code="not_simulator")
    if body.target_agent not in engine.settings.sim_targets:
        raise Invalid(f"不允許模擬傳訊給 {body.target_agent}", code="target_not_allowed")
    target = db.get(Agent, body.target_agent)
    if target is None or not target.enabled:
        raise Invalid(f"找不到或已停用的 agent：{body.target_agent}", code="target_not_found")
    ctx = engine.context(db, principal.actor)
    conversation = conversation_id(body.target_agent, body.persona.external_customer_id)
    # 只記錄、轉送：controller 不解讀訊息內容，不會因此建立或決定任何人工確認
    inbound = record_inbound(db, now=ctx.now, agent_id=body.target_agent,
                             external_customer_id=body.persona.external_customer_id,
                             display_name=body.persona.display_name, body=body.message,
                             created_by=principal.actor)
    ctx.audit(
        "sim.message",
        {
            "target_agent": body.target_agent,
            "external_customer_id": body.persona.external_customer_id,
            "display_name": body.persona.display_name,
            "message": body.message[:500],
            "message_id": inbound.id,
            "conversation_id": conversation,
        },
    )
    # 實際送出走派工介面（與步驟派工相同的 Dispatcher）
    ctx.jobs.append(
        MessageJob(
            agent_id=body.target_agent,
            message=build_sim_message(
                external_customer_id=body.persona.external_customer_id,
                display_name=body.persona.display_name,
                message=body.message,
                conversation_id=conversation,
            ),
            tag="sim",
            # 同一位模擬客戶沿用同一個 session，和真客戶在 LINE 上的對話一致
            session=conversation,
        )
    )
    await engine.commit(ctx)
    return {"status": "dispatched", "target_agent": body.target_agent, "message_id": inbound.id,
            "conversation_id": conversation}


@router.get("/sim/messages", tags=["sim"])
async def sim_conversation(
    external_customer_id: str = Query(min_length=1, max_length=64),
    target_agent: str = Query(default="cs-agent", max_length=64),
    since: str | None = Query(default=None, description="只回這個時間之後的訊息（ISO 8601）"),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_agent),
    db: Session = Depends(get_db),
):
    """扮客戶的 agent 讀模擬對話（含流程送出的回覆）。只接受有模擬權限的 token。"""
    if not principal.token.can_simulate:
        raise Forbidden("這個 token 沒有模擬權限", code="not_simulator")
    conversation = conversation_id(target_agent, external_customer_id)
    query = select(ChannelMessage).where(ChannelMessage.conversation_id == conversation)
    if since:
        try:
            bound = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Invalid(f"since 格式錯誤：{since}", code="invalid_date") from exc
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=UTC)
        query = query.where(ChannelMessage.created_at > bound)
    rows = db.scalars(query.order_by(ChannelMessage.id.desc()).limit(limit)).all()
    return {"conversation_id": conversation,
            "items": [channel_message_view(message) for message in reversed(rows)]}


# ---------------------------------------------------------------- SSE


def _sse(event_id: int | None, event_type: str, data: dict) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


@router.get("/events", tags=["events"])
async def events(
    request: Request,
    principal: Principal = Depends(require_user),
    engine: FlowEngine = Depends(get_engine),
):
    bus = request.app.state.bus
    keepalive = request.app.state.settings.sse_keepalive
    queue = bus.subscribe()

    async def stream():
        try:
            # 連上就先送一個事件，前端據此確認 SSE 可用（否則改用輪詢）
            yield _sse(None, "hello", {"server_time": iso(engine.clock())})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                yield _sse(event["id"], event["type"], event["data"])
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
