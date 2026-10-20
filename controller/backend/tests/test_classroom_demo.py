"""P6 課堂 demo：visit 流程的示範模式（時限縮短）與確認者「agent 代理／真人」切換。

時間：conftest 的時鐘是台北 2026-10-12（一）10:00；示範拜訪排在當天 10:20–11:20。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.engine import Forbidden
from app.flows import visit
from app.models import Agent, AuditLog, Identity
from tests.test_visit_flow import (
    BOOKING,
    CLAIM,
    CLOSE,
    DRAFT,
    HRM,
    MANAGER,
    SALES,
    _decide,
    _inbox_link,
    _load_identities,
    _report,
    _scan,
    _trip,
    _visit_input,
)

DEMO_START = "2026-10-12T10:20:00+08:00"
DEMO_END = "2026-10-12T11:20:00+08:00"
GOBTA = "gobta"


@pytest.fixture
def venv(env):
    _load_identities(env)
    return env


def _demo_input(**overrides) -> dict:
    data = _visit_input(visit_start_at=DEMO_START, visit_end_at=DEMO_END, demo=True)
    data.update(overrides)
    return data


def _start_demo(env, client=None, **overrides):
    client = client or env.agent("cs-agent")
    return client.post("/api/flows", json={"type": "visit", "input": _demo_input(**overrides)})


def _step(detail: dict, name: str) -> dict:
    return next(s for s in detail["steps"] if s["name"] == name)


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _tpe(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _audit(env, action: str) -> list[AuditLog]:
    with env.session_factory() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


# ---------------------------------------------------------------- 示範模式：時限


def test_demo_flow_runs_with_short_deadlines(venv):
    env = venv
    resp = _start_demo(env)
    assert resp.status_code == 201, resp.text
    flow = resp.json()
    flow_id = flow["id"]
    assert flow["demo"] is True
    assert flow["title"] == "［示範］10/12 拜訪 客戶 C"

    detail = env.flow(flow_id)
    assert detail["state"]["input"]["demo"] is True
    # 稽核記下是誰啟動了示範流程
    started = _audit(env, "flow.demo_started")
    assert len(started) == 1 and started[0].actor == "agent:cs-agent" and started[0].flow_id == flow_id

    # 派車預約只到拜訪結束（等還車回報的截止也是拜訪結束）
    job = env.dispatcher.last_job_for("draft_vehicle")
    assert job.input["booking_start_at"] == DEMO_START
    assert job.input["booking_end_at"] == DEMO_END
    draft = {**DRAFT, "start_at": DEMO_START, "end_at": DEMO_END}
    _report(env, flow_id, "draft_vehicle", "admin-agent", draft)

    # 派車確認截止：拜訪開始前 5 分鐘（10:15）
    detail = env.flow(flow_id)
    confirm = _step(detail, "confirm_vehicle")
    assert _at(confirm["deadline_at"]) == _tpe("2026-10-12T10:15:00+08:00")
    page = _decide(env, SALES, flow_id, "approve")
    assert page["context"]["deadline"] == "10/12 10:15"

    _report(env, flow_id, "book_vehicle", "admin-agent", {**BOOKING, "start_at": DEMO_START, "end_at": DEMO_END})
    detail = env.flow(flow_id)
    assert _at(_step(detail, "await_trip")["deadline_at"]) == _tpe(DEMO_END)

    env.clock.advance(minutes=6)  # 10:06：拜訪時段內（ADM 從開始前 15 分鐘起可登記取車）
    assert _trip(env, flow_id).status_code == 200
    _report(env, flow_id, "record_trip", "admin-agent",
            {"booking_id": 41, "pickup_odometer": 1000, "return_odometer": 1037, "distance_km": 37})
    _report(env, flow_id, "draft_claim", "finance-agent", CLAIM)

    # 報帳核准 15 分鐘；過一半就出現在卡住清單
    detail = env.flow(flow_id)
    approve = _step(detail, "approve_claim")
    assert _at(approve["deadline_at"]) == env.clock.now.replace(microsecond=0) + visit.DEMO_CLAIM_APPROVAL_TIMEOUT
    env.clock.advance(minutes=8)
    assert env.flow(flow_id)["stuck"]["code"] == "approval_waiting_too_long"
    _decide(env, MANAGER, flow_id, "approve", "OK")
    _report(env, flow_id, "close_visit", "cs-agent", CLOSE)
    assert env.flow(flow_id)["status"] == "succeeded"


def test_demo_confirm_deadline_keeps_at_least_five_minutes(venv):
    env = venv
    # 拜訪 10:07 開始：開始前 5 分鐘（10:02）太近，至少保留到 10:05
    flow_id = _start_demo(env, visit_start_at="2026-10-12T10:07:00+08:00").json()["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    confirm = _step(env.flow(flow_id), "confirm_vehicle")
    assert _at(confirm["deadline_at"]) == _tpe("2026-10-12T10:05:00+08:00")


def test_demo_confirm_timeout_expires_before_visit(venv):
    env = venv
    flow_id = _start_demo(env).json()["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    env.clock.advance(minutes=14)
    _scan(env)
    assert env.flow(flow_id)["status"] == "waiting_approval"
    env.clock.advance(minutes=1)  # 10:15
    _scan(env)
    detail = env.flow(flow_id)
    assert detail["status"] == "expired"
    notices = env.agent(SALES).get("/api/inbox").json()["items"]
    assert any(n["kind"] == "approval_expired" and "10/12 10:15" in n["body"] for n in notices)


def test_normal_flow_keeps_original_deadlines(venv):
    env = venv
    resp = env.agent("cs-agent").post("/api/flows", json={"type": "visit", "input": _visit_input(demo=False)})
    flow = resp.json()
    assert flow["demo"] is False and not flow["title"].startswith("［示範］")
    detail = env.flow(flow["id"])
    assert "demo" not in detail["state"]["input"]
    assert env.dispatcher.last_job_for("draft_vehicle").input["booking_end_at"] == "2026-10-14T23:59:00+08:00"
    _report(env, flow["id"], "draft_vehicle", "admin-agent", DRAFT)
    confirm = _step(env.flow(flow["id"]), "confirm_vehicle")
    assert _at(confirm["deadline_at"]) == _tpe("2026-10-13T18:00:00+08:00")
    assert _audit(env, "flow.demo_started") == []


def test_claim_approval_deadline_unit():
    now = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)
    assert visit.claim_approval_deadline({"input": {}}, now) == now + visit.CLAIM_APPROVAL_TIMEOUT
    assert visit.claim_approval_deadline({"input": {"demo": True}}, now) == now + visit.DEMO_CLAIM_APPROVAL_TIMEOUT


# ---------------------------------------------------------------- 示範模式：誰可以啟動


def test_admin_can_start_demo_flow(venv):
    env = venv
    resp = _start_demo(env, client=env.user("admin"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["demo"] is True
    assert _audit(env, "flow.demo_started")[0].actor == "user:admin"


def test_viewer_cannot_start_demo_flow(venv):
    resp = _start_demo(venv, client=venv.user("viewer"))
    assert resp.status_code == 403


def test_demo_flag_must_be_boolean(venv):
    resp = _start_demo(venv, demo="yes")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_input"


def test_demo_not_supported_for_other_flow_types(venv):
    env = venv
    resp = env.user("admin").post("/api/flows", json={
        "type": "demo", "input": {"topic": "x", "approver_hrm_employee_id": HRM[MANAGER], "demo": True}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "demo_not_supported"


def test_only_demo_starters_can_start_demo(venv):
    engine = venv.app.state.engine
    other = Agent(id="admin-agent", role="assistant")
    with pytest.raises(Forbidden) as exc:
        engine.authorize_demo("visit", agent=other, is_admin=False)
    assert exc.value.code == "demo_not_allowed"
    engine.authorize_demo("visit", agent=Agent(id="cs-agent", role="assistant"), is_admin=False)
    engine.authorize_demo("visit", agent=None, is_admin=True)


def test_demo_can_be_disabled(venv):
    env = venv
    env.app.state.engine.settings.demo_enabled = False
    try:
        resp = _start_demo(env)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "demo_disabled"
    finally:
        env.app.state.engine.settings.demo_enabled = True


def test_demo_enabled_env(monkeypatch):
    monkeypatch.setenv("FLOW_CONTROLLER_DEMO_ENABLED", "0")
    assert Settings.from_env().demo_enabled is False
    monkeypatch.delenv("FLOW_CONTROLLER_DEMO_ENABLED")
    assert Settings.from_env().demo_enabled is True


# ---------------------------------------------------------------- 示範模式：只接受今天、馬上要開始的拜訪


@pytest.mark.parametrize(("start", "end", "who"), [
    # 明天的正式拜訪：cs-agent 被誘導帶 demo=true 也不行
    ("2026-10-13T10:00:00+08:00", "2026-10-13T11:00:00+08:00", "cs-agent"),
    # 今天、但超過 3 小時後才開始
    ("2026-10-12T13:30:00+08:00", "2026-10-12T14:30:00+08:00", "cs-agent"),
    # 今天、已經結束
    ("2026-10-12T08:00:00+08:00", "2026-10-12T09:00:00+08:00", "cs-agent"),
    # admin 也一樣（示範模式的時限只對馬上要開始的拜訪有意義）
    ("2026-10-14T10:00:00+08:00", "2026-10-14T12:00:00+08:00", "admin"),
])
def test_demo_rejects_visits_outside_window(venv, start, end, who):
    env = venv
    client = env.user("admin") if who == "admin" else env.agent(who)
    resp = _start_demo(env, client=client, visit_start_at=start, visit_end_at=end)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "demo_input_rejected"
    assert _audit(env, "flow.demo_started") == []
    assert env.user("admin").get("/api/flows").json()["items"] == []


def test_demo_accepts_visit_in_progress_and_within_three_hours(venv):
    env = venv
    # 已經開始、還沒結束（例如 fake_agents --demo --visit-date 今天）
    resp = _start_demo(env, follow_up_id=701, visit_start_at="2026-10-12T10:00:00+08:00",
                       visit_end_at="2026-10-12T12:00:00+08:00")
    assert resp.status_code == 201, resp.text
    # 剛好 3 小時後開始
    resp = _start_demo(env, follow_up_id=702, visit_start_at="2026-10-12T13:00:00+08:00",
                       visit_end_at="2026-10-12T14:00:00+08:00")
    assert resp.status_code == 201, resp.text


def test_demo_rerun_after_window_returns_existing_flow(venv):
    env = venv
    first = _start_demo(env).json()
    env.clock.advance(minutes=90)  # 11:30：拜訪已經結束，重跑 start 仍回同一條流程（去重）
    again = _start_demo(env)
    assert again.status_code in (200, 201), again.text
    assert again.json()["id"] == first["id"] and again.json()["created"] is False


def test_demo_check_unit():
    now = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)  # 台北 10:00
    ok = {"visit_start_at": DEMO_START, "visit_end_at": DEMO_END}
    visit.demo_check(ok, now)
    with pytest.raises(ValueError, match="今天"):
        visit.demo_check({"visit_start_at": "2026-10-11T23:30:00+08:00",
                          "visit_end_at": "2026-10-12T10:30:00+08:00"}, now)


def test_flow_list_shows_demo(venv):
    env = venv
    _start_demo(env)
    env.agent("cs-agent").post("/api/flows", json={"type": "visit", "input": _visit_input(follow_up_id=999)})
    items = env.user("viewer").get("/api/flows").json()["items"]
    assert sorted(item["demo"] for item in items) == [False, True]


# ---------------------------------------------------------------- 確認者：agent 代理／真人


def _set_mode(client, recipient: str, mode: str):
    return client.put("/api/inbox/confirm-mode", params={"recipient": recipient}, json={"mode": mode})


def test_confirm_mode_defaults_to_agent(venv):
    resp = venv.agent(GOBTA).get("/api/inbox/confirm-mode")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirm_mode"] == "agent" and body["human"] is False
    assert body["recipient"]["hrm_employee_id"] == HRM[GOBTA]
    assert body["agent_id"] == GOBTA


def test_admin_switches_confirmer_to_human_and_back(venv):
    env = venv
    flow_id = _start_demo(env, assignee_hrm_employee_id=HRM[GOBTA]).json()["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)

    # 切換前：gobta 的 agent 看得到確認連結
    assert _inbox_link(env, GOBTA, flow_id)

    resp = _set_mode(env.user("admin"), GOBTA, "human")  # 可以用 agent id 指定確認者
    assert resp.status_code == 200, resp.text
    assert resp.json()["confirm_mode"] == "human" and resp.json()["changed"] is True
    audits = _audit(env, "identity.confirm_mode")
    assert len(audits) == 1
    assert audits[0].actor == "user:admin"
    assert audits[0].detail["from"] == "agent" and audits[0].detail["to"] == "human"

    # agent-inbox 查得到「由真人確認」
    assert env.agent(GOBTA).get("/api/inbox/confirm-mode").json()["human"] is True
    # 員工 agent 拿不到確認連結（即使不先查確認方式也無法代為確認）
    items = env.agent(GOBTA).get("/api/inbox").json()["items"]
    request = next(i for i in items if i["kind"] == "approval_request")
    assert request["link"] is None and request["link_hidden"] is True
    # 講師用 admin 帳號切到 Gobta 的收件匣，看得到連結並親自確認
    admin_items = env.user("admin").get("/api/inbox", params={"recipient": HRM[GOBTA]}).json()
    assert admin_items["recipient"]["confirm_mode"] == "human"
    approval_id, token = env.latest_approval_link(HRM[GOBTA])
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "approve", "comment": "講師親自核准"})
    assert resp.status_code == 200, resp.text
    assert env.flow(flow_id)["current_step"] == "book_vehicle"

    # 收件人清單也看得到確認方式
    people = env.user("admin").get("/api/inbox/recipients").json()["items"]
    assert next(p for p in people if p["hrm_employee_id"] == HRM[GOBTA])["confirm_mode"] == "human"

    # 同一個值再設一次：不重複寫稽核
    assert _set_mode(env.user("admin"), HRM[GOBTA], "human").json()["changed"] is False
    assert len(_audit(env, "identity.confirm_mode")) == 1

    # 切回 agent 代理
    resp = _set_mode(env.user("admin"), "AGT-GOBTA", "agent")
    assert resp.json()["confirm_mode"] == "agent"
    assert env.agent(GOBTA).get("/api/inbox/confirm-mode").json()["human"] is False
    with env.session_factory() as db:
        identity = db.scalar(select(Identity).where(Identity.agent_id == GOBTA))
        assert identity.confirm_mode == "agent"


def test_link_visible_again_after_switching_back(venv):
    env = venv
    flow_id = _start_demo(env, assignee_hrm_employee_id=HRM[GOBTA]).json()["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    _set_mode(env.user("admin"), GOBTA, "human")
    with pytest.raises(AssertionError):
        _inbox_link(env, GOBTA, flow_id)
    _set_mode(env.user("admin"), GOBTA, "agent")
    assert _inbox_link(env, GOBTA, flow_id)


def test_only_admin_can_switch_confirm_mode(venv):
    env = venv
    assert _set_mode(env.user("viewer"), GOBTA, "human").status_code == 403
    assert _set_mode(env.agent(GOBTA), GOBTA, "human").status_code == 401
    assert _set_mode(env.anon, GOBTA, "human").status_code == 401
    assert _audit(env, "identity.confirm_mode") == []


def test_confirm_mode_validation(venv):
    env = venv
    resp = _set_mode(env.user("admin"), GOBTA, "robot")
    assert resp.status_code == 422
    resp = _set_mode(env.user("admin"), "nobody", "human")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "recipient_not_found"


def test_agent_cannot_read_other_confirm_mode(venv):
    env = venv
    resp = env.agent(GOBTA).get("/api/inbox/confirm-mode", params={"recipient": MANAGER})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_your_inbox"
    # admin 可以查任何人
    assert env.user("admin").get("/api/inbox/confirm-mode", params={"recipient": MANAGER}).status_code == 200
