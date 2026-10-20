"""API 回應的序列化。所有對外輸出都經過 sanitize，不含電話、地址，也不含任何 token。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine import FlowEngine
from app.models import (
    FLOW_TERMINAL,
    HUMAN,
    Agent,
    Approval,
    AuditLog,
    ChannelMessage,
    Flow,
    Identity,
    Notification,
    Step,
)
from app.sanitize import sanitize
from app.security import sha256_hex


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def seconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def today_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    return local_day_bounds(now.astimezone(ZoneInfo(tz)).date(), tz)


def identity_brief(identity: Identity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "id": identity.id,
        "hrm_employee_id": identity.hrm_employee_id,
        "employee_number": identity.employee_number,
        "display_name": identity.display_name,
        "role": identity.role,
        "department": identity.department,
        # agent（員工 agent 代為確認）／human（真人確認）
        "confirm_mode": identity.confirm_mode or "agent",
    }


def _agent_names(db: Session) -> dict[str, str]:
    return {agent.id: agent.display_name or agent.id for agent in db.scalars(select(Agent))}


def waiting_for(db: Session, flow: Flow, step: Step | None, agent_names: dict[str, str]) -> dict | None:
    if step is None or flow.status in FLOW_TERMINAL:
        return None
    if step.assignee == HUMAN:
        name = FlowEngine.approver_name(db, flow, step)
        return {"kind": "human", "id": None, "name": name or "（未指定）"}
    return {"kind": "agent", "id": step.assignee, "name": agent_names.get(step.assignee, step.assignee)}


def step_view(step: Step, now: datetime, *, stuck: dict | None = None) -> dict[str, Any]:
    end = step.finished_at or (now if step.dispatched_at and step.status not in ("pending", "skipped") else None)
    return {
        "name": step.name,
        "title": step.title,
        "kind": step.kind,
        "assignee": step.assignee,
        "status": step.status,
        "summary": step.summary,
        "error": step.error,
        "error_kind": step.error_kind,
        "attempts": step.attempts,
        "failures": step.failures,
        "max_attempts": step.max_attempts,
        "input": sanitize(step.input) if step.input else None,
        "output": sanitize(step.output) if step.output else None,
        "deadline_at": iso(step.deadline_at),
        "dispatched_at": iso(step.dispatched_at),
        "started_at": iso(step.started_at),
        "finished_at": iso(step.finished_at),
        "duration_seconds": seconds_between(step.dispatched_at, end),
        "stuck": stuck,
    }


def flow_summary(db: Session, engine: FlowEngine, flow: Flow, now: datetime,
                 agent_names: dict[str, str] | None = None) -> dict[str, Any]:
    agent_names = agent_names if agent_names is not None else _agent_names(db)
    step = engine.current_step(flow)
    stuck = engine.stuck_for(db, flow, now)
    end = flow.finished_at or now
    return {
        "id": flow.id,
        "type": flow.type,
        "title": flow.title,
        "status": flow.status,
        "revision": flow.revision,
        "current_step": flow.current_step,
        "current_step_title": step.title if step else None,
        "current_step_status": step.status if step else None,
        "waiting_for": waiting_for(db, flow, step, agent_names),
        "elapsed_seconds": seconds_between(flow.created_at, end),
        "step_elapsed_seconds": seconds_between(step.dispatched_at, now) if step and flow.status not in FLOW_TERMINAL else None,
        "stuck": stuck.as_dict() if stuck else None,
        "created_by": flow.created_by,
        # 課堂示範模式（時限縮短）的流程
        "demo": bool(((flow.state or {}).get("input") or {}).get("demo")),
        "created_at": iso(flow.created_at),
        "updated_at": iso(flow.updated_at),
        "finished_at": iso(flow.finished_at),
    }


def approval_view(approval: Approval, now: datetime) -> dict[str, Any]:
    return {
        "id": approval.id,
        "flow_id": approval.flow_id,
        "step": approval.step,
        "approver": identity_brief(approval.approver),
        "question": approval.question,
        "options": approval.options,
        "status": approval.status,
        "decision": approval.decision,
        "comment": approval.comment,
        "decided_at": iso(approval.decided_at),
        "expires_at": iso(approval.expires_at),
        "remaining_seconds": max(0, int((approval.expires_at - now).total_seconds()))
        if approval.status == "pending" else None,
        "sent_count": approval.sent_count,
        "created_at": iso(approval.created_at),
    }


def audit_view(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": entry.id,
        "flow_id": entry.flow_id,
        "actor": entry.actor,
        "action": entry.action,
        "detail": sanitize(entry.detail or {}),
        "created_at": iso(entry.created_at),
    }


def channel_message_view(message: ChannelMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "channel": message.channel,
        "direction": message.direction,
        "conversation_id": message.conversation_id,
        "agent_id": message.agent_id,
        "external_customer_id": message.external_customer_id,
        "display_name": message.display_name,
        "body": message.body,
        "flow_id": message.flow_id,
        "created_at": iso(message.created_at),
    }


def flow_detail(db: Session, engine: FlowEngine, flow: Flow, now: datetime) -> dict[str, Any]:
    summary = flow_summary(db, engine, flow, now)
    stuck = summary["stuck"]
    approvals = db.scalars(
        select(Approval).where(Approval.flow_id == flow.id).order_by(Approval.created_at)
    ).all()
    audit = db.scalars(
        select(AuditLog).where(AuditLog.flow_id == flow.id).order_by(AuditLog.id)
    ).all()
    summary.update(
        {
            "state": sanitize(flow.state or {}),
            "steps": [
                step_view(step, now, stuck=stuck if stuck and stuck["step"] == step.name else None)
                for step in flow.steps
            ],
            "approvals": [approval_view(approval, now) for approval in approvals],
            "audit": [audit_view(entry) for entry in audit],
            # 流程送給客戶的訊息（LINE 接上前是模擬通道）
            "messages": [
                channel_message_view(message)
                for message in db.scalars(
                    select(ChannelMessage).where(ChannelMessage.flow_id == flow.id)
                    .order_by(ChannelMessage.id)
                )
            ],
        }
    )
    return summary


def approval_public_view(db: Session, approval: Approval, now: datetime) -> dict[str, Any]:
    """一次性確認頁：只顯示該步驟需要的內容。"""
    flow = db.get(Flow, approval.flow_id)
    step = next((s for s in flow.steps if s.name == approval.step), None) if flow else None
    return {
        "id": approval.id,
        "flow_title": flow.title if flow else None,
        "step_title": step.title if step else approval.step,
        "approver_name": approval.approver.display_name if approval.approver else None,
        "question": approval.question,
        "options": approval.options,
        "context": sanitize(approval.context or {}),
        "fields": sanitize(approval.fields or []),
        "expires_at": iso(approval.expires_at),
        "remaining_seconds": max(0, int((approval.expires_at - now).total_seconds())),
    }


def _link_token(link: str | None) -> str | None:
    if not link:
        return None
    values = parse_qs(urlsplit(link).query).get("token")
    return values[0] if values else None


def notification_approval_state(notification: Notification, approval: Approval | None,
                                now: datetime | None = None) -> str | None:
    """通知所附確認的目前狀態（收件匣顯示用）。

    pending：連結仍可用；superseded：已重送，這則通知的舊連結失效；
    link_expired：確認還沒結束但連結已過期（需要 admin 重送）；
    decided／expired／cancelled：確認已結束。沒有附確認的通知回 None。
    """
    if not notification.approval_id or approval is None:
        return None
    if approval.status != "pending":
        return approval.status
    token = _link_token(notification.link)
    if token is not None and approval.token_hash != sha256_hex(token):
        return "superseded"
    if now is not None and approval.expires_at is not None and now >= approval.expires_at:
        return "link_expired"
    return "pending"


def notification_view(notification: Notification, *, show_link: bool,
                      approval: Approval | None = None, now: datetime | None = None) -> dict[str, Any]:
    state = notification_approval_state(notification, approval, now)
    decision = None
    if approval is not None and state == "decided" and approval.decision:
        decision = next((o.get("label") for o in approval.options or []
                         if o.get("value") == approval.decision), approval.decision)
    return {
        "id": notification.id,
        "kind": notification.kind,
        "title": notification.title,
        "body": notification.body,
        "link": notification.link if show_link else None,
        "link_hidden": bool(notification.link) and not show_link,
        "flow_id": notification.flow_id,
        "approval_id": notification.approval_id,
        # 確認的狀態：pending／superseded／link_expired／decided／expired／cancelled；非確認通知為 null
        "approval_status": state,
        "approval_decision": decision,
        "created_at": iso(notification.created_at),
        "read_at": iso(notification.read_at),
    }


def agent_names(db: Session) -> dict[str, str]:
    return _agent_names(db)
