"""Agent 狀態與總覽統計（計畫 3.7）。agent 狀態的來源是 controller 自己的步驟紀錄。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal, get_db, get_engine, require_user
from app.engine import FlowEngine, Invalid, NotFound
from app.models import (
    APPROVAL_PENDING,
    FLOW_ACTIVE,
    FLOW_EXPIRED,
    FLOW_FAILED,
    FLOW_RUNNING,
    FLOW_SUCCEEDED,
    FLOW_WAITING_APPROVAL,
    STEP_DISPATCHED,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    Agent,
    Approval,
    Flow,
    Step,
)
from app.routers.flows import _parse_bound
from app.views import agent_names, flow_summary, identity_brief, iso, seconds_between, today_bounds

router = APIRouter(tags=["agents"])

# Agent 詳情「結果」篩選：running 包含已派出（dispatched）與執行中
_RESULT_FILTERS: dict[str, tuple[str, ...]] = {
    "succeeded": (STEP_SUCCEEDED,),
    "failed": (STEP_FAILED,),
    "running": (STEP_DISPATCHED, STEP_RUNNING),
    "skipped": (STEP_SKIPPED,),
}


def _agent_card(db: Session, agent: Agent, now: datetime, tz: str) -> dict[str, Any]:
    start, end = today_bounds(now, tz)
    active_rows = db.execute(
        select(Step, Flow)
        .join(Flow, Step.flow_id == Flow.id)
        .where(
            Step.assignee == agent.id,
            Step.status.in_((STEP_DISPATCHED, STEP_RUNNING)),
            Flow.status.in_(FLOW_ACTIVE),
        )
    ).all()
    today_steps = db.scalars(
        select(Step).where(
            Step.assignee == agent.id,
            Step.finished_at >= start,
            Step.finished_at < end,
            Step.status.in_((STEP_SUCCEEDED, STEP_FAILED)),
        )
    ).all()
    succeeded = [s for s in today_steps if s.status == STEP_SUCCEEDED]
    failed = [s for s in today_steps if s.status == STEP_FAILED]
    durations = [seconds_between(s.dispatched_at, s.finished_at) for s in succeeded]
    durations = [d for d in durations if d is not None]

    if active_rows:
        status = "running"
    elif agent.last_error_at and agent.last_error_at >= start and (
        agent.last_seen_at is None or agent.last_error_at >= agent.last_seen_at
    ):
        status = "error"
    else:
        status = "idle"
    return {
        "id": agent.id,
        "display_name": agent.display_name,
        "emoji": agent.emoji,
        "role": agent.role,
        "enabled": agent.enabled,
        "status": status,
        "current_steps": [
            {
                "flow_id": flow.id,
                "flow_title": flow.title,
                "step": step.name,
                "step_title": step.title,
                "status": step.status,
                "dispatched_at": iso(step.dispatched_at),
                "deadline_at": iso(step.deadline_at),
                "overdue": bool(step.deadline_at and now > step.deadline_at),
            }
            for step, flow in active_rows
        ],
        "today": {"succeeded": len(succeeded), "failed": len(failed)},
        "avg_duration_seconds": int(sum(durations) / len(durations)) if durations else None,
        "last_seen_at": iso(agent.last_seen_at),
        "last_error_at": iso(agent.last_error_at),
        "last_error": agent.last_error,
    }


@router.get("/agents")
async def list_agents(
    role: str = Query(default="assistant", description="assistant／employee／simulator／all"),
    principal: Principal = Depends(require_user),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    query = select(Agent).order_by(Agent.id)
    if role != "all":
        query = query.where(Agent.role == role)
    now = engine.clock()
    return {
        "gateway": engine.gateway.snapshot(),
        "items": [_agent_card(db, agent, now, engine.settings.tz) for agent in db.scalars(query)],
    }


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: str,
    result: str | None = Query(default=None, description="succeeded／failed／running／skipped"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_user),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotFound("找不到 agent", code="agent_not_found")
    now = engine.clock()
    card = _agent_card(db, agent, now, engine.settings.tz)
    query = (
        select(Step, Flow)
        .join(Flow, Step.flow_id == Flow.id)
        .where(Step.assignee == agent_id, Step.dispatched_at.is_not(None))
        .order_by(Step.dispatched_at.desc())
    )
    if result:
        statuses = _RESULT_FILTERS.get(result)
        if statuses is None:
            raise Invalid(f"未知的結果：{result}", code="invalid_result")
        query = query.where(Step.status.in_(statuses))
    start = _parse_bound(from_, engine.settings.tz, end=False)
    finish = _parse_bound(to, engine.settings.tz, end=True)
    if start:
        query = query.where(Step.dispatched_at >= start)
    if finish:
        query = query.where(Step.dispatched_at < finish)
    card["steps"] = [
        {
            "flow_id": flow.id,
            "flow_title": flow.title,
            "flow_type": flow.type,
            "step": step.name,
            "step_title": step.title,
            "status": step.status,
            "attempts": step.attempts,
            "dispatched_at": iso(step.dispatched_at),
            "finished_at": iso(step.finished_at),
            "duration_seconds": seconds_between(step.dispatched_at, step.finished_at),
            "summary": step.summary,
            "error": step.error,
            "error_kind": step.error_kind,
        }
        for step, flow in db.execute(query.limit(limit)).all()
    ]
    card["gateway"] = engine.gateway.snapshot()
    return card


@router.get("/stats/summary")
async def stats_summary(
    principal: Principal = Depends(require_user),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    now = engine.clock()
    start, end = today_bounds(now, engine.settings.tz)
    names = agent_names(db)
    active = db.scalars(select(Flow).where(Flow.status.in_(FLOW_ACTIVE)).order_by(Flow.created_at)).all()
    summaries = [flow_summary(db, engine, flow, now, names) for flow in active]
    stuck = sorted(
        (s for s in summaries if s["stuck"]), key=lambda s: s["stuck"]["waited_seconds"], reverse=True
    )

    def _count_finished(statuses: tuple[str, ...]) -> int:
        return db.scalar(
            select(func.count()).select_from(Flow).where(
                Flow.status.in_(statuses), Flow.finished_at >= start, Flow.finished_at < end
            )
        ) or 0

    pending = db.execute(
        select(Approval, Flow)
        .join(Flow, Approval.flow_id == Flow.id)
        .where(Approval.status == APPROVAL_PENDING, Flow.status == FLOW_WAITING_APPROVAL)
        .order_by(Approval.created_at)
    ).all()
    pending_items = []
    for approval, flow in pending:
        step = engine.step_row(flow, approval.step)
        deadline = step.deadline_at
        pending_items.append(
            {
                "approval_id": approval.id,
                "flow_id": flow.id,
                "flow_title": flow.title,
                "step": step.name,
                "step_title": step.title,
                "approver": identity_brief(approval.approver),
                "question": approval.question,
                "deadline_at": iso(deadline),
                "remaining_seconds": max(0, int((deadline - now).total_seconds())) if deadline else None,
            }
        )

    return {
        "generated_at": iso(now),
        "counts": {
            # 「進行中」只算 agent 或系統正在處理的（status=running），不含等待確認，兩個數字不重疊；
            # active 是兩者合計（進行中＋等待確認）
            "running": sum(1 for f in active if f.status == FLOW_RUNNING),
            "waiting_approval": sum(1 for f in active if f.status == FLOW_WAITING_APPROVAL),
            "active": len(active),
            "stuck": len(stuck),
            "succeeded_today": _count_finished((FLOW_SUCCEEDED,)),
            "failed_today": _count_finished((FLOW_FAILED, FLOW_EXPIRED)),
        },
        "stuck": stuck,
        "pending_approvals": pending_items,
        # OpenClaw gateway：ok／offline（gateway 離線）／degraded（頻道異常）／unknown（尚未檢查）
        "gateway": engine.gateway.snapshot(),
    }
