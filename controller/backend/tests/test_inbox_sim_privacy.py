"""通知收件匣、模擬入口、個資過濾。"""

from __future__ import annotations

from sqlalchemy import select

from app.dispatch import MessageJob
from app.models import Approval, AuditLog, Identity, User
from app.sanitize import sanitize
from tests.conftest import APPROVER


def _to_approval(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={"draft": "v1"})
    return flow_id


# ---------------------------------------------------------------- 收件匣


def test_inbox_lists_approval_request_with_link_for_admin(env):
    flow_id = _to_approval(env)
    inbox = env.user("admin").get("/api/inbox", params={"recipient": APPROVER}).json()
    assert inbox["recipient"]["display_name"] == "王經理"
    assert inbox["unread_count"] == 1
    item = inbox["items"][0]
    assert item["kind"] == "approval_request"
    assert item["flow_id"] == flow_id
    assert item["title"] == "待確認：示範：測試主題"
    assert item["link"].startswith("http://controller.test/approvals/ap_")
    assert "token=" in item["link"]
    # 也可以用員工編號或 Identity id 查
    assert env.user("admin").get("/api/inbox", params={"recipient": "E001"}).json()["unread_count"] == 1


def test_viewer_sees_inbox_without_links(env):
    _to_approval(env)
    inbox = env.user("viewer").get("/api/inbox", params={"recipient": APPROVER}).json()
    assert inbox["items"][0]["link"] is None
    assert inbox["items"][0]["link_hidden"] is True


def test_user_linked_to_identity_sees_own_links(env):
    _to_approval(env)
    with env.session_factory() as db:
        viewer = db.scalar(select(User).where(User.username == "viewer"))
        viewer.identity_id = db.scalar(select(Identity.id).where(Identity.hrm_employee_id == APPROVER))
        db.commit()
    inbox = env.user("viewer").get("/api/inbox").json()  # 不帶 recipient：看自己的
    assert inbox["items"][0]["link"] is not None


def test_employee_agent_reads_own_inbox_only(env):
    _to_approval(env)
    # 員工 agent main 扮演王經理（Identity.agent_id = main）
    inbox = env.agent("main").get("/api/inbox").json()
    assert inbox["recipient"]["hrm_employee_id"] == APPROVER
    assert inbox["items"][0]["link"] is not None
    resp = env.agent("main").get("/api/inbox", params={"recipient": "hrm-emp-002"})
    assert resp.status_code == 403
    # 沒有對應 Identity 的 agent
    assert env.agent("cs-agent").get("/api/inbox").status_code == 404


def test_mark_read_and_read_all(env):
    _to_approval(env)
    admin = env.user("admin")
    item = admin.get("/api/inbox", params={"recipient": APPROVER}).json()["items"][0]
    resp = admin.post(f"/api/inbox/{item['id']}/read")
    assert resp.status_code == 200
    assert resp.json()["read_at"] is not None
    assert admin.get("/api/inbox", params={"recipient": APPROVER}).json()["unread_count"] == 0
    assert admin.post("/api/inbox/99999/read").status_code == 404

    # 再造一則
    flow = env.flow(admin.get("/api/inbox", params={"recipient": APPROVER}).json()["items"][0]["flow_id"])
    admin.post(f"/api/approvals/{flow['approvals'][0]['id']}/resend", json={"revision": flow["revision"]})
    unread = admin.get("/api/inbox", params={"recipient": APPROVER, "unread_only": "true"}).json()
    assert len(unread["items"]) == 1
    assert admin.post("/api/inbox/read-all", params={"recipient": APPROVER}).json() == {"marked": 1}

    # 員工 agent 不能標別人的通知
    other = env.agent("main").post(f"/api/inbox/{item['id']}/read")
    assert other.status_code == 200  # 自己的
    recipients = admin.get("/api/inbox/recipients").json()["items"]
    assert {r["hrm_employee_id"] for r in recipients} == {APPROVER, "hrm-emp-002"}


# ---------------------------------------------------------------- 收件匣顯示確認的狀態


def _approval_items(env):
    items = env.user("admin").get("/api/inbox", params={"recipient": APPROVER}).json()["items"]
    return [item for item in items if item["approval_id"]]


def test_inbox_shows_approval_status_pending_then_decided(env):
    _to_approval(env)
    [item] = _approval_items(env)
    assert item["approval_status"] == "pending"
    assert item["approval_decision"] is None
    approval_id, token = env.latest_approval_link()
    env.anon.post(f"/api/approvals/{approval_id}/decision", json={"token": token, "decision": "approve"})
    [item] = _approval_items(env)
    assert item["approval_status"] == "decided"
    assert item["approval_decision"] == "核准"


