"""審查補充：agent token 的 scope、收件匣寫入權限、dedupe_key 併發。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from app.flows import REGISTRY, demo, register
from app.models import Flow
from tests.conftest import APPROVER


def _demo_body(topic: str = "scope") -> dict:
    return {"type": "demo", "input": {"topic": topic, "approver_hrm_employee_id": APPROVER}}


def test_only_listed_assistant_agents_can_start_flow(env):
    # demo 只允許 cs-agent 啟動
    resp = env.agent("admin-agent").post("/api/flows", json=_demo_body())
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_allowed_to_start"
    # 扮客戶的 milim、員工 agent 都不能啟動流程
    for agent_id in ("milim", "main"):
        resp = env.agent(agent_id).post("/api/flows", json=_demo_body())
        assert resp.status_code == 403, agent_id
    # 未知流程類型仍回 422
    resp = env.agent("cs-agent").post("/api/flows", json={"type": "nope", "input": {}})
    assert resp.status_code == 422
    # admin 從前端啟動不受 starters 限制
    resp = env.user("admin").post("/api/flows", json=_demo_body())
    assert resp.status_code == 201
    # viewer 不能啟動
    assert env.user("viewer").post("/api/flows", json=_demo_body()).status_code == 403


def test_starters_none_allows_any_assistant(env):
    original = REGISTRY["demo"]
    register(replace(original, starters=None))
    try:
        assert env.agent("it-agent").post("/api/flows", json=_demo_body()).status_code == 201
        assert env.agent("milim").post("/api/flows", json=_demo_body()).status_code == 403
    finally:
        register(original)


def test_agent_can_only_view_involved_flows(env):
    flow_id = env.start_demo()["id"]
    # 啟動者、有步驟指派的 agent 可以看
    for agent_id in ("cs-agent", "admin-agent", "finance-agent"):
        assert env.agent(agent_id).get(f"/api/flows/{flow_id}").status_code == 200, agent_id
    # 沒參與的 agent 不行
    for agent_id in ("it-agent", "milim", "main"):
        resp = env.agent(agent_id).get(f"/api/flows/{flow_id}")
        assert resp.status_code == 403, agent_id
        assert resp.json()["detail"]["code"] == "not_involved"
    # 前端使用者都可以看
    assert env.user("viewer").get(f"/api/flows/{flow_id}").status_code == 200


def test_viewer_cannot_mark_others_notifications_read(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={"draft": "d"})
    viewer = env.user("viewer")
    items = viewer.get("/api/inbox", params={"recipient": APPROVER}).json()["items"]
    assert items, "應有確認通知"
    resp = viewer.post(f"/api/inbox/{items[0]['id']}/read")
    assert resp.status_code == 403
    assert viewer.post("/api/inbox/read-all", params={"recipient": APPROVER}).status_code == 403
    # 仍是未讀
    assert viewer.get("/api/inbox", params={"recipient": APPROVER}).json()["unread_count"] == 1
    # 收件人本人（扮演的員工 agent）可以標
    assert env.agent("main").post(f"/api/inbox/{items[0]['id']}/read").status_code == 200


def test_concurrent_dedupe_key_returns_existing_flow(env):
    """預先檢查之後、寫入之前，另一個請求先建了同一個 dedupe_key 的流程。"""
    engine = env.app.state.engine
    original = REGISTRY["demo"]

    def racing_validate(data):
        with env.session_factory() as other:
            other_ctx = engine.context(other, "agent:cs-agent")
            register(original)  # 另一個請求用原本的定義
            engine.start_flow(other_ctx, "demo", data, dedupe_key="crm:fu:1")
            other.commit()
        return original.validate_input(data)

    register(replace(original, validate_input=racing_validate))
    try:
        with env.session_factory() as db:
            ctx = engine.context(db, "agent:cs-agent")
            flow, created = engine.start_flow(
                ctx, "demo", {"topic": "t", "approver_hrm_employee_id": APPROVER},
                dedupe_key="crm:fu:1",
            )
            assert created is False
            winner_id = flow.id
            asyncio.run(engine.commit(ctx))
    finally:
        register(original)
    with env.session_factory() as db:
        flows = db.query(Flow).filter(Flow.dedupe_key == "crm:fu:1").all()
        assert [f.id for f in flows] == [winner_id]


def test_demo_definition_declares_starters():
    assert demo.DEFINITION.starters == ("cs-agent",)
