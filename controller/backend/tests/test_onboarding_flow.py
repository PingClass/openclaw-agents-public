"""流程 ③（onboarding）：用 FakeDispatcher＋假 agent token 跑完整條，以及各種分支。

時間：conftest 的時鐘是台北 2026-10-12（一）10:00；新人 10/19（一）到職，
所以等 IT 指派筆電的截止是 10/16（五）18:00；主管確認收到是 2 個工作天。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.cli import import_identities
from app.flows import onboarding
from app.models import Flow, Identity, User
from tests.test_visit_flow import HRM, RECORDS, _decide, _inbox_link, _load_identities, _notifications, _report, _scan

HR = "shion"
IT = "souei"
MANAGER = "main"
NEW_HIRE = "hrm-new-0001"

LAPTOP = {"asset_code": "LT-003", "asset_name": "ThinkPad T14", "specs": "i5／16GB／512GB",
          "reason": "業務需要外出，選輕薄機種", "alternatives": ["LT-004", "LT-005"], "available_count": 3}
ROOM = {"booking_id": 77, "room_id": 2, "room_name": "小會議室 B",
        "start_at": "2026-10-19T09:30:00+08:00", "end_at": "2026-10-19T10:30:00+08:00", "created": True}


def _input(**overrides) -> dict:
    data = {
        "employee_id": NEW_HIRE,
        "name": "王小明",
        "employee_number": "E2026101",
        "department_id": 1,
        "department_name": "業務部",
        "job_title": "業務專員",
        "start_date": "2026-10-19",
    }
    data.update(overrides)
    return data


def _notice(**overrides) -> dict:
    data = {
        "manager_hrm_employee_ids": [HRM[MANAGER]],
        "checklist": "1. 10/19 09:30 小會議室 B 到職說明\n2. 筆電 LT-003 已配發\n3. 第一週帶新人拜訪客戶",
        "items": ["到職說明", "筆電", "第一週安排"],
    }
    data.update(overrides)
    return data


@pytest.fixture
def oenv(env):
    _load_identities(env)
    return env


def _start(env, agent: str = "hr-agent", **overrides):
    return env.agent(agent).post("/api/flows", json={"type": "onboarding", "input": _input(**overrides)})


def _started(env, **overrides) -> str:
    resp = _start(env, **overrides)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _assign(env, flow_id: str, agent: str = IT, **body):
    payload = {"type": "laptop_assigned", "asset_code": "LT-003"}
    payload.update(body)
    return env.agent(agent).post(f"/api/flows/{flow_id}/events", json=payload)


def _to_await_laptop(env, **overrides) -> str:
    flow_id = _started(env, **overrides)
    _report(env, flow_id, "draft_laptop", "it-agent", LAPTOP)
    return flow_id


def _to_notice(env) -> str:
    flow_id = _to_await_laptop(env)
    assert _assign(env, flow_id).status_code == 200
    _report(env, flow_id, "book_room", "admin-agent", ROOM)
    return flow_id


def _to_ack(env) -> str:
    flow_id = _to_notice(env)
    _report(env, flow_id, "onboarding_notice", "hr-agent", _notice())
    return flow_id


def _admin_identity(env) -> None:
    """admin 帳號連到一個 Identity，才有收件匣收 alert。"""
    with env.session_factory() as db:
        ops = Identity(hrm_employee_id="ops-1", display_name="值班管理員", role="")
        db.add(ops)
        db.flush()
        db.scalar(select(User).where(User.username == "admin")).identity_id = ops.id
        db.commit()


# ---------------------------------------------------------------- 完整流程


def test_onboarding_flow_end_to_end(oenv):
    env = oenv
    resp = _start(env)
    assert resp.status_code == 201, resp.text
    flow = resp.json()
    flow_id = flow["id"]
    assert flow["title"] == "10/19 到職 王小明（業務部）"
    assert flow["current_step"] == "draft_laptop"

    detail = env.flow(flow_id)
    resolve = detail["steps"][0]
    assert resolve["status"] == "succeeded"
    assert resolve["summary"] == "人資 Shion、IT Souei、主管 Main"
    assert detail["state"]["participants"] == [IT]

    # 步驟 2：it-agent 產生配發建議
    job = env.dispatcher.last_job_for("draft_laptop")
    assert job.agent_id == "it-agent"
    assert job.skill == "laptop-allocation-draft"
    assert job.input["new_hire"]["hrm_employee_id"] == NEW_HIRE
    assert job.input["new_hire"]["department_name"] == "業務部"
    assert "不得指派保管人" in job.instruction
    _report(env, flow_id, "draft_laptop", "it-agent", LAPTOP, attempt=1)

    # 步驟 3：IT 人員收件匣收到任務（含建議資產編號），流程等 laptop_assigned
    detail = env.flow(flow_id)
    assert detail["current_step"] == "await_laptop"
    wait = next(s for s in detail["steps"] if s["name"] == "await_laptop")
    assert wait["assignee"] == IT and wait["status"] == "running"
    assert wait["deadline_at"].startswith("2026-10-16T10:00")  # 10/16 18:00 台北
    tasks = [n for n in _notifications(env, IT) if n["kind"] == onboarding.KIND_TASK]
    assert len(tasks) == 1
    assert "LT-003" in tasks[0]["body"] and tasks[0]["flow_id"] == flow_id
    assert f"/api/flows/{flow_id}/events" in tasks[0]["body"]
    # IT 人員的員工 agent 可以讀這條流程的配發建議
    view = env.agent(IT).get(f"/api/flows/{flow_id}")
    assert view.status_code == 200
    assert view.json()["state"]["steps"]["draft_laptop"]["asset_code"] == "LT-003"
    # 這一步不接受 flow-report
    resp = env.report(flow_id, "await_laptop", IT, output={"asset_code": "LT-003"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "use_events"

    resp = _assign(env, flow_id, asset_code="LT-004", note="LT-003 電池膨脹，改配 LT-004")
    assert resp.status_code == 200, resp.text
    detail = env.flow(flow_id)
    assigned = detail["state"]["steps"]["await_laptop"]
    assert assigned["asset_code"] == "LT-004" and assigned["matches_suggestion"] is False
    assert assigned["custodian_hrm_employee_id"] == NEW_HIRE

    # 步驟 4：admin-agent 代訂說明會議室給 HR
    job = env.dispatcher.last_job_for("book_room")
    assert job.agent_id == "admin-agent" and job.skill == "adm-room-book"
    assert job.input["booked_for_hrm_employee_id"] == HRM[HR]
    assert job.input["preferred_start_at"] == "2026-10-19T09:30:00+08:00"
    assert job.input["latest_end_at"] == "2026-10-19T12:00:00+08:00"
    assert job.idempotency_key == f"{flow_id}:book_room"
    _report(env, flow_id, "book_room", "admin-agent", ROOM)

    # 步驟 5：hr-agent 查部門主管、產生到職清單
    job = env.dispatcher.last_job_for("onboarding_notice")
    assert job.agent_id == "hr-agent" and job.skill == "onboarding-notice"
    assert job.input["department_id"] == 1
    assert job.input["expected_manager"] == {"hrm_employee_id": HRM[MANAGER], "display_name": "Main"}
    assert job.input["laptop"]["asset_code"] == "LT-004"
    assert job.input["orientation"]["room_name"] == "小會議室 B"
    _report(env, flow_id, "onboarding_notice", "hr-agent", _notice(
        checklist="王小明 電話 0912-345-678，10/19 09:30 到職說明", new_hire_phone="0912345678"))

    # 步驟 6：主管確認收到（清單不含電話）
    detail = env.flow(flow_id)
    assert detail["current_step"] == "ack_manager"
    confirm = next(s for s in detail["steps"] if s["name"] == "confirm_manager")
    assert confirm["status"] == "succeeded" and "遮掉" in confirm["summary"]
    page = _decide(env, MANAGER, flow_id, "ack", "收到，謝謝")
    assert page["options"] == [{"value": "ack", "label": "確認收到"},
                               {"value": "missing", "label": "有缺漏（請在意見說明）"}]
    assert "0912" not in str(page) and "phone" not in str(page)
    assert page["context"]["laptop"] == "LT-004"
    assert page["context"]["orientation"] == "小會議室 B 10/19 09:30–10:30"
    assert "到職清單" in page["question"]

    detail = env.flow(flow_id)
    assert detail["status"] == "succeeded"
    statuses = {s["name"]: s["status"] for s in detail["steps"]}
    assert statuses["report_gap"] == "skipped"
    assert all(statuses[name] == "succeeded" for name in statuses if name != "report_gap")
    assert "0912" not in str(detail)
    # HR 沒有收到任何 alert
    assert [n for n in _notifications(env, HR) if n["kind"] == "alert"] == []


def test_dedupe_by_hrm_employee_id(oenv):
    first = _start(oenv).json()
    again = _start(oenv, name="王小明（重送）")
    assert again.status_code == 200
    assert again.json()["id"] == first["id"] and again.json()["created"] is False
    other = _start(oenv, employee_id="hrm-new-0002")
    assert other.status_code == 201 and other.json()["id"] != first["id"]


@pytest.mark.parametrize("agent", ["cs-agent", "it-agent", "admin-agent", MANAGER, HR, "milim"])
def test_only_hr_agent_can_start(oenv, agent):
    resp = _start(oenv, agent=agent)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_allowed_to_start"


@pytest.mark.parametrize(("overrides", "message"), [
    ({"employee_id": ""}, "employee_id 必填"),
    ({"name": None}, "name 必填"),
    ({"department_id": None}, "department_id 必填"),
    ({"department_name": " "}, "department_name 必填"),
    ({"start_date": "10/19"}, "start_date 格式錯誤"),
    ({"start_date": None}, "start_date 必填"),
])
def test_invalid_input(oenv, overrides, message):
    resp = _start(oenv, **overrides)
    assert resp.status_code == 422
    assert message in resp.json()["detail"]["message"]


def test_input_keeps_only_needed_fields(oenv):
    flow_id = _started(oenv, phone="0912-000-000", home_address="台北市", department_id="7")
    state_input = oenv.flow(flow_id)["state"]["input"]
    assert set(state_input) == {"employee_id", "name", "employee_number", "department_id", "department_name",
                                "job_title", "start_date"}
    assert state_input["department_id"] == 7


# ---------------------------------------------------------------- 部門沒有主管：停下通知 HR


def test_department_without_manager_stops_and_notifies_hr(oenv):
    env = oenv
    _admin_identity(env)
    flow_id = _started(env, department_id=3, department_name="資訊部")
    detail = env.flow(flow_id)
    assert detail["status"] == "running"
    assert detail["current_step"] == "resolve_parties"
    assert detail["steps"][0]["status"] == "failed"
    assert detail["stuck"]["code"] == "system_error"
    assert "資訊部沒有部門主管" in detail["stuck"]["reason"]
    assert env.dispatcher.step_jobs == []  # 沒有猜一個人就往下走

    alerts = [n for n in _notifications(env, HR) if n["kind"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["flow_id"] == flow_id
    assert "資訊部沒有部門主管" in alerts[0]["body"] and "不會自己挑主管" in alerts[0]["body"]
    admin_inbox = env.user("admin").get("/api/inbox", params={"recipient": "ops-1"}).json()["items"]
    assert any("資訊部沒有部門主管" in n["body"] for n in admin_inbox)
    # 沒有任何人收到確認通知
    assert [n for n in _notifications(env, MANAGER) if n["flow_id"] == flow_id] == []

    # HR 指定主管、管理員補上 Identity 後重試就繼續
    with env.session_factory() as db:
        import_identities(db, [{"hrm_employee_id": "hrm-it-boss", "employee_number": "E900",
                                "display_name": "資訊部主管", "role": "manager", "department": "資訊部"}])
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/resolve_parties/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_step"] == "draft_laptop"
    assert env.flow(flow_id)["state"]["parties"]["manager"]["hrm_employee_id"] == "hrm-it-boss"


def test_department_with_two_managers_stops_and_notifies_hr(oenv):
    with oenv.session_factory() as db:
        import_identities(db, [{"hrm_employee_id": "hrm-x", "employee_number": "X1", "display_name": "另一位主管",
                                "role": "manager", "department": "業務部"}])
    flow_id = _started(oenv)
    assert "有 2 位主管" in oenv.flow(flow_id)["stuck"]["reason"]
    alerts = [n for n in _notifications(oenv, HR) if n["kind"] == "alert"]
    assert len(alerts) == 1 and "有 2 位主管" in alerts[0]["body"]


def test_missing_hr_or_it_stops_and_notifies_admin(env):
    _load_identities(env, [r for r in RECORDS if r["agent_id"] != IT])
    _admin_identity(env)
    flow_id = _started(env)
    assert "找不到IT 人員" in env.flow(flow_id)["stuck"]["reason"]
    admin_inbox = env.user("admin").get("/api/inbox", params={"recipient": "ops-1"}).json()["items"]
    assert any("IT 人員" in n["body"] for n in admin_inbox)


def test_hrm_reports_no_manager_stops_and_notifies_hr(oenv):
    env = oenv
    flow_id = _to_notice(env)
    _report(env, flow_id, "onboarding_notice", "hr-agent", _notice(manager_hrm_employee_ids=[]))
    detail = env.flow(flow_id)
    assert detail["current_step"] == "confirm_manager"
    assert detail["stuck"]["code"] == "system_error"
    assert "HRM 查不到業務部的部門主管" in detail["stuck"]["reason"]
    alerts = [n for n in _notifications(env, HR) if n["kind"] == "alert"]
    assert len(alerts) == 1 and "HRM 查不到業務部的部門主管" in alerts[0]["body"]
    assert [n for n in _notifications(env, MANAGER) if n["flow_id"] == flow_id] == []

    # HR 在 HRM 補上主管後，admin 重試 confirm_manager：回到 onboarding_notice 請 hr-agent 重查
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/confirm_manager/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_step"] == "onboarding_notice"
    jobs = [job for job in env.dispatcher.step_jobs if job.step == "onboarding_notice"]
    assert len(jobs) == 2 and jobs[-1].attempt == 2
    _report(env, flow_id, "onboarding_notice", "hr-agent", _notice(), attempt=2)
    detail = env.flow(flow_id)
    assert detail["current_step"] == "ack_manager"
    confirm = next(s for s in detail["steps"] if s["name"] == "confirm_manager")
    assert confirm["status"] == "succeeded" and confirm["summary"] == "主管 Main"
    assert detail["state"]["manager_recheck"] is False


def test_hrm_manager_mismatch_stops(oenv):
    flow_id = _to_notice(oenv)
    _report(oenv, flow_id, "onboarding_notice", "hr-agent",
            _notice(manager_hrm_employee_ids=[HRM["hinata"]]))
    assert "不一致" in oenv.flow(flow_id)["stuck"]["reason"]
    assert len([n for n in _notifications(oenv, HR) if n["kind"] == "alert"]) == 1


def test_manager_mismatch_retry_reresolves_identity(oenv):
    """HRM 的主管換人：管理員更新 Identity 後重試，controller 要重新解析主管，確認者改成新主管。"""
    env = oenv
    flow_id = _to_notice(env)
    _report(env, flow_id, "onboarding_notice", "hr-agent", _notice(manager_hrm_employee_ids=["hrm-sales-boss"]))
    detail = env.flow(flow_id)
    assert "不一致" in detail["stuck"]["reason"]

    # 只重試、還沒更新 Identity：Identity 仍是 Main，重查後 HRM 還是新主管 → 再停一次
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/confirm_manager/retry",
                                  json={"revision": detail["revision"]})
    assert resp.json()["current_step"] == "onboarding_notice"
    _report(env, flow_id, "onboarding_notice", "hr-agent",
            _notice(manager_hrm_employee_ids=["hrm-sales-boss"]), attempt=2)
    detail = env.flow(flow_id)
    assert detail["current_step"] == "confirm_manager" and "不一致" in detail["stuck"]["reason"]

    # 管理員更新 Identity：Main 不再是主管，新主管匯入
    with env.session_factory() as db:
        import_identities(db, [
            {"hrm_employee_id": HRM[MANAGER], "role": "sales"},
            {"hrm_employee_id": "hrm-sales-boss", "employee_number": "E901", "display_name": "新業務主管",
             "role": "manager", "department": "業務部"},
        ])
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/confirm_manager/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_step"] == "onboarding_notice"
    detail = env.flow(flow_id)
    assert detail["state"]["parties"]["manager"]["hrm_employee_id"] == "hrm-sales-boss"
    job = env.dispatcher.last_job_for("onboarding_notice")
    assert job.input["expected_manager"]["hrm_employee_id"] == "hrm-sales-boss"
    _report(env, flow_id, "onboarding_notice", "hr-agent",
            _notice(manager_hrm_employee_ids=["hrm-sales-boss"]), attempt=3)
    detail = env.flow(flow_id)
    assert detail["current_step"] == "ack_manager"
    with env.session_factory() as db:
        boss = db.scalar(select(Identity).where(Identity.hrm_employee_id == "hrm-sales-boss"))
        assert boss is not None
    ack = next(s for s in detail["steps"] if s["name"] == "ack_manager")
    assert ack["status"] == "waiting_approval"
    assert [n for n in _notifications(env, MANAGER) if n["flow_id"] == flow_id] == []


def test_manager_recheck_stops_again_when_identity_has_no_manager(oenv):
    env = oenv
    flow_id = _to_notice(env)
    _report(env, flow_id, "onboarding_notice", "hr-agent", _notice(manager_hrm_employee_ids=[]))
    detail = env.flow(flow_id)
    # 管理員把 Main 改掉、沒有補新主管：重試要再次停下通知 HR，不能沿用舊主管
    with env.session_factory() as db:
        import_identities(db, [{"hrm_employee_id": HRM[MANAGER], "role": "sales"}])
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/confirm_manager/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    detail = env.flow(flow_id)
    assert detail["current_step"] == "confirm_manager"
    assert "業務部沒有部門主管" in detail["stuck"]["reason"]
    assert detail["state"]["manager_recheck"] is True
    alerts = [n for n in _notifications(env, HR) if n["kind"] == "alert"]
    assert len(alerts) == 2 and any("業務部沒有部門主管" in n["body"] for n in alerts)


def test_notice_redacts_address_and_parenthesized_phone(oenv):
    env = oenv
    flow_id = _to_notice(env)
    _report(env, flow_id, "onboarding_notice", "hr-agent", _notice(
        checklist="王小明 住台北市大安區忠孝東路四段100號5樓，緊急聯絡 (02) 2345-6789",
        items=["住址 新北市板橋區文化路一段188巷44號", "筆電 LT-003"]))
    detail = env.flow(flow_id)
    notice = detail["state"]["steps"]["onboarding_notice"]
    assert notice["pii_redacted"] is True
    assert "忠孝東路" not in str(detail) and "2345-6789" not in str(detail) and "文化路" not in str(detail)
    assert notice["items"][1] == "筆電 LT-003"


def test_notice_requires_manager_ids_field(oenv):
    flow_id = _to_notice(oenv)
    resp = oenv.report(flow_id, "onboarding_notice", "hr-agent", output={"checklist": "x"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "missing_output_fields"


# ---------------------------------------------------------------- IT 指派：只接受 IT 人員


@pytest.mark.parametrize("agent", [MANAGER, HR, "hr-agent", "it-agent", "admin-agent", "mjolmire-businessman"])
def test_laptop_assigned_only_from_it_person(oenv, agent):
    flow_id = _to_await_laptop(oenv)
    resp = _assign(oenv, flow_id, agent=agent)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_reporter"
    assert oenv.flow(flow_id)["current_step"] == "await_laptop"


def test_laptop_assigned_validation_and_timing(oenv):
    env = oenv
    flow_id = _started(env)
    # 還在 draft_laptop：太早
    resp = _assign(env, flow_id)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "step_not_current"
    _report(env, flow_id, "draft_laptop", "it-agent", LAPTOP)
    resp = _assign(env, flow_id, asset_code=" ")
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "invalid_event"
    resp = env.agent(IT).post(f"/api/flows/{flow_id}/events", json={"type": "trip_report"})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "unknown_event"
    assert _assign(env, flow_id).status_code == 200
    assert env.flow(flow_id)["state"]["steps"]["await_laptop"]["matches_suggestion"] is True
    # 已經回報過
    resp = _assign(env, flow_id)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "step_not_current"


def test_it_person_without_it_role_cannot_report(oenv):
    """reporter 只認 resolve_parties 解析出的 role=it Identity。"""
    flow_id = _to_await_laptop(oenv)
    with oenv.session_factory() as db:
        flow = db.get(Flow, flow_id)
        state = dict(flow.state)
        state["parties"] = {**state["parties"], "it": {**state["parties"]["it"], "role": "sales"}}
        flow.state = state
        db.commit()
    resp = _assign(oenv, flow_id)
    assert resp.status_code == 403


def test_laptop_wait_timeout_marks_stuck(oenv, clock):
    flow_id = _to_await_laptop(oenv)
    clock.now = datetime(2026, 10, 16, 10, 1, tzinfo=UTC)  # 10/16 18:01 台北
    assert _scan(oenv)["expired"] == 0
    detail = oenv.flow(flow_id)
    assert detail["status"] == "running"
    assert detail["stuck"]["code"] == "agent_no_report"
    assert "等 IT 人員在 ADM 指派筆電" in detail["stuck"]["reason"] and IT in detail["stuck"]["reason"]
    # 逾時後回報仍可接手
    assert _assign(oenv, flow_id).status_code == 200


def test_draft_laptop_needs_human_when_no_laptop(oenv):
    flow_id = _started(oenv)
    resp = oenv.report(flow_id, "draft_laptop", "it-agent", status="needs_human",
                       summary="ADM 沒有未指派的筆電")
    assert resp.status_code == 200
    stuck = oenv.flow(flow_id)["stuck"]
    assert stuck["code"] == "retries_exhausted" and "沒有未指派的筆電" in stuck["reason"]
    assert [n for n in _notifications(oenv, IT) if n["flow_id"] == flow_id] == []


# ---------------------------------------------------------------- 主管確認


def test_manager_ack_timeout_is_stuck_not_finished(oenv, clock):
    env = oenv
    flow_id = _to_ack(env)
    step = next(s for s in env.flow(flow_id)["steps"] if s["name"] == "ack_manager")
    # 週一 10:00 開始 → 2 個工作天 → 週三 10:00
    assert step["deadline_at"].startswith("2026-10-14T02:00")
    clock.advance(hours=25)
    hr_agent = env.agent("hr-agent")  # 用 agent token 查（admin 的登入 session 12 小時就過期）
    stuck = hr_agent.get(f"/api/flows/{flow_id}").json()["stuck"]
    assert stuck["code"] == "approval_waiting_too_long"
    assert "Main" in stuck["reason"]
    clock.now = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)
    assert _scan(env)["expired"] == 0
    detail = hr_agent.get(f"/api/flows/{flow_id}").json()
    assert detail["status"] == "waiting_approval"
    assert detail["current_step"] == "ack_manager"
    assert detail["stuck"]["code"] == "approval_waiting_too_long"


def test_manager_reports_gap_forwards_to_hr(oenv):
    env = oenv
    flow_id = _to_ack(env)
    _decide(env, MANAGER, flow_id, "missing", "缺少門禁卡申請")
    detail = env.flow(flow_id)
    assert detail["status"] == "succeeded"
    gap = next(s for s in detail["steps"] if s["name"] == "report_gap")
    assert gap["status"] == "succeeded" and "缺少門禁卡申請" in gap["summary"]
    notices = [n for n in _notifications(env, HR) if n["flow_id"] == flow_id]
    assert len(notices) == 1 and "缺少門禁卡申請" in notices[0]["body"]


def test_manager_inbox_link_is_onboarding(oenv):
    flow_id = _to_ack(oenv)
    approval_id, token = _inbox_link(oenv, MANAGER, flow_id)
    page = oenv.anon.get(f"/api/approvals/{approval_id}", params={"token": token}).json()
    assert page["step_title"] == "主管確認收到到職清單"
    assert "到職" in page["question"]


# ---------------------------------------------------------------- 單元


def test_business_days_and_deadlines():
    friday = datetime(2026, 10, 16, 15, 0, tzinfo=onboarding.TZ)
    assert onboarding.add_business_days(friday, 2) == datetime(2026, 10, 20, 15, 0, tzinfo=onboarding.TZ)
    state = {"input": {"start_date": "2026-10-19"}}
    now = datetime(2026, 10, 12, 10, 0, tzinfo=onboarding.TZ)
    assert onboarding.laptop_deadline(state, now) == datetime(2026, 10, 16, 18, 0, tzinfo=onboarding.TZ)
    late = datetime(2026, 10, 16, 17, 0, tzinfo=onboarding.TZ)
    assert onboarding.laptop_deadline(state, late) == datetime(2026, 10, 16, 21, 0, tzinfo=onboarding.TZ)


@pytest.mark.parametrize(("text", "redacted"), [
    ("手機 0912-345-678", True),
    ("電話0223456789", True),
    ("+886 912 345 678", True),
    ("市話 (02) 2345-6789", True),
    ("市話 (04)2234-5678", True),
    ("手機 0912.345.678", True),
    ("+886 (2) 2345-6789", True),
    ("台北市大安區忠孝東路四段100號5樓", True),
    ("中山路12號之3", True),
    ("員工編號 E20261019001、資產 LT-0001、2026-10-19T09:30:00+08:00", False),
    ("10/19 09:30–10:30 小會議室 B，會議室在 3 樓", False),
    ("第一週帶新人拜訪客戶，2026-10-19 到職", False),
])
def test_redact_pii(text, redacted):
    cleaned, changed = onboarding.redact_pii(text)
    assert changed is redacted
    if redacted:
        assert onboarding.REDACTED in cleaned
    else:
        assert cleaned == text
