"""卡住的四種原因、逾時掃描、總覽統計。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.dispatch import FakeDispatcher
from app.models import Flow, Step
from app.stuck import compute_stuck, format_deadline, format_duration
from tests.conftest import APPROVER


def _scan(env):
    import asyncio

    engine = env.app.state.engine
    with env.session_factory() as db:
        ctx = engine.context(db, actor="controller")
        result = engine.scan(ctx)
        asyncio.run(engine.commit(ctx))
    return result


def test_agent_no_report(env, clock):
    flow_id = env.start_demo()["id"]
    # 還在時限內：不算卡住
    clock.advance(minutes=4)
    assert env.flow(flow_id)["stuck"] is None
    assert env.user("viewer").get("/api/flows", params={"stuck": "true"}).json()["total"] == 0

    clock.advance(minutes=3)  # 已派出 7 分鐘，時限 5 分鐘
    resp = env.user("viewer").get("/api/flows", params={"stuck": "true"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [flow_id]
    stuck = items[0]["stuck"]
    assert stuck == {
        "code": "agent_no_report",
        "reason": "admin-agent 已 7 分鐘沒有回報",
        "step": "draft",
        "waited_seconds": 420,
    }
    assert items[0]["waiting_for"]["id"] == "admin-agent"
    # stuck=false 排除卡住的
    assert env.user("viewer").get("/api/flows", params={"stuck": "false"}).json()["total"] == 0

    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["counts"]["stuck"] == 1
    assert summary["counts"]["running"] == 1
    assert summary["stuck"][0]["stuck"]["code"] == "agent_no_report"

    detail = env.flow(flow_id)
    assert detail["steps"][0]["stuck"]["code"] == "agent_no_report"
    assert detail["steps"][1]["stuck"] is None


def test_approval_waiting_too_long(env, clock):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={})
    clock.advance(hours=1, minutes=59)
    assert env.flow(flow_id)["stuck"] is None
    clock.advance(minutes=1)  # 4 小時時限的一半
    stuck = env.flow(flow_id)["stuck"]
    assert stuck["code"] == "approval_waiting_too_long"
    # 台北時間 10:00 開始，4 小時後 14:00 截止
    assert stuck["reason"] == "等王經理確認草稿，已 2 小時，14:00 截止"
    assert stuck["waited_seconds"] == 7200

    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["counts"]["waiting_approval"] == 1
    pending = summary["pending_approvals"][0]
    assert pending["approver"]["display_name"] == "王經理"
    assert pending["remaining_seconds"] == 7200


def test_retries_exhausted(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", status="failed", error="CRM 回 400")
    env.report(flow_id, "draft", "admin-agent", status="failed", error="CRM 回 400")
    stuck = env.flow(flow_id)["stuck"]
    assert stuck["code"] == "retries_exhausted"
    assert stuck["reason"] == "CRM 回 400，已重試 1 次"


def test_system_error_from_agent_report(env):
    flow_id = env.start_demo()["id"]
    for _ in range(2):
        env.report(flow_id, "draft", "admin-agent", status="failed", error="ADM 無法連線",
                   error_kind="system")
    stuck = env.flow(flow_id)["stuck"]
    assert stuck["code"] == "system_error"
    assert stuck["reason"] == "產生草稿：ADM 無法連線"


def test_system_error_from_dispatcher(env):
    env.dispatcher.fail_with = "無法執行 openclaw"
    flow_id = env.start_demo()["id"]
    detail = env.flow(flow_id)
    draft = detail["steps"][0]
    # max_attempts=2：派工失敗會自動重派一次，之後停下來
    assert draft["attempts"] == 2
    assert draft["status"] == "failed"
    assert draft["error_kind"] == "system"
    assert detail["stuck"]["code"] == "system_error"
    agents = env.user("viewer").get("/api/agents").json()["items"]
    admin_agent = next(a for a in agents if a["id"] == "admin-agent")
    assert admin_agent["status"] == "error"
    assert admin_agent["last_error"] == "無法執行 openclaw"


def test_scan_expires_approval_and_notifies(env, clock):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={})
    clock.advance(hours=3)
    result = _scan(env)
    assert result["expired"] == 0
    assert result["stuck_changes"] == 1

    clock.advance(hours=1)
    result = _scan(env)
    assert result["expired"] == 1
    detail = env.flow(flow_id)
    assert detail["status"] == "expired"
    assert detail["approvals"][0]["status"] == "expired"
    assert detail["steps"][1]["status"] == "failed"
    assert detail["steps"][2]["status"] == "skipped"
    inbox = env.user("admin").get("/api/inbox", params={"recipient": APPROVER}).json()
    kinds = [item["kind"] for item in inbox["items"]]
    assert "approval_expired" in kinds
    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["counts"]["failed_today"] == 1
    assert summary["counts"]["running"] == 0


def test_scan_reports_stuck_changes_once(env, clock):
    env.start_demo()
    assert _scan(env)["stuck_changes"] == 0  # 未卡住不推送
    assert _scan(env)["stuck_changes"] == 0
    clock.advance(minutes=6)
    assert _scan(env)["stuck_changes"] == 1
    assert _scan(env)["stuck_changes"] == 0


def test_stats_succeeded_today(env):
    flow_id = env.start_demo()["id"]
    env.report(flow_id, "draft", "admin-agent", output={})
    approval_id, token = env.latest_approval_link()
    env.anon.post(f"/api/approvals/{approval_id}/decision", json={"token": token, "decision": "approve"})
    env.report(flow_id, "finalize", "finance-agent", output={})
    counts = env.user("viewer").get("/api/stats/summary").json()["counts"]
    assert counts == {"running": 0, "waiting_approval": 0, "active": 0, "stuck": 0,
                      "succeeded_today": 1, "failed_today": 0}


def test_stats_running_excludes_waiting_approval(env):
    """總覽的「進行中」不含等待確認，兩個數字不重疊；active 是合計。"""
    first = env.start_demo("A")["id"]
    env.start_demo("B")
    env.report(first, "draft", "admin-agent", output={})  # A 進入等待確認
    counts = env.user("viewer").get("/api/stats/summary").json()["counts"]
    assert counts["running"] == 1
    assert counts["waiting_approval"] == 1
    assert counts["active"] == 2


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(30, "不到 1 分鐘"), (420, "7 分鐘"), (3 * 3600, "3 小時"), (3 * 3600 + 600, "3 小時 10 分鐘"),
     (2 * 86400 + 5, "2 天")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_format_deadline_other_day():
    now = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)
    assert format_deadline(now + timedelta(hours=8), now, "Asia/Taipei") == "18:00"
    assert format_deadline(now + timedelta(days=2, hours=8), now, "Asia/Taipei") == "10/14 18:00"


def test_compute_stuck_ignores_finished_flow():
    flow = Flow(id="fl_x", type="demo", title="x", status="succeeded", state={})
    step = Step(name="draft", status="failed", assignee="admin-agent", failures=3)
    assert compute_stuck(flow, step, datetime.now(UTC)) is None


def test_list_filters(env, clock):
    a = env.start_demo("A")["id"]
    clock.advance(days=1)
    b = env.start_demo("B")["id"]
    viewer = env.user("viewer")
    assert viewer.get("/api/flows", params={"type": "demo"}).json()["total"] == 2
    assert viewer.get("/api/flows", params={"type": "visit"}).json()["total"] == 0
    assert viewer.get("/api/flows", params={"status": "running,waiting_approval"}).json()["total"] == 2
    assert viewer.get("/api/flows", params={"status": "bogus"}).status_code == 422
    resp = viewer.get("/api/flows", params={"from": "2026-10-13"}).json()
    assert [item["id"] for item in resp["items"]] == [b]
    resp = viewer.get("/api/flows", params={"to": "2026-10-12"}).json()
    assert [item["id"] for item in resp["items"]] == [a]
    resp = viewer.get("/api/flows", params={"from": "2026-10-12T00:00:00+08:00",
                                            "to": "2026-10-12T23:59:59+08:00"}).json()
    assert [item["id"] for item in resp["items"]] == [a]
    assert viewer.get("/api/flows", params={"from": "not-a-date"}).status_code == 422
    resp = viewer.get("/api/flows", params={"limit": 1, "offset": 1}).json()
    assert resp["total"] == 2 and [i["id"] for i in resp["items"]] == [a]


def test_fake_dispatcher_non_inline_records_jobs():
    import asyncio

    from app.dispatch import MessageJob

    dispatcher = FakeDispatcher(inline=False)
    result = asyncio.run(dispatcher.dispatch(MessageJob(agent_id="cs-agent", message="hi")))
    assert result.ok
    assert dispatcher.step_jobs == []
    with pytest.raises(LookupError):
        dispatcher.last_job_for("draft")
