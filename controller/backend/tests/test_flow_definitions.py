"""流程定義介面：等待外部事件步驟、依狀態算截止時間、定義檢查、事件匯流排、背景掃描。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.events import EventBus
from app.flows import REGISTRY, get_definition, register
from app.flows.base import END, AgentStep, ApprovalStep, FlowDefinition, Option, WaitStep


def _wait_flow(on_timeout: str) -> FlowDefinition:
    return FlowDefinition(
        type=f"waittest_{on_timeout}",
        display_name="等待測試",
        validate_input=lambda data: data,
        make_title=lambda data: "等待測試",
        steps=(
            AgentStep(name="book", title="預約", agent="admin-agent",
                      instruction=lambda state: "預約"),
            # 截止時間依狀態計算（例如「拜訪當天 23:59」）
            WaitStep(name="trip", title="回報還車", agent="admin-agent",
                     timeout=lambda state, now: now + timedelta(hours=2), on_timeout=on_timeout,
                     route=lambda state, output: END),
        ),
    )


@pytest.fixture
def wait_flows():
    defs = [_wait_flow("stuck"), _wait_flow("expire"), _wait_flow("fail")]
    for definition in defs:
        register(definition)
    yield
    for definition in defs:
        REGISTRY.pop(definition.type, None)


def _start(env, flow_type):
    resp = env.agent("cs-agent").post("/api/flows", json={"type": flow_type, "input": {}})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _scan(env):
    engine = env.app.state.engine
    with env.session_factory() as db:
        ctx = engine.context(db, actor="controller")
        result = engine.scan(ctx)
        asyncio.run(engine.commit(ctx))
    return result


def test_wait_step_is_not_dispatched_and_accepts_report(env, wait_flows):
    flow_id = _start(env, "waittest_stuck")
    env.report(flow_id, "book", "admin-agent", output={"booking_id": 1})
    detail = env.flow(flow_id)
    trip = detail["steps"][1]
    assert trip["status"] == "running"
    assert trip["kind"] == "wait"
    assert [j.step for j in env.dispatcher.step_jobs] == ["book"]
    resp = env.report(flow_id, "trip", "admin-agent", output={"km": 42})
    assert resp.json()["status"] == "succeeded"


def test_wait_step_timeout_stuck(env, clock, wait_flows):
    flow_id = _start(env, "waittest_stuck")
    env.report(flow_id, "book", "admin-agent", output={})
    clock.advance(hours=3)
    assert _scan(env)["expired"] == 0
    stuck = env.flow(flow_id)["stuck"]
    assert stuck["code"] == "agent_no_report"
    assert "回報還車" in stuck["reason"]
    # 人工重試：重新開始等待
    detail = env.flow(flow_id)
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/trip/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200
    assert env.flow(flow_id)["stuck"] is None


@pytest.mark.parametrize(("kind", "status"), [("expire", "expired"), ("fail", "failed")])
def test_wait_step_timeout_ends_flow(env, clock, wait_flows, kind, status):
    flow_id = _start(env, f"waittest_{kind}")
    env.report(flow_id, "book", "admin-agent", output={})
    clock.advance(hours=3)
    assert _scan(env)["expired"] == 1
    assert env.flow(flow_id)["status"] == status


def test_definition_validation():
    agent = AgentStep(name="a", title="a", agent="x", instruction=lambda s: "")
    with pytest.raises(ValueError, match="重複"):
        FlowDefinition(type="t", display_name="t", validate_input=dict, make_title=str,
                       steps=(agent, agent))
    with pytest.raises(ValueError, match="on_failure"):
        FlowDefinition(type="t", display_name="t", validate_input=dict, make_title=str,
                       steps=(AgentStep(name="a", title="a", agent="x", instruction=lambda s: "",
                                        on_failure="zzz"),))
    with pytest.raises(ValueError, match="涵蓋"):
        FlowDefinition(type="t", display_name="t", validate_input=dict, make_title=str,
                       steps=(ApprovalStep(name="c", title="c", approver=lambda s: "",
                                           question=lambda s: "", routes={"approve": END}),))
    with pytest.raises(ValueError, match="不存在"):
        FlowDefinition(type="t", display_name="t", validate_input=dict, make_title=str,
                       steps=(ApprovalStep(name="c", title="c", approver=lambda s: "",
                                           question=lambda s: "", options=(Option("ok", "OK"),),
                                           routes={"ok": "nowhere"}),))
    with pytest.raises(KeyError):
        get_definition("nope")
    with pytest.raises(KeyError):
        get_definition("demo").step("nope")


def test_event_bus_delivery_and_overflow():
    async def run():
        bus = EventBus(queue_size=2)
        queue = bus.subscribe()
        assert bus.subscriber_count == 1
        for i in range(3):
            bus.publish("x", {"i": i})
        first = queue.get_nowait()
        second = queue.get_nowait()
        bus.unsubscribe(queue)
        bus.publish("x", {"i": 99})
        return first, second, queue.qsize()

    first, second, remaining = asyncio.run(run())
    # 滿了丟最舊的
    assert (first["data"]["i"], second["data"]["i"]) == (1, 2)
    assert remaining == 0


def test_event_bus_publish_from_other_thread():
    import threading

    async def run():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        queue = bus.subscribe()
        thread = threading.Thread(target=bus.publish, args=("t", {"ok": True}))
        thread.start()
        thread.join()
        return await asyncio.wait_for(queue.get(), timeout=1)

    event = asyncio.run(run())
    assert event["type"] == "t"


def test_background_scan_loop_runs(tmp_path, clock):
    import time

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.dispatch import FakeDispatcher
    from app.main import create_app

    settings = Settings(db_url=f"sqlite:///{tmp_path / 'scan.db'}", scan_interval=0.05,
                        dispatcher="fake", cors_origins=["http://localhost:5173"])
    app = create_app(settings, dispatcher=FakeDispatcher(), clock=clock)
    calls = []
    original = app.state.engine.scan

    def spy(ctx):
        calls.append(ctx.now)
        return original(ctx)

    app.state.engine.scan = spy
    with TestClient(app) as client:
        for _ in range(40):
            if len(calls) >= 2:
                break
            time.sleep(0.05)
        resp = client.options("/api/flows", headers={"Origin": "http://localhost:5173",
                                                     "Access-Control-Request-Method": "GET"})
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert len(calls) >= 2
    assert isinstance(calls[0], datetime)
