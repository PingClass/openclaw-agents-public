"""revision 防過期寫入，與前端操作的權限。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm.exc import StaleDataError

from app.models import Flow


def test_cancel_with_stale_revision_is_rejected(env):
    flow = env.start_demo()
    flow_id = flow["id"]
    stale = flow["revision"]
    # 流程往前走，revision 變了
    env.report(flow_id, "draft", "admin-agent", output={})
    current = env.flow(flow_id)["revision"]
    assert current > stale

    resp = env.user("admin").post(f"/api/flows/{flow_id}/cancel", json={"revision": stale})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revision_conflict"
    assert env.flow(flow_id)["status"] == "waiting_approval"

    resp = env.user("admin").post(f"/api/flows/{flow_id}/cancel",
                                  json={"revision": current, "reason": "測試取消"})
    assert resp.status_code == 200, resp.text
    detail = env.flow(flow_id)
    assert detail["status"] == "cancelled"
    assert detail["approvals"][0]["status"] == "cancelled"
    cancel_log = next(e for e in detail["audit"] if e["action"] == "flow.cancelled")
    assert cancel_log["actor"] == "user:admin"
    assert cancel_log["detail"]["reason"] == "測試取消"

    # 已取消的流程不能再取消
    resp = env.user("admin").post(f"/api/flows/{flow_id}/cancel", json={"revision": detail["revision"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "flow_finished"


def test_step_report_with_stale_revision_is_rejected(env):
    flow = env.start_demo()
    resp = env.report(flow["id"], "draft", "admin-agent", revision=flow["revision"] - 1)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revision_conflict"


def test_concurrent_writes_compare_and_swap(env):
    """兩個交易同時更新同一條流程：後提交的被 version_id_col 拒絕。"""
    flow_id = env.start_demo()["id"]
    first = env.session_factory()
    second = env.session_factory()
    try:
        a = first.get(Flow, flow_id)
        b = second.get(Flow, flow_id)
        a.title = "A"
        first.commit()
        b.title = "B"
        with pytest.raises(StaleDataError):
            second.commit()
    finally:
        first.close()
        second.close()


def test_viewer_cannot_retry_cancel_or_resend(env):
    flow = env.start_demo()
    flow_id = flow["id"]
    viewer = env.user("viewer")
    revision = env.flow(flow_id)["revision"]
    assert viewer.post(f"/api/flows/{flow_id}/cancel", json={"revision": revision}).status_code == 403
    assert viewer.post(f"/api/flows/{flow_id}/steps/draft/retry",
                       json={"revision": revision}).status_code == 403
    env.report(flow_id, "draft", "admin-agent", output={})
    approval_id = env.flow(flow_id)["approvals"][0]["id"]
    revision = env.flow(flow_id)["revision"]
    assert viewer.post(f"/api/approvals/{approval_id}/resend",
                       json={"revision": revision}).status_code == 403
    # viewer 可以看
    assert viewer.get(f"/api/flows/{flow_id}").status_code == 200
    assert viewer.get("/api/flows").status_code == 200
    # agent token 也不能做人的操作
    resp = env.agent("cs-agent").post(f"/api/flows/{flow_id}/cancel", json={"revision": revision})
    assert resp.status_code == 401
    # 沒登入
    assert env.anon.post(f"/api/flows/{flow_id}/cancel", json={"revision": revision}).status_code == 401
    assert env.anon.get("/api/flows").status_code == 401
    assert env.anon.get(f"/api/flows/{flow_id}").status_code == 401


def test_admin_retry_redispatches_with_same_idempotency_key(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", status="failed", error="x")
    env.report(flow_id, "draft", "admin-agent", status="failed", error="y")
    detail = env.flow(flow_id)
    assert detail["stuck"]["code"] == "retries_exhausted"

    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/draft/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 200, resp.text
    detail = env.flow(flow_id)
    draft = detail["steps"][0]
    assert draft["status"] == "running"
    assert draft["attempts"] == 3
    assert draft["failures"] == 0
    assert detail["stuck"] is None
    assert env.dispatcher.last_job_for("draft").idempotency_key == f"{flow_id}:draft"
    assert any(e["action"] == "step.retry" and e["actor"] == "user:admin" for e in detail["audit"])

    # 只能重試目前步驟；人工確認要用重送
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/finalize/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 409
    env.report(flow_id, "draft", "admin-agent", output={})
    detail = env.flow(flow_id)
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/confirm/retry",
                                  json={"revision": detail["revision"]})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "use_resend"


def test_retry_requires_revision(env):
    flow_id = env.start_demo()["id"]
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/draft/retry", json={})
    assert resp.status_code == 422


def test_login_logout_and_me(env):
    client = env.user("admin")
    me = client.get("/api/auth/me").json()
    assert me["role"] == "admin"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    env.clients.pop("user:admin")

    resp = env.anon.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    resp = env.anon.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert resp.status_code == 401


def test_session_expires(env, clock):
    client = env.user("viewer")
    assert client.get("/api/auth/me").status_code == 200
    clock.advance(hours=13)
    assert client.get("/api/auth/me").status_code == 401


def test_invalid_and_revoked_agent_token(env):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models import AgentToken

    from fastapi.testclient import TestClient

    bad = TestClient(env.app)
    bad.headers["Authorization"] = "Bearer fca_not-a-token"
    assert bad.get("/api/flows/fl_x").status_code == 401

    with env.session_factory() as db:
        for token in db.scalars(select(AgentToken).where(AgentToken.agent_id == "cs-agent")):
            token.revoked_at = datetime.now(UTC)
        db.commit()
    resp = env.agent("cs-agent").post("/api/flows", json={"type": "demo", "input": {}})
    assert resp.status_code == 401
