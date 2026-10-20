"""流程引擎（計畫 3.6）：推進、派工、建立人工確認＋通知、處理回報、逾時掃描。

所有操作都在一個 Ctx（一個 DB 交易）內進行；提交後才推送 SSE 事件、送出派工。
Flow.revision 由 SQLAlchemy 的 version_id_col 做 compare-and-swap，
任何兩個同時更新同一條流程的寫入，後到的會被拒絕（StaleDataError → 409）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.config import Settings
from app.dispatch import DispatchResult, Dispatcher, MessageJob, StepJob
from app.events import EventBus
from app.flows import get_definition
from app.flows.base import (
    END,
    FAIL,
    NEXT,
    AgentStep,
    ApprovalStep,
    ControllerStep,
    FlowDefinition,
    StepStop,
    WaitStep,
    resolve_deadline,
)
from app.channels import record_outbound
from app.gateway import GatewayMonitor
from app.models import (
    APPROVAL_CANCELLED,
    APPROVAL_DECIDED,
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    FLOW_ACTIVE,
    FLOW_CANCELLED,
    FLOW_EXPIRED,
    FLOW_FAILED,
    FLOW_RUNNING,
    FLOW_SUCCEEDED,
    FLOW_WAITING_APPROVAL,
    HUMAN,
    ROLE_ADMIN,
    STEP_DISPATCHED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    STEP_WAITING_APPROVAL,
    Agent,
    Approval,
    AuditLog,
    ChannelMessage,
    Flow,
    Identity,
    Notification,
    Step,
    User,
)
from app.notify import KIND_ALERT, KIND_APPROVAL_EXPIRED, KIND_APPROVAL_REQUEST, KIND_INFO, Message, Notifier
from app.sanitize import sanitize
from app.security import new_token, sha256_hex
from app.stuck import compute_stuck

logger = logging.getLogger(__name__)


class EngineError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFound(EngineError):
    status_code = 404
    code = "not_found"


class Conflict(EngineError):
    status_code = 409
    code = "conflict"


class Forbidden(EngineError):
    status_code = 403
    code = "forbidden"


class Gone(EngineError):
    status_code = 410
    code = "gone"


class Invalid(EngineError):
    status_code = 422
    code = "invalid"


@dataclass
class Ctx:
    db: Session
    now: datetime
    actor: str
    events: list[tuple[str, dict[str, Any], Flow | None]] = field(default_factory=list)
    jobs: list[StepJob | MessageJob] = field(default_factory=list)
    after_commit: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def emit(self, event_type: str, data: dict[str, Any], flow: Flow | None = None) -> None:
        self.events.append((event_type, data, flow))

    def audit(self, action: str, detail: dict[str, Any] | None = None, *, flow_id: str | None = None,
              actor: str | None = None) -> None:
        self.db.add(
            AuditLog(
                flow_id=flow_id,
                actor=actor or self.actor,
                action=action,
                detail=sanitize(detail or {}),
                created_at=self.now,
            )
        )


CONTROLLER = "controller"


class StepRuntime:
    """給 ControllerStep 用的執行環境：同一個交易的 db、現在時間、發通知。"""

    def __init__(self, engine: FlowEngine, ctx: Ctx, flow: Flow) -> None:
        self.engine = engine
        self.ctx = ctx
        self.flow = flow

    @property
    def db(self) -> Session:
        return self.ctx.db

    @property
    def now(self) -> datetime:
        return self.ctx.now

    def set_state(self, key: str, value: Any) -> None:
        """寫入流程狀態的頂層欄位（例如 parties、claim）。JSON 欄位要整個重新指定才會存進 DB。"""
        state = dict(self.flow.state or {})
        state[key] = value
        self.flow.state = state

    def send_customer_message(self, *, external_customer_id: str | None, body: str,
                              agent_id: str = "cs-agent") -> ChannelMessage:
        """送一則對外訊息給客戶。LINE 接上前只記在 controller（模擬通道）：
        該客戶有模擬對話（/sim/messages 送過訊息）時，記在同一個對話裡，扮客戶的 agent 讀得到。"""
        message = record_outbound(self.db, now=self.now, flow_id=self.flow.id, agent_id=agent_id,
                                  external_customer_id=external_customer_id, body=body,
                                  created_by=CONTROLLER)
        self.ctx.audit("channel.message_sent", {"message_id": message.id, "channel": message.channel,
                                                "conversation_id": message.conversation_id},
                       flow_id=self.flow.id)
        self.ctx.emit("channel.message", {"message_id": message.id, "direction": message.direction}, self.flow)
        return message

    def notify(self, recipient: Identity | str, title: str, body: str, *, kind: str = KIND_INFO) -> Identity:
        identity = recipient
        if isinstance(recipient, str):
            identity = self.db.scalar(select(Identity).where(Identity.hrm_employee_id == recipient))
            if identity is None:
                raise StepStop(f"找不到通知對象（hrm_employee_id={recipient}）")
        self.engine.notifier.send(
            self.ctx,
            Message(recipient=identity, kind=kind, title=title, body=body, flow_id=self.flow.id),
        )
        return identity


class FlowEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        dispatcher: Dispatcher,
        notifier: Notifier,
        bus: EventBus,
        clock: Callable[[], datetime],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.dispatcher = dispatcher
        self.notifier = notifier
        self.bus = bus
        self.clock = clock
        self._tasks: set[asyncio.Task] = set()
        # OpenClaw gateway 健康狀態（計畫 3.7）；背景檢查由 main.py 的 lifespan 啟動
        self.gateway = GatewayMonitor(url=settings.gateway_url, timeout=settings.gateway_timeout,
                                      clock=clock)
        # 背景掃描記住每條流程上一次的卡住代碼，變化時推送 SSE（不寫 DB，避免改動 revision）
        self._stuck_codes: dict[str, str | None] = {}

    # ------------------------------------------------------------------ 交易

    def context(self, db: Session, actor: str) -> Ctx:
        return Ctx(db=db, now=self.clock(), actor=actor)

    async def commit(self, ctx: Ctx) -> None:
        ctx.db.commit()
        for event_type, data, flow in ctx.events:
            payload = dict(data)
            if flow is not None:
                payload.setdefault("flow_id", flow.id)
                payload["flow_status"] = flow.status
                payload["revision"] = flow.revision
            self.bus.publish(event_type, payload)
        ctx.events.clear()
        jobs, ctx.jobs = ctx.jobs, []
        callbacks, ctx.after_commit = ctx.after_commit, []
        for job in jobs:
            await self.submit(job)
        for callback in callbacks:
            await callback()

    async def submit(self, job: StepJob | MessageJob) -> None:
        if self.dispatcher.inline:
            await self.run_job(job)
            return
        task = asyncio.get_running_loop().create_task(self.run_job(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run_job(self, job: StepJob | MessageJob) -> None:
        try:
            result = await self.dispatcher.dispatch(job)
        except Exception as exc:  # noqa: BLE001  派工不能讓整個服務掛掉
            logger.exception("派工失敗")
            result = DispatchResult(ok=False, error=f"派工例外：{exc}")
        with self.session_factory() as db:
            ctx = self.context(db, actor="controller")
            try:
                self.on_dispatch_finished(ctx, job, result)
                await self.commit(ctx)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("處理派工結果失敗")

    async def drain(self) -> None:
        """等所有背景派工結束（關機與測試用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ 查詢輔助

    def get_flow(self, db: Session, flow_id: str) -> Flow:
        flow = db.get(Flow, flow_id)
        if flow is None:
            raise NotFound("找不到流程", code="flow_not_found")
        return flow

    @staticmethod
    def step_row(flow: Flow, name: str) -> Step:
        for step in flow.steps:
            if step.name == name:
                return step
        raise NotFound(f"找不到步驟 {name}", code="step_not_found")

    @staticmethod
    def current_step(flow: Flow) -> Step | None:
        if flow.current_step is None:
            return None
        for step in flow.steps:
            if step.name == flow.current_step:
                return step
        return None

    @staticmethod
    def check_revision(flow: Flow, expected: int | None) -> None:
        if expected is not None and expected != flow.revision:
            raise Conflict(
                f"流程已被更新（目前 revision {flow.revision}，請求帶的是 {expected}），請重新整理",
                code="revision_conflict",
            )

    @staticmethod
    def _require_active(flow: Flow) -> None:
        if flow.status not in FLOW_ACTIVE:
            raise Conflict(f"流程已結束（{flow.status}）", code="flow_finished")

    def _touch(self, ctx: Ctx, flow: Flow) -> None:
        flow.updated_at = ctx.now
        # 即使時間相同也要標成已修改，確保 revision 每次都 +1
        flag_modified(flow, "updated_at")
        ctx.emit("flow.updated", {"current_step": flow.current_step}, flow)

    def _set_state(self, flow: Flow, section: str, key: str, value: Any) -> None:
        # JSON 欄位要整個重新指定，SQLAlchemy 才會偵測到變更
        state = dict(flow.state or {})
        bucket = dict(state.get(section) or {})
        bucket[key] = value
        state[section] = bucket
        flow.state = state

    def _step_event(self, ctx: Ctx, flow: Flow, step: Step) -> None:
        ctx.emit(
            "step.updated",
            {"step": step.name, "step_status": step.status, "assignee": step.assignee},
            flow,
        )

    # ------------------------------------------------------------------ 啟動

    @staticmethod
    def authorize_start(flow_type: str, agent: Agent) -> None:
        """agent token 的 scope：只有助理 agent 能啟動流程，且必須在流程定義的 starters 內。

        員工 agent（扮演人）與扮客戶的 milim 不能啟動流程，避免模擬入口被拿來繞過 cs-agent。
        """
        try:
            definition = get_definition(flow_type)
        except KeyError as exc:
            raise Invalid(str(exc.args[0]), code="unknown_flow_type") from exc
        if agent.role != "assistant":
            raise Forbidden("只有助理 agent 可以啟動流程", code="not_allowed_to_start")
        if definition.starters is not None and agent.id not in definition.starters:
            raise Forbidden(f"{agent.id} 不能啟動 {flow_type} 流程", code="not_allowed_to_start")

    @staticmethod
    def wants_demo(data: dict[str, Any] | None) -> bool:
        """啟動資料有沒有要求示範模式（demo 不是 false／null 就算，交給 validate_input 檢查型別）。"""
        return isinstance(data, dict) and data.get("demo") not in (None, False)

    def authorize_demo(self, flow_type: str, *, agent: Agent | None, is_admin: bool) -> None:
        """課堂示範模式（時限縮短）：只有 admin，或流程定義 demo_starters 列出的助理 agent 可以啟動。"""
        try:
            definition = get_definition(flow_type)
        except KeyError as exc:
            raise Invalid(str(exc.args[0]), code="unknown_flow_type") from exc
        if not self.settings.demo_enabled:
            raise Forbidden("這個 controller 沒有開放示範模式（FLOW_CONTROLLER_DEMO_ENABLED=0）",
                            code="demo_disabled")
        if not definition.supports_demo:
            raise Invalid(f"{flow_type} 流程不支援示範模式", code="demo_not_supported")
        if is_admin:
            return
        if agent is None or agent.id not in definition.demo_starters:
            who = agent.id if agent is not None else "這個帳號"
            raise Forbidden(f"{who} 不能啟動示範模式的 {flow_type} 流程", code="demo_not_allowed")

    @staticmethod
    def agent_can_view(flow: Flow, agent_id: str) -> bool:
        """agent 只能查詢自己啟動、有步驟指派給自己、或被流程列為參與者（例如業務本人）的流程。"""
        participants = (flow.state or {}).get("participants") or []
        return (
            flow.created_by == f"agent:{agent_id}"
            or any(step.assignee == agent_id for step in flow.steps)
            or agent_id in participants
        )

    def start_flow(
        self,
        ctx: Ctx,
        flow_type: str,
        data: dict[str, Any],
        *,
        title: str | None = None,
        dedupe_key: str | None = None,
    ) -> tuple[Flow, bool]:
        try:
            definition = get_definition(flow_type)
        except KeyError as exc:
            raise Invalid(str(exc.args[0]), code="unknown_flow_type") from exc
        try:
            clean = definition.validate_input(dict(data or {}))
        except ValueError as exc:
            raise Invalid(str(exc), code="invalid_input") from exc
        if definition.dedupe_key is not None:
            # 流程定義決定去重鍵（例如 CRM 待辦跟進 id），呼叫端給的值不採用
            dedupe_key = definition.dedupe_key(clean)
        if dedupe_key:
            existing = ctx.db.scalar(
                select(Flow).where(Flow.type == flow_type, Flow.dedupe_key == dedupe_key)
            )
            if existing is not None:
                return existing, False
        if clean.get("demo") and definition.demo_check is not None:
            # 示範模式的時限只適用於「馬上要開始」的拜訪（重送已存在的流程照常回傳；新流程才檢查）
            try:
                definition.demo_check(clean, ctx.now)
            except ValueError as exc:
                raise Invalid(str(exc), code="demo_input_rejected") from exc

        flow = Flow(
            type=flow_type,
            title=(title or definition.make_title(clean))[:200],
            status=FLOW_RUNNING,
            state={"input": clean, "steps": {}, "decisions": {}},
            dedupe_key=dedupe_key,
            created_by=ctx.actor,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        for position, step_def in enumerate(definition.steps):
            flow.steps.append(
                Step(
                    name=step_def.name,
                    title=step_def.title,
                    position=position,
                    kind=step_def.kind,
                    assignee=_initial_assignee(step_def),
                    status=STEP_PENDING,
                    max_attempts=getattr(step_def, "max_attempts", 1),
                )
            )
        ctx.db.add(flow)
        try:
            ctx.db.flush()
        except IntegrityError:
            # 兩個請求同時用同一個 dedupe_key 啟動：後到的回傳先建立的那條流程
            ctx.db.rollback()
            existing = (
                ctx.db.scalar(select(Flow).where(Flow.type == flow_type, Flow.dedupe_key == dedupe_key))
                if dedupe_key
                else None
            )
            if existing is None:
                raise
            return existing, False
        ctx.audit("flow.started", {"type": flow_type, "dedupe_key": dedupe_key}, flow_id=flow.id)
        if clean.get("demo"):
            ctx.audit("flow.demo_started", {"type": flow_type, "started_by": ctx.actor,
                                            "note": "示範模式：時限縮短（見流程定義）"}, flow_id=flow.id)
        ctx.emit("flow.created", {"type": flow_type}, flow)
        self._enter_step(ctx, flow, definition, definition.steps[0].name)
        return flow, True

    # ------------------------------------------------------------------ 推進

    def _enter_step(self, ctx: Ctx, flow: Flow, definition: FlowDefinition, name: str) -> None:
        step_def = definition.step(name)
        step = self.step_row(flow, name)
        flow.current_step = name
        step.error = None
        step.error_kind = None
        step.finished_at = None
        timeout = getattr(step_def, "timeout", None)
        step.deadline_at = resolve_deadline(timeout, flow.state, ctx.now) if timeout else None

        if isinstance(step_def, AgentStep):
            flow.status = FLOW_RUNNING
            step.attempts += 1
            step.status = STEP_DISPATCHED
            step.dispatched_at = ctx.now
            step.started_at = None
            instruction = step_def.instruction(flow.state)
            inputs = step_def.inputs(flow.state) if step_def.inputs else None
            step.input = {"instruction": instruction, "skill": step_def.skill, "input": inputs}
            ctx.jobs.append(
                StepJob(
                    flow_id=flow.id,
                    flow_type=flow.type,
                    step=name,
                    attempt=step.attempts,
                    agent_id=step_def.agent,
                    instruction=instruction,
                    skill=step_def.skill,
                    input=inputs,
                    write_keys=step_def.write_keys,
                    outputs=tuple(
                        (field.name, field.type, field.description, field.required)
                        for field in step_def.outputs
                    ),
                )
            )
            ctx.audit("step.dispatched", {"step": name, "agent": step_def.agent,
                                          "attempt": step.attempts}, flow_id=flow.id)
        elif isinstance(step_def, WaitStep):
            flow.status = FLOW_RUNNING
            step.attempts += 1
            step.status = STEP_RUNNING
            step.dispatched_at = ctx.now
            step.started_at = ctx.now
            if step_def.reporter is not None:
                step.assignee = step_def.reporter(flow.state) or step_def.agent
            ctx.audit("step.waiting", {"step": name, "agent": step.assignee}, flow_id=flow.id)
        elif isinstance(step_def, ControllerStep):
            self._touch(ctx, flow)
            self._run_controller_step(ctx, flow, definition, step, step_def)
            return
        else:
            self._open_approval(ctx, flow, step, step_def)
        self._step_event(ctx, flow, step)
        self._touch(ctx, flow)

    def _run_controller_step(self, ctx: Ctx, flow: Flow, definition: FlowDefinition, step: Step,
                             step_def: ControllerStep) -> None:
        flow.status = FLOW_RUNNING
        step.attempts += 1
        step.status = STEP_RUNNING
        step.dispatched_at = ctx.now
        step.started_at = ctx.now
        step.input = None
        try:
            output = step_def.run(StepRuntime(self, ctx, flow), flow.state) or {}
        except StepStop as exc:
            # 設定問題（例如找不到確認對象）：停下來通知 admin，不要猜
            self._notify_admins(ctx, flow, f"流程停下：{flow.title}",
                                f"「{step.title}」無法完成：{exc.message}。修正後請在流程詳情按「重試這一步」。")
            self._fail_step(ctx, flow, definition, step, error=exc.message, error_kind="system")
            return
        clean = sanitize(output)
        step.status = STEP_SUCCEEDED
        step.output = clean
        step.summary = step_def.summary(clean) if step_def.summary else None
        step.finished_at = ctx.now
        self._set_state(flow, "steps", step.name, clean)
        ctx.audit("step.completed", {"step": step.name, "summary": step.summary}, flow_id=flow.id)
        self._step_event(ctx, flow, step)
        target = step_def.route(flow.state, clean) if step_def.route else NEXT
        self._advance(ctx, flow, definition, step.name, target)

    def admin_recipients(self, db: Session) -> list[Identity]:
        """「通知 admin」的對象：連到 Identity 的 admin 帳號，以及 role 為 admin 的 Identity。"""
        identities: dict[int, Identity] = {}
        for user in db.scalars(select(User).where(User.role == ROLE_ADMIN, User.disabled.is_(False),
                                                  User.identity_id.is_not(None))):
            identity = db.get(Identity, user.identity_id)
            if identity is not None:
                identities[identity.id] = identity
        for identity in db.scalars(select(Identity).where(Identity.role == "admin")):
            identities[identity.id] = identity
        return list(identities.values())

    def _notify_admins(self, ctx: Ctx, flow: Flow, title: str, body: str) -> None:
        recipients = self.admin_recipients(ctx.db)
        for identity in recipients:
            self.notifier.send(ctx, Message(recipient=identity, kind=KIND_ALERT, title=title, body=body,
                                            flow_id=flow.id))
        # 沒有任何 admin 收件匣時，至少在稽核與總覽的卡住清單看得到
        ctx.audit("admin.notified", {"title": title, "body": body, "recipients": len(recipients)},
                  flow_id=flow.id)

    def _open_approval(self, ctx: Ctx, flow: Flow, step: Step, step_def: ApprovalStep) -> None:
        step.attempts += 1
        step.started_at = ctx.now
        step.dispatched_at = ctx.now
        approver_key = step_def.approver(flow.state)
        identity = ctx.db.scalar(select(Identity).where(Identity.hrm_employee_id == approver_key))
        if identity is None:
            # 設定問題：找不到確認者。標成系統錯誤，讓前端顯示卡住
            flow.status = FLOW_RUNNING
            step.status = STEP_FAILED
            step.error = f"找不到確認者（hrm_employee_id={approver_key}），請先匯入 Identity"
            step.error_kind = "system"
            step.finished_at = ctx.now
            step.failures += 1
            ctx.audit("approval.no_approver", {"step": step.name, "approver": approver_key},
                      flow_id=flow.id)
            self._notify_admins(ctx, flow, f"流程停下：{flow.title}",
                                f"「{step.title}」{step.error}。修正後請在流程詳情按「重試這一步」。")
            return

        flow.status = FLOW_WAITING_APPROVAL
        step.status = STEP_WAITING_APPROVAL
        step.input = {"approver_id": identity.id}
        approval = Approval(
            flow_id=flow.id,
            step=step.name,
            approver_id=identity.id,
            question=step_def.question(flow.state),
            options=[{"value": opt.value, "label": opt.label} for opt in step_def.options],
            context=sanitize(step_def.context(flow.state)),
            fields=sanitize(step_def.fields(flow.state)) if step_def.fields else [],
            status=APPROVAL_PENDING,
            expires_at=ctx.now,  # 由 _issue_token 設定
            created_at=ctx.now,
        )
        ctx.db.add(approval)
        ctx.db.flush()
        token = self._issue_token(ctx, approval, step)
        self._notify_approval(ctx, flow, approval, identity, token)
        ctx.audit("approval.created", {"approval_id": approval.id, "step": step.name,
                                       "approver": identity.display_name}, flow_id=flow.id)
        ctx.emit("approval.created", {"approval_id": approval.id, "step": step.name}, flow)

    def _issue_token(self, ctx: Ctx, approval: Approval, step: Step) -> str:
        token = new_token("apt")
        approval.token_hash = sha256_hex(token)
        approval.token_used_at = None
        expires = ctx.now + timedelta(hours=self.settings.approval_ttl_hours)
        # 截止時間已過（逾時處理為 stuck 的確認，重送時）就不再縮短，否則新連結一發出就過期
        if step.deadline_at is not None and ctx.now < step.deadline_at < expires:
            expires = step.deadline_at
        approval.expires_at = expires
        approval.sent_count += 1
        return token

    def approval_link(self, approval: Approval, token: str) -> str:
        return f"{self.settings.public_url}/approvals/{approval.id}?token={token}"

    def _notify_approval(self, ctx: Ctx, flow: Flow, approval: Approval, identity: Identity,
                         token: str) -> None:
        self.notifier.send(
            ctx,
            Message(
                recipient=identity,
                kind=KIND_APPROVAL_REQUEST,
                title=f"待確認：{flow.title}",
                body=approval.question,
                link=self.approval_link(approval, token),
                flow_id=flow.id,
                approval_id=approval.id,
            ),
        )

    def _advance(self, ctx: Ctx, flow: Flow, definition: FlowDefinition, from_step: str,
                 target: str) -> None:
        if target == NEXT:
            following = definition.next_after(from_step)
            target = following if following is not None else END
        if target == END:
            self._finish(ctx, flow, FLOW_SUCCEEDED, "流程完成")
        elif target == FAIL:
            self._finish(ctx, flow, FLOW_FAILED, f"在「{from_step}」結束")
        else:
            self._enter_step(ctx, flow, definition, target)

    def _finish(self, ctx: Ctx, flow: Flow, status: str, reason: str) -> None:
        flow.status = status
        flow.finished_at = ctx.now
        for step in flow.steps:
            if step.status in (STEP_PENDING, STEP_DISPATCHED, STEP_RUNNING, STEP_WAITING_APPROVAL):
                step.status = STEP_SKIPPED
                step.finished_at = step.finished_at or ctx.now
                self._step_event(ctx, flow, step)
        for approval in ctx.db.scalars(
            select(Approval).where(Approval.flow_id == flow.id, Approval.status == APPROVAL_PENDING)
        ):
            approval.status = APPROVAL_CANCELLED
            approval.token_hash = None
        ctx.audit(f"flow.{status}", {"reason": reason}, flow_id=flow.id)
        self._stuck_codes.pop(flow.id, None)
        self._touch(ctx, flow)

    def _fail_step(self, ctx: Ctx, flow: Flow, definition: FlowDefinition, step: Step,
                   *, error: str, error_kind: str, summary: str | None = None,
                   output: dict[str, Any] | None = None) -> None:
        """output：這次失敗回報的結構化輸出（例如 409 的 conflict 資訊），給 on_failure 判斷。"""
        step_def = definition.step(step.name)
        step.status = STEP_FAILED
        step.error = error
        step.error_kind = error_kind
        step.summary = summary or step.summary
        step.finished_at = ctx.now
        step.failures += 1
        agent = ctx.db.get(Agent, step.assignee)
        if agent is not None:
            agent.last_error_at = ctx.now
            agent.last_error = error[:500]
        self._step_event(ctx, flow, step)
        failure_output = output or {}
        previous = ((flow.state or {}).get("failures") or {}).get(step.name) or {}
        # 每次失敗都留下來（最多 10 筆），例如 ① 要排除所有 409 衝突過的車，不只最後一台
        history = [*(previous.get("history") or []),
                   {"error": error, "error_kind": error_kind, "output": failure_output}][-10:]
        self._set_state(flow, "failures", step.name, {
            "count": step.failures,
            "error": error,
            "error_kind": error_kind,
            "output": failure_output,
            "history": history,
        })
        max_attempts = getattr(step_def, "max_attempts", 1)
        if error_kind != "needs_human" and step.failures < max_attempts:
            on_failure = getattr(step_def, "on_failure", None)
            if callable(on_failure):
                on_failure = on_failure(flow.state, {"error": error, "error_kind": error_kind,
                                                     "output": failure_output})
            target = on_failure or step.name
            ctx.audit("step.auto_retry", {"step": step.name, "error": error, "goto": target,
                                          "failures": step.failures}, flow_id=flow.id)
            self._enter_step(ctx, flow, definition, target)
        else:
            ctx.audit("step.failed", {"step": step.name, "error": error, "error_kind": error_kind,
                                      "failures": step.failures}, flow_id=flow.id)
            self._touch(ctx, flow)

    # ------------------------------------------------------------------ agent 回報

    def report_result(
        self,
        ctx: Ctx,
        *,
        flow_id: str,
        step_name: str,
        agent_id: str,
        status: str,
        summary: str | None,
        output: dict[str, Any] | None,
        error: str | None,
        error_kind: str | None,
        attempt: int | None,
        revision: int | None,
    ) -> Flow:
        flow = self.get_flow(ctx.db, flow_id)
        self.check_revision(flow, revision)
        self._require_active(flow)
        step = self.step_row(flow, step_name)
        if step.assignee != agent_id:
            raise Forbidden("這個步驟不是指派給你的", code="not_assignee")
        if flow.current_step != step_name:
            raise Conflict("這個步驟不是目前的步驟", code="step_not_current")
        if step.status not in (STEP_DISPATCHED, STEP_RUNNING):
            raise Conflict(f"步驟目前狀態為 {step.status}，不接受回報", code="step_not_running")
        if attempt is not None and attempt != step.attempts:
            raise Conflict(f"回報的 attempt {attempt} 已過期（目前 {step.attempts}）",
                           code="stale_attempt")

        definition = get_definition(flow.type)
        step_def = definition.step(step_name)
        if isinstance(step_def, WaitStep) and step_def.event_type:
            raise Conflict(f"這一步要用 POST /flows/{{id}}/events 回報（type={step_def.event_type}）",
                           code="use_events")
        clean_output = sanitize(output or {})
        if status == "ok" and isinstance(step_def, AgentStep):
            missing = [f.name for f in step_def.outputs if f.required and clean_output.get(f.name) is None]
            if missing:
                raise Invalid(f"output 缺少欄位：{', '.join(missing)}", code="missing_output_fields")
            if step_def.validate_output is not None:
                try:
                    step_def.validate_output(flow.state, clean_output)
                except ValueError as exc:
                    raise Invalid(str(exc), code="invalid_output") from exc
            if step_def.transform_output is not None:
                clean_output = sanitize(step_def.transform_output(flow.state, dict(clean_output)))
        agent = ctx.db.get(Agent, agent_id)
        if agent is not None:
            agent.last_seen_at = ctx.now
        step.started_at = step.started_at or step.dispatched_at or ctx.now
        ctx.audit("step.reported", {"step": step_name, "status": status, "summary": summary,
                                    "attempt": step.attempts}, flow_id=flow.id, actor=f"agent:{agent_id}")

        if status == "ok":
            step.status = STEP_SUCCEEDED
            step.output = clean_output
            step.summary = summary
            step.finished_at = ctx.now
            self._set_state(flow, "steps", step_name, clean_output)
            self._step_event(ctx, flow, step)
            route = getattr(step_def, "route", None)
            target = route(flow.state, clean_output) if route else NEXT
            self._advance(ctx, flow, definition, step_name, target)
        else:
            step.output = clean_output or step.output
            kind = "needs_human" if status == "needs_human" else (error_kind or "business")
            self._fail_step(ctx, flow, definition, step, error=error or summary or "agent 回報失敗",
                            error_kind=kind, summary=summary, output=clean_output)
        return flow

    # ------------------------------------------------------------------ 外部事件

    def submit_event(self, ctx: Ctx, *, flow_id: str, agent_id: str, event: dict[str, Any]) -> Flow:
        """外部事件（例如業務回報取車還車里程）：只有該流程指定的回報者可以送。"""
        flow = self.get_flow(ctx.db, flow_id)
        definition = get_definition(flow.type)
        event_type = str(event.get("type") or "")
        wait_steps = [s for s in definition.steps if isinstance(s, WaitStep) and s.event_type == event_type]
        if not wait_steps:
            raise Invalid(f"{flow.type} 流程不接受事件 {event_type or '（未指定）'}", code="unknown_event")
        step_def = wait_steps[0]
        reporter = step_def.reporter(flow.state) if step_def.reporter else step_def.agent
        if not reporter or reporter != agent_id:
            raise Forbidden("只有這條流程指定的回報者可以送這個事件", code="not_reporter")
        self._require_active(flow)
        step = self.step_row(flow, step_def.name)
        if flow.current_step != step.name or step.status != STEP_RUNNING:
            raise Conflict(f"流程目前不在「{step.title}」，不接受 {event_type}", code="step_not_current")
        try:
            clean = sanitize(step_def.accept_event(flow.state, dict(event)))  # type: ignore[misc]
        except ValueError as exc:
            raise Invalid(str(exc), code="invalid_event") from exc

        agent = ctx.db.get(Agent, agent_id)
        if agent is not None:
            agent.last_seen_at = ctx.now
        step.status = STEP_SUCCEEDED
        step.output = clean
        step.summary = step_def.event_summary(clean) if step_def.event_summary else event_type
        step.finished_at = ctx.now
        self._set_state(flow, "steps", step.name, clean)
        ctx.audit("event.received", {"type": event_type, "step": step.name, "summary": step.summary},
                  flow_id=flow.id, actor=f"agent:{agent_id}")
        self._step_event(ctx, flow, step)
        target = step_def.route(flow.state, clean) if step_def.route else NEXT
        self._advance(ctx, flow, definition, step.name, target)
        return flow

    def on_dispatch_finished(self, ctx: Ctx, job: StepJob | MessageJob, result: DispatchResult) -> None:
        if isinstance(job, MessageJob):
            ctx.audit(
                f"{job.tag}.dispatch_{'ok' if result.ok else 'failed'}",
                {"agent": job.agent_id, "error": result.error},
            )
            return
        flow = ctx.db.get(Flow, job.flow_id)
        if flow is None:
            return
        step = self.step_row(flow, job.step)
        stale = (
            flow.status not in FLOW_ACTIVE
            or flow.current_step != job.step
            or step.attempts != job.attempt
            or step.status not in (STEP_DISPATCHED, STEP_RUNNING)
        )
        if result.ok:
            excerpt = {k: v for k, v in list((result.output or {}).items())[:10]}
            ctx.audit("dispatch.completed", {"step": job.step, "attempt": job.attempt,
                                             "output": excerpt}, flow_id=flow.id)
            if not stale and step.status == STEP_DISPATCHED:
                step.status = STEP_RUNNING
                step.started_at = step.dispatched_at
                self._step_event(ctx, flow, step)
                self._touch(ctx, flow)
            return
        if stale:
            ctx.audit("dispatch.failed_stale", {"step": job.step, "error": result.error},
                      flow_id=flow.id)
            return
        self._fail_step(ctx, flow, get_definition(flow.type), step,
                        error=result.error or "派工失敗", error_kind="system")

    # ------------------------------------------------------------------ 人工確認

    def _approval_by_token(self, db: Session, approval_id: str, token: str, now: datetime) -> Approval:
        approval = db.get(Approval, approval_id)
        if approval is None:
            raise NotFound("找不到確認", code="approval_not_found")
        if not token or approval.token_hash is None or approval.token_hash != sha256_hex(token):
            if approval.status != APPROVAL_PENDING:
                raise Gone("這個確認已經結束", code="approval_closed")
            raise Forbidden("確認連結無效（可能已重送新的連結）", code="invalid_token")
        if approval.token_used_at is not None:
            raise Gone("確認連結已經使用過", code="token_used")
        if approval.status != APPROVAL_PENDING:
            raise Gone("這個確認已經結束", code="approval_closed")
        if now >= approval.expires_at:
            raise Gone("確認連結已過期", code="token_expired")
        return approval

    def view_approval(self, db: Session, approval_id: str, token: str) -> Approval:
        return self._approval_by_token(db, approval_id, token, self.clock())

    def decide_approval(self, ctx: Ctx, *, approval_id: str, token: str, decision: str,
                        comment: str | None, data: dict[str, Any] | None = None) -> Approval:
        approval = self._approval_by_token(ctx.db, approval_id, token, ctx.now)
        if decision not in {opt["value"] for opt in approval.options}:
            raise Invalid(f"不是有效的選項：{decision}", code="invalid_decision")
        flow = self.get_flow(ctx.db, approval.flow_id)
        self._require_active(flow)
        step = self.step_row(flow, approval.step)
        if flow.current_step != step.name or step.status != STEP_WAITING_APPROVAL:
            raise Gone("這個確認已經不在等待中", code="approval_closed")

        definition = get_definition(flow.type)
        step_def = definition.step(step.name)
        assert isinstance(step_def, ApprovalStep)
        approver = approval.approver
        approver_name = (approver.display_name or approver.hrm_employee_id) if approver else None
        extra: dict[str, Any] = {}
        if step_def.accept_decision is not None:
            meta = {"approver": approver_name, "decided_at": ctx.now.isoformat()}
            try:
                extra = sanitize(step_def.accept_decision(flow.state, decision, dict(data or {}), meta) or {})
            except ValueError as exc:
                raise Invalid(str(exc), code="invalid_decision_data") from exc
        actor = f"human:{approver_name}" if approver_name else "human"
        approval.status = APPROVAL_DECIDED
        approval.decision = decision
        approval.comment = (comment or "").strip()[:2000] or None
        approval.decided_at = ctx.now
        approval.token_used_at = ctx.now
        for notification in ctx.db.scalars(
            select(Notification).where(Notification.approval_id == approval.id,
                                       Notification.read_at.is_(None))
        ):
            notification.read_at = ctx.now

        label = next(opt["label"] for opt in approval.options if opt["value"] == decision)
        record = {"decision": decision, "comment": approval.comment, **extra}
        step.status = STEP_SUCCEEDED
        step.output = record
        if step_def.decision_summary is not None:
            step.summary = step_def.decision_summary(label, record)
        else:
            step.summary = f"{label}" + (f"：{approval.comment}" if approval.comment else "")
        step.finished_at = ctx.now
        self._set_state(flow, "decisions", step.name, record)
        ctx.audit("approval.decided", {"approval_id": approval.id, "step": step.name,
                                       "decision": decision, **extra}, flow_id=flow.id, actor=actor)
        ctx.emit("approval.decided", {"approval_id": approval.id, "decision": decision}, flow)
        self._step_event(ctx, flow, step)
        self._advance(ctx, flow, definition, step.name, step_def.routes[decision])
        return approval

    def resend_approval(self, ctx: Ctx, *, approval_id: str, revision: int | None) -> Approval:
        approval = ctx.db.get(Approval, approval_id)
        if approval is None:
            raise NotFound("找不到確認", code="approval_not_found")
        flow = self.get_flow(ctx.db, approval.flow_id)
        self.check_revision(flow, revision)
        self._require_active(flow)
        if approval.status != APPROVAL_PENDING:
            raise Conflict("這個確認已經結束，不能重送", code="approval_closed")
        step = self.step_row(flow, approval.step)
        identity = approval.approver
        if identity is None:
            raise Conflict("這個確認沒有確認者", code="no_approver")
        # 舊連結隨之失效（資料庫只有新 token 的雜湊）
        token = self._issue_token(ctx, approval, step)
        self._notify_approval(ctx, flow, approval, identity, token)
        ctx.audit("approval.resent", {"approval_id": approval.id, "sent_count": approval.sent_count},
                  flow_id=flow.id)
        self._touch(ctx, flow)
        return approval

    # ------------------------------------------------------------------ 人的操作

    def cancel_flow(self, ctx: Ctx, *, flow_id: str, revision: int | None, reason: str | None) -> Flow:
        flow = self.get_flow(ctx.db, flow_id)
        self.check_revision(flow, revision)
        self._require_active(flow)
        self._finish(ctx, flow, FLOW_CANCELLED, reason or "人工取消")
        return flow

    def retry_step(self, ctx: Ctx, *, flow_id: str, step_name: str, revision: int | None) -> Flow:
        flow = self.get_flow(ctx.db, flow_id)
        self.check_revision(flow, revision)
        self._require_active(flow)
        step = self.step_row(flow, step_name)
        if flow.current_step != step_name:
            raise Conflict("只能重試目前的步驟", code="step_not_current")
        if step.kind == "approval" and step.status == STEP_WAITING_APPROVAL:
            raise EngineError("人工確認請改用「重送確認通知」", code="use_resend")
        if step.status not in (STEP_FAILED, STEP_DISPATCHED, STEP_RUNNING):
            raise Conflict(f"步驟狀態為 {step.status}，不需要重試", code="step_not_retryable")
        ctx.audit("step.retry", {"step": step_name, "previous_status": step.status,
                                 "previous_failures": step.failures,
                                 "previous_error": step.error}, flow_id=flow.id)
        step.failures = 0
        # 人工重試沿用同一個 Idempotency-Key（{flow_id}:{step}），系統端不會重複寫入
        self._enter_step(ctx, flow, get_definition(flow.type), step_name)
        return flow

    # ------------------------------------------------------------------ 逾時掃描

    def scan(self, ctx: Ctx) -> dict[str, int]:
        """每分鐘執行：處理人工確認與等待步驟的逾時，偵測卡住狀態的變化。"""
        expired = 0
        flows = ctx.db.scalars(select(Flow).where(Flow.status.in_(FLOW_ACTIVE))).all()
        for flow in flows:
            step = self.current_step(flow)
            if step is None or step.deadline_at is None:
                continue
            if step.status in (STEP_WAITING_APPROVAL, STEP_RUNNING) and step.kind in ("approval", "wait") \
                    and ctx.now >= step.deadline_at:
                if self._handle_timeout(ctx, flow, step):
                    expired += 1
        stuck_changes = 0
        for flow in flows:
            if flow.status not in FLOW_ACTIVE:
                continue
            stuck = self.stuck_for(ctx.db, flow, ctx.now)
            code = stuck.code if stuck else None
            if self._stuck_codes.get(flow.id) != code:
                self._stuck_codes[flow.id] = code
                stuck_changes += 1
                ctx.emit("flow.stuck", {"stuck": stuck.as_dict() if stuck else None}, flow)
        return {"expired": expired, "stuck_changes": stuck_changes}

    def _handle_timeout(self, ctx: Ctx, flow: Flow, step: Step) -> bool:
        definition = get_definition(flow.type)
        step_def = definition.step(step.name)
        action = step_def.on_timeout
        if action == "stuck":
            return False
        if isinstance(step_def, ApprovalStep):
            approval = ctx.db.scalar(
                select(Approval).where(Approval.flow_id == flow.id, Approval.step == step.name,
                                       Approval.status == APPROVAL_PENDING)
            )
            if approval is not None:
                approval.status = APPROVAL_EXPIRED
                approval.token_hash = None
                if approval.approver is not None and step_def.timeout_notice is not None:
                    self.notifier.send(
                        ctx,
                        Message(
                            recipient=approval.approver,
                            kind=KIND_APPROVAL_EXPIRED,
                            title=f"已逾時：{flow.title}",
                            body=step_def.timeout_notice(flow.state),
                            flow_id=flow.id,
                            approval_id=approval.id,
                        ),
                    )
        step.status = STEP_FAILED
        step.error = "超過時限"
        step.error_kind = "timeout"
        step.finished_at = ctx.now
        self._step_event(ctx, flow, step)
        ctx.audit("step.timeout", {"step": step.name, "action": action}, flow_id=flow.id)
        self._finish(ctx, flow, FLOW_EXPIRED if action == "expire" else FLOW_FAILED,
                     f"「{step.title}」超過時限")
        return True

    # ------------------------------------------------------------------ 卡住

    def stuck_for(self, db: Session, flow: Flow, now: datetime):  # noqa: ANN201
        step = self.current_step(flow)
        approver_name = None
        if step is not None and step.kind == "approval":
            approver_name = self.approver_name(db, flow, step)
        return compute_stuck(flow, step, now, tz=self.settings.tz, approver_name=approver_name)

    @staticmethod
    def approver_name(db: Session, flow: Flow, step: Step) -> str | None:
        approval = db.scalar(
            select(Approval)
            .where(Approval.flow_id == flow.id, Approval.step == step.name)
            .order_by(Approval.created_at.desc())
        )
        if approval is None or approval.approver is None:
            return None
        return approval.approver.display_name or approval.approver.hrm_employee_id


def _initial_assignee(step_def) -> str:  # noqa: ANN001
    if isinstance(step_def, ApprovalStep):
        return HUMAN
    if isinstance(step_def, ControllerStep):
        return CONTROLLER
    return step_def.agent
