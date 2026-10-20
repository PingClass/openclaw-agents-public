"""流程 ①（visit）：用 FakeDispatcher＋假 agent token 跑完整條，以及各種分支。

時間：conftest 的時鐘是台北 2026-10-12（一）10:00；拜訪排在 10/14 10:00–12:00，
所以派車確認截止是 10/13 18:00，等還車回報截止是 10/14 23:59。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.cli import import_identities
from app.flows import visit
from app.models import Agent, AgentToken, Identity, User
from app.security import new_token, sha256_hex
from tests.conftest import APPROVER

IDENTITIES_FILE = Path(__file__).resolve().parents[2] / "deploy" / "identities.json"
RECORDS = json.loads(IDENTITIES_FILE.read_text(encoding="utf-8"))["identities"]
HRM = {record["agent_id"]: record["hrm_employee_id"] for record in RECORDS}
SALES = "mjolmire-businessman"
MANAGER = "main"
FINANCE = "luminous"

DRAFT = {
    "vehicle_id": 2,
    "vehicle_code": "ABC-1234",
    "vehicle_name": "Altis",
    "seats": 5,
    "start_at": "2026-10-14T10:00:00+08:00",
    "end_at": "2026-10-14T12:00:00+08:00",
    "destination": "台中 客戶 C 工廠",
    "purpose": "客戶拜訪：客戶 C",
}
BOOKING = {"booking_id": 41, "vehicle_id": 2, "start_at": DRAFT["start_at"], "end_at": DRAFT["end_at"],
           "created": True}
CLAIM = {"distance_km": 37, "rate_twd_per_km": 5, "amount_twd": 185, "rate_is_placeholder": True,
         "description": "10/14 拜訪客戶 C，37 km"}
CLOSE = {"interaction_id": "6f1d2c3b-0a9e-4c1f-8f5a-1b2c3d4e5f60",
         "follow_up_id": "0b7e4a52-3f7c-4d2e-9a1b-2c3d4e5f6a7b", "follow_up_status": "completed"}


def _visit_input(**overrides) -> dict:
    data = {
        "follow_up_id": 123,
        "customer": {"name": "客戶 C", "external_id": "C-003"},
        "visit_start_at": "2026-10-14T10:00:00+08:00",
        "visit_end_at": "2026-10-14T12:00:00+08:00",
        "location": "台中 客戶 C 工廠",
        "seats": 2,
        "assignee_hrm_employee_id": HRM[SALES],
        "subject": "新品試吃",
    }
    data.update(overrides)
    return data


def _load_identities(env, records=RECORDS) -> None:
    with env.session_factory() as db:
        # conftest 的王經理（APPROVER）也標成 main 扮演；visit 只用 identities.json 的 7 個人
        old = db.scalar(select(Identity).where(Identity.hrm_employee_id == APPROVER))
        old.agent_id = None
        db.commit()
        import_identities(db, records)
        for record in RECORDS:
            agent_id = record["agent_id"]
            if db.get(Agent, agent_id) is None:
                db.add(Agent(id=agent_id, display_name=record["display_name"], role="employee"))
        db.flush()
        for record in RECORDS:
            agent_id = record["agent_id"]
            if agent_id not in env.tokens:
                raw = new_token("fca")
                env.tokens[agent_id] = raw
                db.add(AgentToken(agent_id=agent_id, token_hash=sha256_hex(raw)))
        db.commit()


@pytest.fixture
def venv(env):
    _load_identities(env)
    return env


def _start(env, **overrides) -> dict:
    resp = env.agent("cs-agent").post("/api/flows", json={"type": "visit", "input": _visit_input(**overrides)})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _inbox_link(env, agent_id: str, flow_id: str) -> tuple[str, str]:
    """員工 agent 用自己的 token 讀收件匣，取出這條流程最新的確認連結。"""
    resp = env.agent(agent_id).get("/api/inbox", params={"unread_only": True})
    assert resp.status_code == 200, resp.text
    for item in resp.json()["items"]:
        if item["kind"] == "approval_request" and item["flow_id"] == flow_id and item["link"]:
            parsed = urlparse(item["link"])
            return parsed.path.rsplit("/", 1)[-1], parse_qs(parsed.query)["token"][0]
    raise AssertionError(f"{agent_id} 的收件匣沒有 {flow_id} 的確認連結")


def _decide(env, agent_id: str, flow_id: str, decision: str, comment: str | None = None) -> dict:
    approval_id, token = _inbox_link(env, agent_id, flow_id)
    page = env.anon.get(f"/api/approvals/{approval_id}", params={"token": token})
    assert page.status_code == 200, page.text
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": decision, "comment": comment})
    assert resp.status_code == 200, resp.text
    return page.json()


def _report(env, flow_id: str, step: str, agent_id: str, output: dict, **body):
    resp = env.report(flow_id, step, agent_id, output=output, summary=f"{step} 完成", **body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _trip(env, flow_id: str, agent_id: str = SALES, **body):
    payload = {"type": "trip_report", "pickup_odometer": 1000, "return_odometer": 1037,
               "note": "客戶對新品有興趣，下週報價"}
    payload.update(body)
    return env.agent(agent_id).post(f"/api/flows/{flow_id}/events", json=payload)


def _scan(env):
    engine = env.app.state.engine
    with env.session_factory() as db:
        ctx = engine.context(db, actor="controller")
        result = engine.scan(ctx)
        asyncio.run(engine.commit(ctx))
    return result


def _notifications(env, agent_id: str) -> list[dict]:
    return env.agent(agent_id).get("/api/inbox").json()["items"]


def _to_book(env) -> str:
    flow_id = _start(env)["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    _decide(env, SALES, flow_id, "approve")
    return flow_id


def _to_trip(env) -> str:
    flow_id = _to_book(env)
    _report(env, flow_id, "book_vehicle", "admin-agent", BOOKING)
    return flow_id


def _to_claim_approval(env) -> str:
    flow_id = _to_trip(env)
    assert _trip(env, flow_id).status_code == 200
    _report(env, flow_id, "record_trip", "admin-agent",
            {"booking_id": 41, "pickup_odometer": 1000, "return_odometer": 1037, "distance_km": 37})
    _report(env, flow_id, "draft_claim", "finance-agent", CLAIM)
    return flow_id


# ---------------------------------------------------------------- 完整流程


def test_visit_flow_end_to_end(venv):
    env = venv
    flow = _start(env)
    flow_id = flow["id"]
    assert flow["created"] is True
    assert flow["title"] == "10/14 拜訪 客戶 C"
    assert flow["current_step"] == "draft_vehicle"

    detail = env.flow(flow_id)
    resolve = detail["steps"][0]
    assert resolve["name"] == "resolve_parties" and resolve["status"] == "succeeded"
    assert resolve["summary"] == "業務 Mjolmire、主管 Main、財會 Luminous"
    assert detail["state"]["parties"]["manager"]["hrm_employee_id"] == HRM[MANAGER]
    assert detail["state"]["participants"] == [SALES]

    # 步驟 2：派給 admin-agent，指令帶 flow_id、step、冪等鍵、skill、輸入、output 欄位
    job = env.dispatcher.last_job_for("draft_vehicle")
    assert job.agent_id == "admin-agent"
    assert job.skill == "adm-vehicle-draft"
    assert job.idempotency_key == f"{flow_id}:draft_vehicle"
    assert job.input["seats"] == 2
    assert job.input["sales"] == {"hrm_employee_id": HRM[SALES], "display_name": "Mjolmire"}
    assert job.input["exclude_vehicle_ids"] == []
    # 預約時段涵蓋到拜訪當天 23:59：業務還車後才回報里程，ADM 只接受預約時段內登記取車
    assert job.input["booking_start_at"] == "2026-10-14T10:00:00+08:00"
    assert job.input["booking_end_at"] == "2026-10-14T23:59:00+08:00"
    assert "booking_end_at" in job.instruction
    from app.dispatch import build_step_message

    message = build_step_message(job)
    for expected in [f"flow_id: {flow_id}", "step: draft_vehicle", f"Idempotency-Key: {flow_id}:draft_vehicle",
                     "skill: adm-vehicle-draft", "- vehicle_id（int，必填）", "flow-report"]:
        assert expected in message
    resp = env.report(flow_id, "draft_vehicle", "admin-agent", output=DRAFT, attempt=1)
    assert resp.json()["status"] == "waiting_approval"
    assert resp.json()["waiting_for"]["name"] == "Mjolmire"

    # 步驟 3：業務本人（員工 agent）從自己的收件匣確認
    page = _decide(env, SALES, flow_id, "approve")
    assert page["approver_name"] == "Mjolmire"
    assert page["context"]["vehicle"] == "ABC-1234（Altis）"
    assert page["context"]["deadline"] == "10/13 18:00"
    assert "ABC-1234" in page["question"]

    # 步驟 4：代訂，帶業務的 HRM 員工 id
    job = env.dispatcher.last_job_for("book_vehicle")
    assert job.skill == "adm-vehicle-book"
    assert job.input == {"vehicle_id": 2, "start_at": DRAFT["start_at"], "end_at": DRAFT["end_at"],
                         "purpose": DRAFT["purpose"], "destination": DRAFT["destination"],
                         "booked_for_hrm_employee_id": HRM[SALES]}
    assert job.idempotency_key == f"{flow_id}:book_vehicle"
    _report(env, flow_id, "book_vehicle", "admin-agent", BOOKING)

    # 步驟 5：等業務回報；業務 agent 這時看得到流程
    detail = env.flow(flow_id)
    assert detail["current_step"] == "await_trip"
    trip_step = next(s for s in detail["steps"] if s["name"] == "await_trip")
    assert trip_step["assignee"] == SALES and trip_step["status"] == "running"
    assert env.agent(SALES).get(f"/api/flows/{flow_id}").status_code == 200
    resp = _trip(env, flow_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_step"] == "record_trip"
    job = env.dispatcher.last_job_for("record_trip")
    assert job.agent_id == "admin-agent" and job.skill == "adm-vehicle-trip"
    assert job.input == {"booking_id": 41, "pickup_odometer": 1000, "return_odometer": 1037,
                         "condition_notes": "客戶對新品有興趣，下週報價"}
    _report(env, flow_id, "record_trip", "admin-agent",
            {"booking_id": 41, "pickup_odometer": 1000, "return_odometer": 1037, "distance_km": 37})

    # 步驟 6：報帳草稿只存在 controller
    job = env.dispatcher.last_job_for("draft_claim")
    assert job.agent_id == "finance-agent" and job.skill == "mileage-claim-draft"
    assert job.input["distance_km"] == 37
    assert job.input["rate_env"] == "MILEAGE_RATE_TWD_PER_KM"
    _report(env, flow_id, "draft_claim", "finance-agent", CLAIM)
    # 核准前：財會沒有收到任何通知，流程也沒有核准的 claim
    assert _notifications(env, FINANCE) == []
    assert "claim" not in env.flow(flow_id)["state"]

    # 步驟 7：業務主管核准
    page = _decide(env, MANAGER, flow_id, "approve", "OK")
    assert page["context"]["amount_twd"] == 185
    assert "37 km × NT$5/km（demo 假費率） = NT$185" in page["question"]

    # 核准後通知財會（代替 LINE），內容含金額
    notices = _notifications(env, FINANCE)
    assert len(notices) == 1
    assert notices[0]["kind"] == "info"
    assert "NT$185" in notices[0]["body"] and "Main" in notices[0]["body"]
    assert notices[0]["flow_id"] == flow_id

    # 步驟 8：cs-agent 記拜訪互動、完成待辦
    job = env.dispatcher.last_job_for("close_visit")
    assert job.agent_id == "cs-agent" and job.skill == "crm-visit-close"
    assert job.input["follow_up_id"] == 123
    assert job.input["claim_status"] == "approved"
    assert job.input["visit_note"] == "客戶對新品有興趣，下週報價"
    assert "amount_twd" not in job.input
    # C5 拜訪摘要的交通欄位：車牌與起訖里程
    assert job.input["vehicle_code"] == "ABC-1234"
    assert (job.input["pickup_odometer"], job.input["return_odometer"]) == (1000, 1037)
    assert job.input["distance_km"] == 37
    # CRM 的冪等紀錄綁定端點：記互動與完成待辦各用一個鍵
    assert job.write_keys == ("interaction", "complete")
    message = build_step_message(job)
    assert f"Idempotency-Key.interaction: {flow_id}:close_visit:interaction" in message
    assert f"Idempotency-Key.complete: {flow_id}:close_visit:complete" in message
    resp = env.report(flow_id, "close_visit", "cs-agent", output=CLOSE)
    assert resp.json()["status"] == "succeeded"

    detail = env.flow(flow_id)
    assert detail["status"] == "succeeded"
    assert [s["status"] for s in detail["steps"]] == ["succeeded"] * 10
    assert detail["state"]["claim"]["status"] == "approved"
    assert detail["state"]["claim"]["amount_twd"] == 185
    assert [a["decision"] for a in detail["approvals"]] == ["approve", "approve"]
    actions = [entry["action"] for entry in detail["audit"]]
    for expected in ["flow.started", "step.completed", "approval.decided", "event.received", "flow.succeeded"]:
        assert expected in actions
    # 每個 agent 步驟只派一次，冪等鍵都是 {flow_id}:{step}
    for job in env.dispatcher.step_jobs:
        assert job.idempotency_key == f"{flow_id}:{job.step}"
        assert job.session_id == f"wf-{flow_id}-{job.step}-{job.attempt}"


def test_dedupe_by_follow_up_id(venv):
    first = _start(venv)
    resp = venv.agent("cs-agent").post(
        "/api/flows", json={"type": "visit", "input": _visit_input(), "dedupe_key": "something-else"})
    assert resp.status_code == 200
    assert resp.json()["id"] == first["id"]
    assert resp.json()["created"] is False
    other = _start(venv, follow_up_id="124")
    assert other["id"] != first["id"]
    assert len([j for j in venv.dispatcher.step_jobs if j.step == "draft_vehicle"]) == 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"follow_up_id": None}, "follow_up_id"),
        ({"customer": None}, "customer"),
        ({"customer": {"external_id": "C-1"}}, "customer.name"),
        ({"visit_start_at": "not-a-date"}, "visit_start_at"),
        ({"visit_end_at": "2026-10-14T09:00:00+08:00"}, "晚於"),
        ({"seats": 0}, "seats"),
        ({"seats": True}, "seats"),
        ({"location": ""}, "location"),
        ({"assignee_hrm_employee_id": ""}, "assignee_hrm_employee_id"),
    ],
)
def test_invalid_input(venv, overrides, message):
    resp = venv.agent("cs-agent").post("/api/flows", json={"type": "visit", "input": _visit_input(**overrides)})
    assert resp.status_code == 422
    assert message in resp.json()["detail"]["message"]


def test_naive_times_are_taipei_and_only_cs_agent_starts():
    clean = visit.validate_input(_visit_input(visit_start_at="2026-10-14T10:00:00",
                                              visit_end_at="2026-10-14T12:00:00", follow_up_id="77"))
    assert clean["visit_start_at"] == "2026-10-14T10:00:00+08:00"
    assert clean["follow_up_id"] == 77
    assert visit.DEFINITION.starters == ("cs-agent",)


def test_only_cs_agent_can_start(venv):
    resp = venv.agent("admin-agent").post("/api/flows", json={"type": "visit", "input": _visit_input()})
    assert resp.status_code == 403
    resp = venv.agent(SALES).post("/api/flows", json={"type": "visit", "input": _visit_input()})
    assert resp.status_code == 403


def test_missing_output_fields_rejected(venv):
    flow_id = _start(venv)["id"]
    resp = venv.report(flow_id, "draft_vehicle", "admin-agent", output={"vehicle_id": 2})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "missing_output_fields"
    assert "vehicle_code" in resp.json()["detail"]["message"]
    # 被拒的回報不改變狀態，補齊後可以再送
    assert venv.flow(flow_id)["steps"][1]["status"] == "running"
    assert venv.report(flow_id, "draft_vehicle", "admin-agent", output=DRAFT).status_code == 200


# ---------------------------------------------------------------- 409 → 回步驟 2，最多 2 次


def _conflict(env, flow_id: str, vehicle_id: int):
    resp = env.report(flow_id, "book_vehicle", "admin-agent", status="failed",
                      error="ADM 回 409 時段衝突", error_kind="business",
                      output={"conflict": True, "http_status": 409, "vehicle_id": vehicle_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_book_conflict_goes_back_to_draft_at_most_twice(venv):
    env = venv
    flow_id = _to_book(env)
    approvals_before = len(env.flow(flow_id)["approvals"])

    for round_, vehicle in enumerate([2, 3], start=1):
        summary = _conflict(env, flow_id, vehicle)
        assert summary["current_step"] == "draft_vehicle", round_
        job = env.dispatcher.last_job_for("draft_vehicle")
        # 累積排除所有衝突過的車（第 2 次重查時 2、3 都要排除，不能只排除最後一台）
        assert job.input["exclude_vehicle_ids"] == list(range(2, vehicle + 1))
        assert "時段衝突" in job.instruction
        # 重查後直接預約，不再請業務確認
        summary = _report(env, flow_id, "draft_vehicle", "admin-agent", {**DRAFT, "vehicle_id": vehicle + 1})
        assert summary["current_step"] == "book_vehicle"
        assert env.dispatcher.last_job_for("book_vehicle").input["vehicle_id"] == vehicle + 1

    assert len(env.flow(flow_id)["approvals"]) == approvals_before

    # 第 3 次 409：停下來交給人
    _conflict(env, flow_id, 4)
    detail = env.flow(flow_id)
    assert detail["status"] == "running"
    assert detail["current_step"] == "book_vehicle"
    book = next(s for s in detail["steps"] if s["name"] == "book_vehicle")
    assert book["status"] == "failed" and book["failures"] == 3
    assert detail["stuck"]["code"] == "retries_exhausted"
    assert detail["stuck"]["reason"] == "ADM 回 409 時段衝突，已重試 2 次"
    # 三次預約派工都用同一個冪等鍵
    keys = {j.idempotency_key for j in env.dispatcher.step_jobs if j.step == "book_vehicle"}
    assert keys == {f"{flow_id}:book_vehicle"}
    attempts = [j.attempt for j in env.dispatcher.step_jobs if j.step == "book_vehicle"]
    assert attempts == [1, 2, 3]


def test_book_system_error_retries_same_step(venv):
    flow_id = _to_book(venv)
    resp = venv.report(flow_id, "book_vehicle", "admin-agent", status="failed",
                       error="ADM 無法連線", error_kind="system")
    assert resp.json()["current_step"] == "book_vehicle"
    assert [j.step for j in venv.dispatcher.step_jobs][-2:] == ["book_vehicle", "book_vehicle"]


def test_non_conflict_failure_after_conflict_retries_same_step(venv):
    """上一次 409 的 output 不會被沿用：這次沒有 conflict 就重試同一步，不回去重查。"""
    flow_id = _to_book(venv)
    _conflict(venv, flow_id, 2)
    _report(venv, flow_id, "draft_vehicle", "admin-agent", {**DRAFT, "vehicle_id": 3})
    resp = venv.report(flow_id, "book_vehicle", "admin-agent", status="failed", error="ADM 回 400",
                       error_kind="business")
    assert resp.json()["current_step"] == "book_vehicle"
    assert venv.flow(flow_id)["state"]["failures"]["book_vehicle"]["output"] == {}


def test_retry_keeps_idempotency_key(venv):
    """同一步驟重試（自動或 admin 按重試）Idempotency-Key 都不變。"""
    env = venv
    flow_id = _to_book(env)
    env.report(flow_id, "book_vehicle", "admin-agent", status="needs_human", summary="ADM 冪等鍵衝突")
    detail = env.flow(flow_id)
    assert detail["stuck"]["code"] == "retries_exhausted"
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/book_vehicle/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    jobs = [j for j in env.dispatcher.step_jobs if j.step == "book_vehicle"]
    assert len(jobs) == 2
    assert jobs[0].idempotency_key == jobs[1].idempotency_key == f"{flow_id}:book_vehicle"
    assert jobs[0].session_id != jobs[1].session_id
    from app.dispatch import build_step_message

    assert f"Idempotency-Key: {flow_id}:book_vehicle" in build_step_message(jobs[1]).splitlines()


# ---------------------------------------------------------------- 派車確認的分支


def test_confirm_timeout_expires_and_tells_sales_to_book(venv, clock):
    env = venv
    flow_id = _start(env)["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    detail = env.flow(flow_id)
    confirm = next(s for s in detail["steps"] if s["name"] == "confirm_vehicle")
    assert confirm["deadline_at"] == "2026-10-13T10:00:00Z"  # 台北 10/13 18:00
    # 一次性連結預設 24 小時有效（比截止時間早到期時，admin 可以重送）
    assert detail["approvals"][0]["expires_at"] == "2026-10-13T02:00:00Z"

    clock.advance(hours=31, minutes=59)  # 10/13 17:59
    assert _scan(env)["expired"] == 0
    clock.advance(minutes=1)  # 10/13 18:00
    env.clients.pop("user:admin", None)
    assert _scan(env)["expired"] == 1
    detail = env.flow(flow_id)
    assert detail["status"] == "expired"
    assert detail["approvals"][0]["status"] == "expired"
    assert all(s["status"] == "skipped" for s in detail["steps"][3:])
    notices = [n for n in _notifications(env, SALES) if n["kind"] == "approval_expired"]
    assert len(notices) == 1
    assert "自行到 ADM 預約" in notices[0]["body"]
    assert not any(j.step == "book_vehicle" for j in env.dispatcher.step_jobs)


def test_confirm_revise_and_reject(venv):
    env = venv
    flow_id = _start(env)["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    _decide(env, SALES, flow_id, "revise", "要 7 人座")
    job = env.dispatcher.last_job_for("draft_vehicle")
    assert job.attempt == 2
    assert job.input["sales_comment"] == "要 7 人座"
    _report(env, flow_id, "draft_vehicle", "admin-agent", {**DRAFT, "vehicle_id": 5, "seats": 7})
    assert env.flow(flow_id)["current_step"] == "confirm_vehicle"
    _decide(env, SALES, flow_id, "reject", "自己開車")
    detail = env.flow(flow_id)
    assert detail["status"] == "succeeded"
    assert not any(j.step == "book_vehicle" for j in env.dispatcher.step_jobs)


# ---------------------------------------------------------------- 步驟 5：trip_report


def test_trip_report_only_from_sales_agent(venv):
    env = venv
    flow_id = _start(env)["id"]
    # 還沒到等回報的步驟：業務本人送也不收
    resp = _trip(env, flow_id)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "step_not_current"

    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    _decide(env, SALES, flow_id, "approve")
    _report(env, flow_id, "book_vehicle", "admin-agent", BOOKING)

    for other in ["gobta", MANAGER, "cs-agent", "admin-agent"]:
        resp = _trip(env, flow_id, agent_id=other)
        assert resp.status_code == 403, other
        assert resp.json()["detail"]["code"] == "not_reporter"
    # 前端帳號不能送（只收 agent token）
    assert env.user("admin").post(f"/api/flows/{flow_id}/events",
                                  json={"type": "trip_report"}).status_code == 401
    # 也不能用 flow-report 跳過事件
    resp = env.report(flow_id, "await_trip", SALES, output={"distance_km": 1})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "use_events"
    # 內容驗證
    assert _trip(env, flow_id, pickup_odometer=1100).status_code == 422
    assert _trip(env, flow_id, pickup_odometer="1000").status_code == 422
    assert _trip(env, flow_id, return_odometer=999999).status_code == 422
    resp = env.agent(SALES).post(f"/api/flows/{flow_id}/events", json={"type": "other"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_event"
    assert env.flow(flow_id)["current_step"] == "await_trip"

    resp = _trip(env, flow_id)
    assert resp.status_code == 200
    trip_step = next(s for s in env.flow(flow_id)["steps"] if s["name"] == "await_trip")
    assert trip_step["summary"] == "取車 1000 km、還車 1037 km，里程差 37 km"
    # 重送不會再推進
    assert _trip(env, flow_id).status_code == 409


def test_trip_report_deadline_marks_stuck(venv, clock):
    env = venv
    flow_id = _to_trip(env)
    step = next(s for s in env.flow(flow_id)["steps"] if s["name"] == "await_trip")
    assert step["deadline_at"] == "2026-10-14T15:59:00Z"  # 台北 10/14 23:59
    clock.advance(days=2, hours=14)  # 10/15 00:00
    env.clients.pop("user:admin", None)  # 登入 session 12 小時，重新登入
    assert _scan(env)["expired"] == 0  # 只標卡住，不結束流程
    detail = env.flow(flow_id)
    assert detail["status"] == "running"
    assert detail["stuck"]["code"] == "agent_no_report"
    assert "等業務回報取車還車里程" in detail["stuck"]["reason"]
    assert SALES in detail["stuck"]["reason"]
    # 晚一點回報仍然可以接手
    assert _trip(env, flow_id).status_code == 200


# ---------------------------------------------------------------- 報帳的分支


def test_claim_reject_skips_finance_and_closes_visit(venv):
    env = venv
    flow_id = _to_claim_approval(env)
    _decide(env, MANAGER, flow_id, "reject", "不符規定")
    assert _notifications(env, FINANCE) == []
    job = env.dispatcher.last_job_for("close_visit")
    assert job.input["claim_status"] == "rejected"
    detail = env.flow(flow_id)
    notify = next(s for s in detail["steps"] if s["name"] == "notify_finance")
    assert notify["status"] == "pending"
    env.report(flow_id, "close_visit", "cs-agent", output=CLOSE)
    detail = env.flow(flow_id)
    assert detail["status"] == "succeeded"
    assert "claim" not in detail["state"]
    assert next(s for s in detail["steps"] if s["name"] == "notify_finance")["status"] == "skipped"


def test_claim_revise_redrafts(venv):
    env = venv
    flow_id = _to_claim_approval(env)
    _decide(env, MANAGER, flow_id, "revise", "里程要扣掉私人行程 7 km")
    job = env.dispatcher.last_job_for("draft_claim")
    assert job.attempt == 2
    assert job.input["manager_comment"] == "里程要扣掉私人行程 7 km"
    _report(env, flow_id, "draft_claim", "finance-agent", {**CLAIM, "distance_km": 30, "amount_twd": 150})
    _decide(env, MANAGER, flow_id, "approve")
    notices = _notifications(env, FINANCE)
    assert len(notices) == 1 and "NT$150" in notices[0]["body"]


# ---------------------------------------------------------------- 找不到人：停下並通知 admin


def test_missing_manager_stops_and_notifies_admin(env):
    # 只匯入業務與財會：業務部沒有主管
    _load_identities(env, [r for r in RECORDS if r["agent_id"] != MANAGER])
    with env.session_factory() as db:
        ops = Identity(hrm_employee_id="ops-1", display_name="值班管理員", role="")
        db.add(ops)
        db.flush()
        admin = db.scalar(select(User).where(User.username == "admin"))
        admin.identity_id = ops.id
        db.commit()

    flow_id = _start(env)["id"]
    detail = env.flow(flow_id)
    assert detail["status"] == "running"
    assert detail["current_step"] == "resolve_parties"
    assert detail["steps"][0]["status"] == "failed"
    assert detail["stuck"]["code"] == "system_error"
    assert "找不到業務部的業務主管" in detail["stuck"]["reason"]
    assert env.dispatcher.step_jobs == []  # 沒有猜一個人就往下走

    inbox = env.user("admin").get("/api/inbox", params={"recipient": "ops-1"}).json()
    alerts = [n for n in inbox["items"] if n["kind"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["flow_id"] == flow_id
    assert "找不到業務部的業務主管" in alerts[0]["body"]

    # 補上主管後，admin 按重試就繼續
    with env.session_factory() as db:
        import_identities(db, [r for r in RECORDS if r["agent_id"] == MANAGER])
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/resolve_parties/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_step"] == "draft_vehicle"


def test_ambiguous_manager_and_missing_sales_stop(env):
    extra = {"hrm_employee_id": "hrm-x", "employee_number": "X1", "display_name": "另一位主管",
             "role": "manager", "department": "業務部"}
    _load_identities(env, [*RECORDS, extra])
    flow_id = _start(env)["id"]
    stuck = env.flow(flow_id)["stuck"]
    assert stuck["code"] == "system_error"
    assert "有 2 位" in stuck["reason"]

    flow_id = _start(env, follow_up_id=999, assignee_hrm_employee_id="nobody")["id"]
    assert "找不到負責業務" in env.flow(flow_id)["stuck"]["reason"]


# ---------------------------------------------------------------- 卡住：agent 不回報


def test_agent_no_report_is_stuck(venv, clock):
    env = venv
    flow_id = _start(env)["id"]
    clock.advance(minutes=4)
    assert env.flow(flow_id)["stuck"] is None
    clock.advance(minutes=3)
    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["counts"]["stuck"] == 1
    stuck = summary["stuck"][0]
    assert stuck["id"] == flow_id
    assert stuck["current_step"] == "draft_vehicle"
    assert stuck["waiting_for"]["id"] == "admin-agent"
    assert stuck["stuck"] == {
        "code": "agent_no_report",
        "reason": "admin-agent 已 7 分鐘沒有回報",
        "step": "draft_vehicle",
        "waited_seconds": 420,
    }


def test_waiting_for_sales_confirm_too_long(venv, clock):
    env = venv
    flow_id = _start(env)["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    # 10:00 開始等，截止 10/13 18:00（32 小時），過一半（16 小時）算等太久
    clock.advance(hours=16)
    stuck = env.flow(flow_id)["stuck"]
    assert stuck["code"] == "approval_waiting_too_long"
    assert stuck["reason"] == "等Mjolmire確認派車，已 16 小時，18:00 截止"


# ---------------------------------------------------------------- 員工 agent 的收件匣


def test_employee_agent_inbox_scope(venv):
    env = venv
    flow_id = _start(env)["id"]
    _report(env, flow_id, "draft_vehicle", "admin-agent", DRAFT)
    inbox = env.agent(SALES).get("/api/inbox").json()
    assert inbox["recipient"]["hrm_employee_id"] == HRM[SALES]
    assert inbox["recipient"]["department"] == "業務部"
    assert inbox["items"][0]["link"] is not None
    # 讀不到別人的收件匣
    resp = env.agent(SALES).get("/api/inbox", params={"recipient": HRM[MANAGER]})
    assert resp.status_code == 403
    assert env.agent(MANAGER).get("/api/inbox").json()["items"] == []
    # 員工 agent 看不到沒參與的流程
    assert env.agent("gobta").get(f"/api/flows/{flow_id}").status_code == 403


@pytest.mark.parametrize(
    ("start", "end", "booking_end"),
    [
        ("2026-10-14T10:00:00+08:00", "2026-10-14T12:00:00+08:00", "2026-10-14T23:59:00+08:00"),
        # 拜訪跨過午夜：預約至少到拜訪結束
        ("2026-10-14T22:00:00+08:00", "2026-10-15T01:00:00+08:00", "2026-10-15T01:00:00+08:00"),
    ],
)
def test_booking_window_covers_trip_report(start, end, booking_end):
    state = {"input": visit.validate_input(_visit_input(visit_start_at=start, visit_end_at=end))}
    booking_start, booking_stop = visit.booking_window(state)
    assert booking_start.isoformat() == start
    assert booking_stop.isoformat() == booking_end



# ---------------------------------------------------------------- ① 修整（批次 4 審查）


def test_exclude_vehicle_ids_accumulate_and_ignore_non_conflicts(venv):
    """排除清單累積所有 409 衝突過的車；非衝突的失敗（例如連線失敗）不會把車加進去。"""
    env = venv
    flow_id = _to_book(env)
    _conflict(env, flow_id, 2)
    _report(env, flow_id, "draft_vehicle", "admin-agent", {**DRAFT, "vehicle_id": 3})
    # 連線失敗：重試同一步，不加入排除清單
    env.report(flow_id, "book_vehicle", "admin-agent", status="failed", error="ADM 無法連線",
               error_kind="system", output={"vehicle_id": 3})
    # admin 按重試（失敗次數歸零），衝突紀錄仍然保留
    detail = env.flow(flow_id)
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/book_vehicle/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    _conflict(env, flow_id, 3)
    job = env.dispatcher.last_job_for("draft_vehicle")
    assert job.input["exclude_vehicle_ids"] == [2, 3]
    history = env.flow(flow_id)["state"]["failures"]["book_vehicle"]["history"]
    assert [h["error_kind"] for h in history] == ["business", "system", "business"]


def test_conflict_vehicle_ids_reads_legacy_state_without_history():
    state = {"failures": {"book_vehicle": {"count": 1, "error_kind": "business",
                                           "output": {"conflict": True, "vehicle_id": 7}}}}
    assert visit.conflict_vehicle_ids(state) == [7]
    assert visit.conflict_vehicle_ids({}) == []


def test_close_outputs_are_uuid_strings():
    fields = {f.name: f for f in visit.CLOSE_OUTPUTS}
    assert fields["interaction_id"].type == "str" and "UUID" in fields["interaction_id"].description
    assert fields["follow_up_id"].type == "str" and "UUID" in fields["follow_up_id"].description
    doc = (Path(__file__).resolve().parents[1] / "docs" / "flows" / "visit.md").read_text(encoding="utf-8")
    assert "| `interaction_id` | str | ✔ | CRM 互動紀錄 id（UUID 字串） |" in doc
    assert "| `follow_up_id` | str | ✔ | CRM 待辦跟進 id（UUID 字串） |" in doc
    for name in ("vehicle_code", "pickup_odometer", "return_odometer"):
        assert name in doc


def test_finance_notice_is_composed_by_controller(venv):
    """財會通知由 controller 自組，不引用草稿的 description（避免「草稿・待主管核准」和「已核准」並列）。"""
    env = venv
    flow_id = _to_book(env)
    _report(env, flow_id, "book_vehicle", "admin-agent", BOOKING)
    _trip(env, flow_id)
    _report(env, flow_id, "record_trip", "admin-agent",
            {"booking_id": 41, "pickup_odometer": 1000, "return_odometer": 1037, "distance_km": 37})
    _report(env, flow_id, "draft_claim", "finance-agent",
            {**CLAIM, "description": "【草稿・待主管核准】10/14 拜訪客戶 C，37 km"})
    _decide(env, MANAGER, flow_id, "approve", "OK")
    [notice] = _notifications(env, FINANCE)
    assert "草稿" not in notice["body"] and "待主管核准" not in notice["body"]
    assert "已由 Main 核准" in notice["body"]
    assert "NT$185" in notice["body"] and "ABC-1234" in notice["body"] and "#41" in notice["body"]
    assert flow_id in notice["body"]
