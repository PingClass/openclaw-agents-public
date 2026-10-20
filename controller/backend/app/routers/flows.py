"""流程相關 API：啟動、查詢、回報、取消、重試、列表。"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, get_db, get_engine, require_admin, require_agent, require_agent_or_admin, require_any, require_user
from app.engine import FlowEngine, Forbidden, Invalid
from app.models import FLOW_STATUSES, Flow
from app.schemas import CancelBody, FlowCreate, FlowEvent, RevisionBody, StepResult
from app.views import agent_names, flow_detail, flow_summary, local_day_bounds

router = APIRouter(tags=["flows"])


def _parse_bound(value: str | None, tz: str, *, end: bool) -> datetime | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            start, next_day = local_day_bounds(date.fromisoformat(value), tz)
            return next_day if end else start
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Invalid(f"日期格式錯誤：{value}", code="invalid_date") from exc
    if parsed.tzinfo is None:
        from zoneinfo import ZoneInfo

        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed


@router.post("/flows", status_code=201)
async def create_flow(
    body: FlowCreate,
    principal: Principal = Depends(require_agent_or_admin),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    if principal.kind == "agent":
        engine.authorize_start(body.type, principal.agent)
    if engine.wants_demo(body.input):
        # 課堂示範模式：只有 admin 或流程指定的助理 agent（visit：cs-agent）可以啟動
        engine.authorize_demo(body.type, agent=principal.agent, is_admin=principal.is_admin)
    ctx = engine.context(db, principal.actor)
    flow, created = engine.start_flow(
        ctx, body.type, body.input, title=body.title, dedupe_key=body.dedupe_key
    )
    await engine.commit(ctx)
    db.refresh(flow)
    data = flow_detail(db, engine, flow, engine.clock())
    data["created"] = created
    return JSONResponse(data, status_code=201 if created else 200)


@router.get("/flows")
async def list_flows(
    type: str | None = None,
    status: str | None = Query(default=None, description="以逗號分隔多個狀態"),
    stuck: bool | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_user),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    tz = engine.settings.tz
    query = select(Flow).order_by(Flow.created_at.desc())
    if type:
        query = query.where(Flow.type == type)
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        unknown = set(statuses) - set(FLOW_STATUSES)
        if unknown:
            raise Invalid(f"未知的狀態：{', '.join(sorted(unknown))}", code="invalid_status")
        query = query.where(Flow.status.in_(statuses))
    start = _parse_bound(from_, tz, end=False)
    finish = _parse_bound(to, tz, end=True)
    if start:
        query = query.where(Flow.created_at >= start)
    if finish:
        query = query.where(Flow.created_at < finish)

    now = engine.clock()
    names = agent_names(db)
    items = [flow_summary(db, engine, flow, now, names) for flow in db.scalars(query)]
    if stuck is not None:
        items = [item for item in items if (item["stuck"] is not None) == stuck]
    return {"total": len(items), "items": items[offset : offset + limit]}


@router.get("/flows/{flow_id}")
async def get_flow(
    flow_id: str,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    flow = engine.get_flow(db, flow_id)
    if principal.kind == "agent" and not engine.agent_can_view(flow, principal.agent.id):
        raise Forbidden("agent 只能查詢自己啟動或參與的流程", code="not_involved")
    return flow_detail(db, engine, flow, engine.clock())


@router.post("/flows/{flow_id}/steps/{step}/result")
async def report_step_result(
    flow_id: str,
    step: str,
    body: StepResult,
    principal: Principal = Depends(require_agent),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    ctx = engine.context(db, principal.actor)
    flow = engine.report_result(
        ctx,
        flow_id=flow_id,
        step_name=step,
        agent_id=principal.agent.id,
        status=body.status,
        summary=body.summary,
        output=body.output,
        error=body.error,
        error_kind=body.error_kind,
        attempt=body.attempt,
        revision=body.revision,
    )
    await engine.commit(ctx)
    db.refresh(flow)
    return flow_summary(db, engine, flow, engine.clock())


@router.post("/flows/{flow_id}/events")
async def submit_event(
    flow_id: str,
    body: FlowEvent,
    principal: Principal = Depends(require_agent),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    """外部事件。visit 流程：業務本人的員工 agent 送 trip_report（取車、還車里程）。"""
    ctx = engine.context(db, principal.actor)
    flow = engine.submit_event(ctx, flow_id=flow_id, agent_id=principal.agent.id,
                               event=body.model_dump())
    await engine.commit(ctx)
    db.refresh(flow)
    return flow_summary(db, engine, flow, engine.clock())


@router.post("/flows/{flow_id}/cancel")
async def cancel_flow(
    flow_id: str,
    body: CancelBody,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    ctx = engine.context(db, principal.actor)
    flow = engine.cancel_flow(ctx, flow_id=flow_id, revision=body.revision, reason=body.reason)
    await engine.commit(ctx)
    db.refresh(flow)
    return flow_summary(db, engine, flow, engine.clock())


@router.post("/flows/{flow_id}/steps/{step}/retry")
async def retry_step(
    flow_id: str,
    step: str,
    body: RevisionBody,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    ctx = engine.context(db, principal.actor)
    flow = engine.retry_step(ctx, flow_id=flow_id, step_name=step, revision=body.revision)
    await engine.commit(ctx)
    db.refresh(flow)
    return flow_summary(db, engine, flow, engine.clock())
