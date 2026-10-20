"""測試用的假流程：agent 步驟 → 人工確認 → agent 步驟。

用來驗證 engine 端對端推進，不對應任何真實業務。流程 ①②③ 照這個模組的寫法新增。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.flows.base import (
    END,
    FAIL,
    NEXT,
    AgentStep,
    ApprovalStep,
    FlowDefinition,
    Option,
    State,
)


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    topic = str(data.get("topic", "")).strip()
    approver = str(data.get("approver_hrm_employee_id", "")).strip()
    if not topic:
        raise ValueError("topic 必填")
    if not approver:
        raise ValueError("approver_hrm_employee_id 必填")
    return {"topic": topic[:100], "approver_hrm_employee_id": approver}


def _draft_instruction(state: State) -> str:
    return (
        f"為主題「{state['input']['topic']}」產生一份草稿。\n"
        "output 請包含 draft（字串）。"
    )


def _finalize_instruction(state: State) -> str:
    draft = state.get("steps", {}).get("draft", {})
    decision = state.get("decisions", {}).get("confirm", {})
    return (
        "依核准後的草稿完成作業。\n"
        f"草稿：{json.dumps(draft, ensure_ascii=False)}\n"
        f"確認意見：{decision.get('comment') or '（無）'}"
    )


def _question(state: State) -> str:
    return f"請確認「{state['input']['topic']}」的草稿"


def _context(state: State) -> dict[str, Any]:
    draft = state.get("steps", {}).get("draft", {})
    return {"topic": state["input"]["topic"], "draft": draft.get("draft", "")}


DEFINITION = FlowDefinition(
    type="demo",
    display_name="示範流程",
    validate_input=_validate,
    make_title=lambda data: f"示範：{data['topic']}",
    starters=("cs-agent",),
    steps=(
        AgentStep(
            name="draft",
            title="產生草稿",
            agent="admin-agent",
            instruction=_draft_instruction,
            timeout=timedelta(minutes=5),
            max_attempts=2,
        ),
        ApprovalStep(
            name="confirm",
            title="確認草稿",
            approver=lambda state: state["input"]["approver_hrm_employee_id"],
            question=_question,
            options=(
                Option("approve", "核准"),
                Option("revise", "退回重擬"),
                Option("reject", "不核准"),
            ),
            routes={"approve": NEXT, "revise": "draft", "reject": FAIL},
            timeout=timedelta(hours=4),
            on_timeout="expire",
            context=_context,
            timeout_notice=lambda state: f"「{state['input']['topic']}」確認已逾時，流程已結束",
        ),
        AgentStep(
            name="finalize",
            title="完成作業",
            agent="finance-agent",
            instruction=_finalize_instruction,
            timeout=timedelta(minutes=5),
            # 失敗時回到 draft 重擬，最多重試 2 次（共 3 次）
            max_attempts=3,
            on_failure="draft",
            route=lambda state, output: END,
        ),
    ),
)
