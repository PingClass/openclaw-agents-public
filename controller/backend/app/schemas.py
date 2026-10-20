"""請求內容的驗證。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FlowCreate(BaseModel):
    type: str = Field(min_length=1, max_length=32)
    input: dict[str, Any] = Field(default_factory=dict)
    title: str | None = Field(default=None, max_length=200)
    # 同一個來源只啟動一條流程（例如 crm:follow-up:123）；重複呼叫回傳既有流程
    dedupe_key: str | None = Field(default=None, max_length=128)


class StepResult(BaseModel):
    status: Literal["ok", "failed", "needs_human"]
    summary: str | None = Field(default=None, max_length=2000)
    output: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)
    # business：業務錯誤（如 409 時段衝突）；system：連線失敗或 5xx
    error_kind: Literal["business", "system"] | None = None
    # 派工指令中的 attempt；帶了就檢查，防止過期的回報
    attempt: int | None = None
    revision: int | None = None


class FlowEvent(BaseModel):
    """外部事件（例如 visit 流程的 trip_report）。其他欄位依事件類型，由流程定義驗證。"""

    model_config = {"extra": "allow"}

    type: str = Field(min_length=1, max_length=64)


class ConfirmModeBody(BaseModel):
    """確認者由誰確認：agent（員工 agent 代理）／human（真人）。"""

    mode: Literal["agent", "human"]


class RevisionBody(BaseModel):
    revision: int


class CancelBody(RevisionBody):
    reason: str | None = Field(default=None, max_length=500)


class Decision(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    decision: str = Field(min_length=1, max_length=32)
    comment: str | None = Field(default=None, max_length=2000)
    # 確認頁的欄位（例如 ② 的退款金額 amount_twd）；由流程定義驗證
    data: dict[str, Any] | None = None


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class Persona(BaseModel):
    external_customer_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)


class SimMessage(BaseModel):
    target_agent: str = Field(default="cs-agent", max_length=64)
    persona: Persona
    message: str = Field(min_length=1, max_length=2000)
