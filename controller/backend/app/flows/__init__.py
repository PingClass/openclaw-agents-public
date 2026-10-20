"""流程定義註冊表。新增流程：在 flows/<type>.py 定義 DEFINITION，並加進 REGISTRY。"""

from __future__ import annotations

from app.flows import demo, onboarding, refund, visit
from app.flows.base import FlowDefinition

REGISTRY: dict[str, FlowDefinition] = {
    demo.DEFINITION.type: demo.DEFINITION,
    visit.DEFINITION.type: visit.DEFINITION,
    refund.DEFINITION.type: refund.DEFINITION,
    onboarding.DEFINITION.type: onboarding.DEFINITION,
}


def get_definition(flow_type: str) -> FlowDefinition:
    try:
        return REGISTRY[flow_type]
    except KeyError as exc:
        raise KeyError(f"未知的流程類型：{flow_type}") from exc


def register(definition: FlowDefinition) -> None:
    REGISTRY[definition.type] = definition
