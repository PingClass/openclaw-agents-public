"""資料模型（計畫 3.2、0.5）。

controller 只存流程狀態與步驟摘要，不存業務資料；Identity 不存電話、地址。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow

# ---- 狀態常數 ----

FLOW_RUNNING = "running"
FLOW_WAITING_APPROVAL = "waiting_approval"
FLOW_SUCCEEDED = "succeeded"
FLOW_FAILED = "failed"
FLOW_EXPIRED = "expired"
FLOW_CANCELLED = "cancelled"
FLOW_STATUSES = (
    FLOW_RUNNING,
    FLOW_WAITING_APPROVAL,
    FLOW_SUCCEEDED,
    FLOW_FAILED,
    FLOW_EXPIRED,
    FLOW_CANCELLED,
)
FLOW_ACTIVE = (FLOW_RUNNING, FLOW_WAITING_APPROVAL)
FLOW_TERMINAL = (FLOW_SUCCEEDED, FLOW_FAILED, FLOW_EXPIRED, FLOW_CANCELLED)

STEP_PENDING = "pending"
STEP_DISPATCHED = "dispatched"
STEP_RUNNING = "running"
STEP_WAITING_APPROVAL = "waiting_approval"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_STATUSES = (
    STEP_PENDING,
    STEP_DISPATCHED,
    STEP_RUNNING,
    STEP_WAITING_APPROVAL,
    STEP_SUCCEEDED,
    STEP_FAILED,
    STEP_SKIPPED,
)

APPROVAL_PENDING = "pending"
APPROVAL_DECIDED = "decided"
APPROVAL_EXPIRED = "expired"
APPROVAL_CANCELLED = "cancelled"

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"

HUMAN = "human"

# Identity.confirm_mode：確認由誰處理（計畫 9.4「agent 代理／真人」切換）
CONFIRM_AGENT = "agent"  # 員工 agent 的 agent-inbox 代為確認（預設）
CONFIRM_HUMAN = "human"  # 真人（例如課堂上的講師）自己確認；agent 看不到確認連結
CONFIRM_MODES = (CONFIRM_AGENT, CONFIRM_HUMAN)


def new_flow_id() -> str:
    return f"fl_{secrets.token_hex(8)}"


def new_approval_id() -> str:
    return f"ap_{secrets.token_hex(8)}"


class Flow(Base):
    __tablename__ = "flows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_flow_id)
    type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), index=True, default=FLOW_RUNNING)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 防止過期寫入：每次更新 +1，由 SQLAlchemy 以 compare-and-swap 檢查
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 同一個來源（例如 CRM 待辦跟進 id）只啟動一條流程
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    steps: Mapped[list[Step]] = relationship(
        back_populates="flow", order_by="Step.position", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("type", "dedupe_key", name="uq_flow_type_dedupe"),)
    __mapper_args__ = {"version_id_col": revision}


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_id: Mapped[str] = mapped_column(ForeignKey("flows.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="agent")  # agent／approval／wait
    # agent id，或 human（人工確認）
    assignee: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default=STEP_PENDING, index=True)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # business（業務錯誤，如 409 時段衝突）／system（連線失敗、5xx、派工失敗）／needs_human
    error_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 派工次數（含重試）
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 失敗次數，用來判斷「重試用完」；人工重試時歸零
    failures: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    flow: Mapped[Flow] = relationship(back_populates="steps")

    __table_args__ = (UniqueConstraint("flow_id", "name", name="uq_step_flow_name"),)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    emoji: Mapped[str] = mapped_column(String(16), default="")
    workspace: Mapped[str] = mapped_column(String(255), default="")
    # assistant（5 隻助理）／employee（員工 agent）／simulator（扮客戶的 milim）
    role: Mapped[str] = mapped_column(String(16), default="assistant")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentToken(Base):
    __tablename__ = "agent_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    # 只存 SHA-256
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # 可以呼叫 /sim/messages（只發給扮客戶的 agent）
    can_simulate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    agent: Mapped[Agent] = relationship()


class Identity(Base):
    """跨系統的人員對照表。不存電話、地址。"""

    __tablename__ = "identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hrm_employee_id: Mapped[str] = mapped_column(String(64), unique=True)
    employee_number: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    line_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adm_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    crm_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 流程用的角色：sales／manager（部門主管）／finance／it／hr／admin
    role: Mapped[str] = mapped_column(String(64), default="")
    # 部門名稱（例如「業務部」）；部門主管＝同部門、role 為 manager 的人
    department: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 扮演這個人的 OpenClaw 員工 agent id（9.4），可用 agent token 讀自己的收件匣
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 確認由誰處理：agent（agent-inbox 代理）／human（真人，agent 拿不到確認連結）
    confirm_mode: Mapped[str] = mapped_column(String(8), default=CONFIRM_AGENT, server_default=CONFIRM_AGENT)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_approval_id)
    flow_id: Mapped[str] = mapped_column(ForeignKey("flows.id", ondelete="CASCADE"), index=True)
    step: Mapped[str] = mapped_column(String(64))
    approver_id: Mapped[int | None] = mapped_column(ForeignKey("identities.id"), nullable=True)
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    # 確認頁只顯示這裡的內容（由流程定義挑選）
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 確認頁要填的欄位（例如退款金額）；沒有就是空 list
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default=APPROVAL_PENDING, index=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # 一次性 token 只存 SHA-256；用過或重送後清空
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    token_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    approver: Mapped[Identity | None] = relationship()


class Notification(Base):
    """前端通知收件匣（0.5）。LINE 接上前，所有確認通知都進這裡。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("identities.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    flow_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    approval_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    recipient: Mapped[Identity] = relationship()


CHANNEL_SIM = "sim"
DIRECTION_IN = "in"
DIRECTION_OUT = "out"


class ChannelMessage(Base):
    """對外通道的訊息紀錄（LINE 接上前的模擬通道，計畫 9.5）。

    - in：/sim/messages 收到的模擬客戶訊息（轉給 cs-agent）
    - out：流程送給客戶的回覆（例如 ② 的 deliver_reply）。如果該客戶有模擬對話，記在同一個 conversation_id。
    """

    __tablename__ = "channel_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(16), default=CHANNEL_SIM)
    direction: Mapped[str] = mapped_column(String(8))
    # 同一位客戶與同一隻 agent 的對話（例如 sim-cs-agent-C-007）；沒有對話時為 null
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), default="")
    external_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    flow_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class User(Base):
    """controller 自己的帳號表（MVP）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_VIEWER)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    identity_id: Mapped[int | None] = mapped_column(ForeignKey("identities.id"), nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship()
