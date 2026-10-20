"""一次性確認 token：只存雜湊、有期限、用過即失效、重送後舊連結失效。"""

from __future__ import annotations

from app.models import Approval, Notification
from app.security import sha256_hex


def _to_approval(env):
    flow = env.start_demo()
    env.report(flow["id"], "draft", "admin-agent", output={"draft": "v1"})
    approval_id, token = env.latest_approval_link()
    return flow["id"], approval_id, token


def test_token_is_stored_only_as_hash(env):
    flow_id, approval_id, token = _to_approval(env)
    with env.session_factory() as db:
        approval = db.get(Approval, approval_id)
        assert approval.token_hash == sha256_hex(token)
        assert token not in str(approval.__dict__)
    # 流程詳情、確認頁都不會回傳 token 或雜湊
    for text in (
        env.user("admin").get(f"/api/flows/{flow_id}").text,
        env.anon.get(f"/api/approvals/{approval_id}", params={"token": token}).text,
    ):
        assert sha256_hex(token) not in text
        assert token not in text


def test_token_used_once(env):
    flow_id, approval_id, token = _to_approval(env)
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "approve"})
    assert resp.status_code == 200
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "reject"})
    assert resp.status_code == 410
    assert resp.json()["detail"]["code"] == "token_used"
    assert env.anon.get(f"/api/approvals/{approval_id}", params={"token": token}).status_code == 410
    assert env.flow(flow_id)["current_step"] == "finalize"


def test_token_expires(env, clock):
    _, approval_id, token = _to_approval(env)
    # demo 的確認時限是 4 小時，比預設 24 小時的連結期限短，所以連結 4 小時後失效
    with env.session_factory() as db:
        approval = db.get(Approval, approval_id)
        assert (approval.expires_at - clock.now).total_seconds() == 4 * 3600
    clock.advance(hours=4, seconds=1)
    resp = env.anon.get(f"/api/approvals/{approval_id}", params={"token": token})
    assert resp.status_code == 410
    assert resp.json()["detail"]["code"] == "token_expired"
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "approve"})
    assert resp.status_code == 410


def test_wrong_token_and_missing_token(env):
    _, approval_id, token = _to_approval(env)
    assert env.anon.get(f"/api/approvals/{approval_id}", params={"token": "nope"}).status_code == 403
    assert env.anon.get(f"/api/approvals/{approval_id}").status_code == 403
    assert env.anon.get("/api/approvals/ap_missing", params={"token": token}).status_code == 404


def test_resend_rotates_token(env):
    flow_id, approval_id, old_token = _to_approval(env)
    revision = env.flow(flow_id)["revision"]
    resp = env.user("admin").post(f"/api/approvals/{approval_id}/resend", json={"revision": revision})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent_count"] == 2
    _, new_token = env.latest_approval_link()
    assert new_token != old_token
    # 舊連結失效
    assert env.anon.get(f"/api/approvals/{approval_id}", params={"token": old_token}).status_code == 403
    assert env.anon.get(f"/api/approvals/{approval_id}", params={"token": new_token}).status_code == 200
    with env.session_factory() as db:
        count = db.query(Notification).filter(Notification.approval_id == approval_id).count()
    assert count == 2
    detail = env.flow(flow_id)
    assert any(e["action"] == "approval.resent" and e["actor"] == "user:admin" for e in detail["audit"])
    # 過期的 revision 被拒
    resp = env.user("admin").post(f"/api/approvals/{approval_id}/resend", json={"revision": revision})
    assert resp.status_code == 409


def test_cancelled_flow_invalidates_link(env):
    flow_id, approval_id, token = _to_approval(env)
    revision = env.flow(flow_id)["revision"]
    env.user("admin").post(f"/api/flows/{flow_id}/cancel", json={"revision": revision})
    resp = env.anon.get(f"/api/approvals/{approval_id}", params={"token": token})
    assert resp.status_code == 410