def test_inbox_marks_old_link_superseded_after_resend(env):
    flow_id = _to_approval(env)
    detail = env.flow(flow_id)
    approval_id = detail["approvals"][0]["id"]
    resp = env.user("admin").post(f"/api/approvals/{approval_id}/resend", json={"revision": detail["revision"]})
    assert resp.status_code == 200
    # 新通知在前（pending），舊通知的連結已失效（superseded）
    assert [item["approval_status"] for item in _approval_items(env)] == ["pending", "superseded"]


def test_inbox_shows_link_expired_and_cancelled(env, clock):
    flow_id = _to_approval(env)
    # 連結過期但確認還沒結束（逾時處理為 stuck 的確認）
    with env.session_factory() as db:
        approval = db.scalar(select(Approval).where(Approval.flow_id == flow_id))
        approval.expires_at = clock()
        db.commit()
    assert _approval_items(env)[0]["approval_status"] == "link_expired"
    detail = env.flow(flow_id)
    env.user("admin").post(f"/api/flows/{flow_id}/cancel", json={"revision": detail["revision"]})
    assert _approval_items(env)[0]["approval_status"] == "cancelled"
    # 非確認通知沒有狀態
    items = env.user("admin").get("/api/inbox", params={"recipient": APPROVER}).json()["items"]
    assert all(item["approval_status"] is None for item in items if not item["approval_id"])


def test_inbox_requires_auth_and_valid_recipient(env):
    assert env.anon.get("/api/inbox", params={"recipient": APPROVER}).status_code == 401
    assert env.user("admin").get("/api/inbox", params={"recipient": "nobody"}).status_code == 404
    assert env.user("admin").get("/api/inbox").status_code == 404  # admin 沒對應 Identity


# ---------------------------------------------------------------- 模擬入口


def test_sim_message_dispatches_and_audits(env):
    resp = env.agent("milim").post(
        "/api/sim/messages",
        json={"target_agent": "cs-agent",
              "persona": {"external_customer_id": "C-007", "display_name": "陳小姐"},
              "message": "上週買的商品壞了，我要退款"},
    )
    assert resp.status_code == 202, resp.text
    job = env.dispatcher.jobs[-1]
    assert isinstance(job, MessageJob)
    assert job.agent_id == "cs-agent"
    assert "external_customer_id: C-007" in job.message
    assert "simulated: true" in job.message
    with env.session_factory() as db:
        actions = [(a.actor, a.action) for a in db.scalars(select(AuditLog))]
    assert ("agent:milim", "sim.message") in actions
    assert ("controller", "sim.dispatch_ok") in actions


def test_sim_message_rejects_non_simulator_and_bad_target(env):
    body = {"persona": {"external_customer_id": "C-1", "display_name": "x"}, "message": "hi"}
    assert env.agent("cs-agent").post("/api/sim/messages", json=body).status_code == 403
    assert env.user("admin").post("/api/sim/messages", json=body).status_code == 401
    resp = env.agent("milim").post("/api/sim/messages", json={**body, "target_agent": "finance-agent"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "target_not_allowed"


def test_sim_message_target_missing(env):
    env.app.state.settings.sim_targets.append("ghost-agent")
    body = {"target_agent": "ghost-agent",
            "persona": {"external_customer_id": "C-1", "display_name": "x"}, "message": "hi"}
    resp = env.agent("milim").post("/api/sim/messages", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "target_not_found"


# ---------------------------------------------------------------- 個資


def test_api_responses_strip_phone_and_address(env):
    flow_id = env.start_demo()["id"]
    env.report(
        flow_id, "draft", "admin-agent",
        output={"draft": "v1", "customer": {"name": "陳小姐", "phone": "0912-000-000",
                                            "mobile_phone": "0912", "address": "台北市",
                                            "contacts": [{"tel": "02-1234", "name": "x"}],
                                            "地址": "某處"}},
    )
    detail_text = env.user("admin").get(f"/api/flows/{flow_id}").text
    for secret in ("0912", "台北市", "02-1234", "某處"):
        assert secret not in detail_text
    assert "陳小姐" in detail_text
    approval_id, token = env.latest_approval_link()
    page = env.anon.get(f"/api/approvals/{approval_id}", params={"token": token}).text
    assert "0912" not in page


def test_identity_has_no_phone_and_import_ignores_it(env):
    assert not any("phone" in c.name or "address" in c.name for c in Identity.__table__.columns)
    inbox = env.user("admin").get("/api/inbox", params={"recipient": APPROVER}).text
    assert "0912-345-678" not in inbox


def test_sanitize_keeps_other_values():
    data = {"name": "a", "items": [{"telephone": "1", "ok": 2}], "tuple": ("x",), "Address_Line": "y"}
    assert sanitize(data) == {"name": "a", "items": [{"ok": 2}], "tuple": ["x"]}
