"""「卡住」的判斷（計畫 3.7）。由後端統一計算，前端只負責顯示。

每條流程只回一個最主要的原因，代碼：
- system_error：呼叫系統或派工時連線失敗、5xx（重試用完後）
- retries_exhausted：失敗次數達上限（含 agent 回報 needs_human）
- agent_no_report：步驟已派出，超過時限仍沒有回報
- approval_waiting_too_long：人工確認超過時限的一半仍未決定
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import (
    FLOW_ACTIVE,
    STEP_DISPATCHED,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_WAITING_APPROVAL,
    Flow,
    Step,
)

SYSTEM_ERROR = "system_error"
RETRIES_EXHAUSTED = "retries_exhausted"
AGENT_NO_REPORT = "agent_no_report"
APPROVAL_WAITING_TOO_LONG = "approval_waiting_too_long"
STUCK_CODES = (SYSTEM_ERROR, RETRIES_EXHAUSTED, AGENT_NO_REPORT, APPROVAL_WAITING_TOO_LONG)


@dataclass(frozen=True)
class Stuck:
    code: str
    reason: str
    step: str
    waited_seconds: int

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "reason": self.reason,
            "step": self.step,
            "waited_seconds": self.waited_seconds,
        }


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "不到 1 分鐘"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分鐘"
    hours = minutes // 60
    if hours < 24:
        rest = minutes % 60
        return f"{hours} 小時" if rest == 0 else f"{hours} 小時 {rest} 分鐘"
    return f"{hours // 24} 天"


def format_deadline(deadline: datetime, now: datetime, tz: str) -> str:
    zone = ZoneInfo(tz)
    local = deadline.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return local.strftime("%H:%M")
    return f"{local.month}/{local.day} {local.strftime('%H:%M')}"


def _since(step: Step) -> datetime | None:
    return step.started_at or step.dispatched_at


def compute_stuck(
    flow: Flow,
    step: Step | None,
    now: datetime,
    *,
    tz: str = "Asia/Taipei",
    approver_name: str | None = None,
) -> Stuck | None:
    if flow.status not in FLOW_ACTIVE or step is None:
        return None

    if step.status == STEP_FAILED:
        waited = (now - (step.finished_at or step.dispatched_at or flow.updated_at)).total_seconds()
        if step.error_kind == "system":
            reason = f"{step.title or step.name}：{step.error or '系統錯誤'}"
            return Stuck(SYSTEM_ERROR, reason, step.name, int(waited))
        if step.error_kind == "needs_human":
            reason = f"{step.assignee} 回報需要人處理：{step.summary or step.error or '（未說明）'}"
            return Stuck(RETRIES_EXHAUSTED, reason, step.name, int(waited))
        retried = max(0, step.failures - 1)
        reason = f"{step.error or '步驟失敗'}，已重試 {retried} 次"
        return Stuck(RETRIES_EXHAUSTED, reason, step.name, int(waited))

    if step.status in (STEP_DISPATCHED, STEP_RUNNING) and step.deadline_at is not None:
        if now > step.deadline_at:
            since = step.dispatched_at or step.started_at or flow.updated_at
            waited = (now - since).total_seconds()
            if step.kind == "wait":
                reason = f"等待「{step.title}」已逾時，{step.assignee} 已 {format_duration(waited)}沒有回報"
            else:
                reason = f"{step.assignee} 已 {format_duration(waited)}沒有回報"
            return Stuck(AGENT_NO_REPORT, reason, step.name, int(waited))
        return None

    if step.status == STEP_WAITING_APPROVAL and step.deadline_at is not None:
        since = _since(step) or flow.updated_at
        half = since + (step.deadline_at - since) / 2
        if now >= half:
            waited = (now - since).total_seconds()
            who = approver_name or "確認者"
            reason = (
                f"等{who}{step.title}，已 {format_duration(waited)}，"
                f"{format_deadline(step.deadline_at, now, tz)} 截止"
            )
            return Stuck(APPROVAL_WAITING_TOO_LONG, reason, step.name, int(waited))
    return None
