"""人工確認 API：一次性確認頁、做決定、重送通知。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import Principal, get_db, get_engine, require_admin
from app.engine import FlowEngine
from app.schemas import Decision, RevisionBody
from app.views import approval_public_view, approval_view

router = APIRouter(tags=["approvals"])


@router.get("/approvals/{approval_id}")
async def get_approval(
    approval_id: str,
    token: str = Query(default="", max_length=200),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    """確認頁只認一次性 token，不需要登入；用過、過期或重送後舊連結都會失效。"""
    approval = engine.view_approval(db, approval_id, token)
    return approval_public_view(db, approval, engine.clock())


@router.post("/approvals/{approval_id}/decision")
async def decide(
    approval_id: str,
    body: Decision,
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    ctx = engine.context(db, actor="human")
    approval = engine.decide_approval(
        ctx, approval_id=approval_id, token=body.token, decision=body.decision, comment=body.comment,
        data=body.data,
    )
    await engine.commit(ctx)
    return {"id": approval.id, "status": approval.status, "decision": approval.decision}


@router.post("/approvals/{approval_id}/resend")
async def resend(
    approval_id: str,
    body: RevisionBody,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    engine: FlowEngine = Depends(get_engine),
):
    ctx = engine.context(db, principal.actor)
    approval = engine.resend_approval(ctx, approval_id=approval_id, revision=body.revision)
    await engine.commit(ctx)
    return approval_view(approval, engine.clock())
