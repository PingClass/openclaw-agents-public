"""demo 流程用 FakeDispatcher 從開始跑到結束，以及各種分支。"""

from __future__ import annotations

from app.dispatch import StepJob
from tests.conftest import APPROVER


def test_demo_flow_end_to_end(env):
    flow = env.start_demo("10/15 拜訪客戶 C")
    flow_id = flow["id"]
    assert flow["created"] is True
    assert flow["status"] == "running"
    assert flow["current_step"] == "draft"
    assert flow["title"] == "示範：10/15 拜訪客戶 C"

    # 1) draft 派給 admin-agent，指令帶 flow_id 與冪等鍵
    job = env.dispatcher.last_job_for("draft")
    assert isinstance(job, StepJob)
    assert job.agent_id == "admin-agent"
    assert job.flow_id == flow_id
    assert job.idempotency_key == f"{flow_id}:draft"
    detail = env.flow(flow_id)
    draft = detail["steps"][0]
    assert draft["status"] == "running"  # FakeDispatcher 已完成派工
    assert draft["attempts"] == 1

    resp = env.report(flow_id, "draft", "admin-agent", summary="草稿完成",
                      output={"draft": "派車：3 號車"}, attempt=1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "waiting_approval"
    assert resp.json()["waiting_for"] == {"kind": "human", "id": None, "name": "王經理"}

    # 2) 人工確認：收件匣有連結，確認頁只顯示需要的內容
    approval_id, token = env.latest_approval_link()
    page = env.anon.get(f"/api/approvals/{approval_id}", params={"token": token})
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["question"] == "請確認「10/15 拜訪客戶 C」的草稿"
    assert body["context"] == {"topic": "10/15 拜訪客戶 C", "draft": "派車：3 號車"}
    assert body["approver_name"] == "王經理"
    assert "token" not in str(body).lower().replace("token_", "")

    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "approve", "comment": "OK"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "approve"

    # 3) finalize 派給 finance-agent，指令裡有確認意見
    job = env.dispatcher.last_job_for("finalize")
    assert job.agent_id == "finance-agent"
    assert "OK" in job.instruction
    resp = env.report(flow_id, "finalize", "finance-agent", summary="完成", output={"done": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "succeeded"

    detail = env.flow(flow_id)
    assert detail["status"] == "succeeded"
    assert detail["finished_at"] is not None
    assert [s["status"] for s in detail["steps"]] == ["succeeded", "succeeded", "succeeded"]
    assert detail["state"]["decisions"]["confirm"] == {"decision": "approve", "comment": "OK"}
    assert detail["approvals"][0]["decision"] == "approve"
    actions = [entry["action"] for entry in detail["audit"]]
    for expected in ["flow.started", "step.dispatched", "step.reported", "approval.created",
                     "approval.decided", "flow.succeeded"]:
        assert expected in actions
    decided = next(e for e in detail["audit"] if e["action"] == "approval.decided")
    assert decided["actor"] == "human:王經理"

    # 確認後通知標為已讀
    inbox = env.user("admin").get("/api/inbox", params={"recipient": APPROVER}).json()
    assert inbox["unread_count"] == 0


def test_dedupe_key_returns_existing_flow(env):
    first = env.start_demo(dedupe_key="crm:follow-up:1")
    second = env.start_demo(dedupe_key="crm:follow-up:1")
    assert second["id"] == first["id"]
    assert second["created"] is False
    assert len(env.dispatcher.step_jobs) == 1


def test_invalid_input_and_unknown_type(env):
    resp = env.agent("cs-agent").post("/api/flows", json={"type": "demo", "input": {"topic": ""}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_input"
    resp = env.agent("cs-agent").post("/api/flows", json={"type": "nope", "input": {}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_flow_type"


def test_admin_can_start_flow_viewer_cannot(env):
    payload = {"type": "demo", "input": {"topic": "x", "approver_hrm_employee_id": APPROVER}}
    assert env.user("admin").post("/api/flows", json=payload).status_code == 201
    assert env.user("viewer").post("/api/flows", json=payload).status_code == 403
    assert env.anon.post("/api/flows", json=payload).status_code == 401


def test_revise_goes_back_to_draft_and_reject_fails(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={"draft": "v1"})
    approval_id, token = env.latest_approval_link()
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "revise", "comment": "改日期"})
    assert resp.status_code == 200
    detail = env.flow(flow_id)
    assert detail["current_step"] == "draft"
    assert detail["steps"][0]["attempts"] == 2
    assert env.dispatcher.last_job_for("draft").attempt == 2

    env.report(flow_id, "draft", "admin-agent", output={"draft": "v2"}, attempt=2)
    approval_id2, token2 = env.latest_approval_link()
    assert approval_id2 != approval_id
    resp = env.anon.post(f"/api/approvals/{approval_id2}/decision",
                         json={"token": token2, "decision": "reject"})
    assert resp.status_code == 200
    detail = env.flow(flow_id)
    assert detail["status"] == "failed"
    assert detail["steps"][2]["status"] == "skipped"


def test_invalid_decision_value(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={})
    approval_id, token = env.latest_approval_link()
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "maybe"})
    assert resp.status_code == 422
    # 錯誤選項不會消耗 token
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": "approve"})
    assert resp.status_code == 200


def test_failure_goes_back_to_on_failure_step_then_exhausts(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={"draft": "v1"})
    approval_id, token = env.latest_approval_link()
    env.anon.post(f"/api/approvals/{approval_id}/decision", json={"token": token, "decision": "approve"})

    # finalize 失敗 → 回到 draft（最多 3 次）
    for round_ in range(1, 3):
        resp = env.report(flow_id, "finalize", "finance-agent", status="failed",
                          error="ADM 回 409 時段衝突", error_kind="business")
        assert resp.status_code == 200, resp.text
        detail = env.flow(flow_id)
        assert detail["current_step"] == "draft", round_
        env.report(flow_id, "draft", "admin-agent", output={"draft": f"v{round_ + 1}"})
        approval_id, token = env.latest_approval_link()
        env.anon.post(f"/api/approvals/{approval_id}/decision", json={"token": token, "decision": "approve"})

    resp = env.report(flow_id, "finalize", "finance-agent", status="failed",
                      error="ADM 回 409 時段衝突", error_kind="business")
    detail = env.flow(flow_id)
    assert detail["status"] == "running"  # 停下來交給人
    assert detail["current_step"] == "finalize"
    finalize = detail["steps"][2]
    assert finalize["status"] == "failed"
    assert finalize["failures"] == 3
    assert detail["stuck"]["code"] == "retries_exhausted"
    assert detail["stuck"]["reason"] == "ADM 回 409 時段衝突，已重試 2 次"


def test_same_step_retry_when_no_on_failure(env):
    flow_id = env.start_demo()["id"]
    resp = env.report(flow_id, "draft", "admin-agent", status="failed", error="暫時失敗")
    assert resp.status_code == 200
    detail = env.flow(flow_id)
    assert detail["current_step"] == "draft"
    assert detail["steps"][0]["status"] == "running"
    assert detail["steps"][0]["attempts"] == 2
    # 同一步驟重試沿用相同的冪等鍵
    jobs = [j for j in env.dispatcher.step_jobs if j.step == "draft"]
    assert {j.idempotency_key for j in jobs} == {f"{flow_id}:draft"}


def test_report_guards(env):
    flow_id = env.start_demo()["id"]
    # 不是指派對象
    resp = env.report(flow_id, "draft", "finance-agent")
    assert resp.status_code == 403
    # 不是目前步驟
    resp = env.report(flow_id, "finalize", "finance-agent")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "step_not_current"
    # 過期的 attempt
    resp = env.report(flow_id, "draft", "admin-agent", attempt=5)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "stale_attempt"
    # 正常回報後重複回報
    assert env.report(flow_id, "draft", "admin-agent").status_code == 200
    resp = env.report(flow_id, "draft", "admin-agent")
    assert resp.status_code == 409
    # 前端帳號不能回報
    resp = env.user("admin").post(f"/api/flows/{flow_id}/steps/draft/result", json={"status": "ok"})
    assert resp.status_code == 401
    # 找不到流程
    assert env.report("fl_nope", "draft", "admin-agent").status_code == 404


def test_needs_human_does_not_auto_retry(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", status="needs_human", summary="資料不足，請人補")
    detail = env.flow(flow_id)
    assert detail["steps"][0]["status"] == "failed"
    assert detail["steps"][0]["attempts"] == 1
    assert detail["stuck"]["code"] == "retries_exhausted"
    assert "需要人處理" in detail["stuck"]["reason"]


def test_missing_approver_identity_is_system_error(env):
    resp = env.agent("cs-agent").post(
        "/api/flows",
        json={"type": "demo", "input": {"topic": "x", "approver_hrm_employee_id": "nobody"}},
    )
    flow_id = resp.json()["id"]
    env.report(flow_id, "draft", "admin-agent", output={})
    detail = env.flow(flow_id)
    assert detail["current_step"] == "confirm"
    assert detail["stuck"]["code"] == "system_error"
    assert "找不到確認者" in detail["stuck"]["reason"]
