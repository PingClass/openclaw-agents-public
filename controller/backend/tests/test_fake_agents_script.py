"""scripts/fake_agents.py：對真的 uvicorn（FakeDispatcher）跑完整條 ①，以及「故意不回報」。"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.config import Settings
from app.dispatch import FakeDispatcher
from app.main import create_app
from tests.conftest import Env, _seed
from tests.test_server_sse import _free_port
from tests.test_visit_flow import _load_identities

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fake_agents.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("fake_agents", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def live(tmp_path, clock, monkeypatch):
    settings = Settings(db_url=f"sqlite:///{tmp_path / 'live.db'}", scan_interval=0, dispatcher="fake",
                        gateway_check_interval=0)
    app = create_app(settings, dispatcher=FakeDispatcher(inline=True), clock=clock)
    env = Env(app=app, clock=clock, dispatcher=app.state.engine.dispatcher)
    _seed(env)
    _load_identities(env)
    script = _load_script()
    for agent_id, token in env.tokens.items():
        monkeypatch.setenv(script.token_env(agent_id), token)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield script, f"http://127.0.0.1:{port}", env
    server.should_exit = True
    thread.join(timeout=5)


def test_token_env_name():
    script = _load_script()
    assert script.token_env("mjolmire-businessman") == "FLOW_CONTROLLER_TOKEN_MJOLMIRE_BUSINESSMAN"
    assert script.token_env("cs-agent") == "FLOW_CONTROLLER_TOKEN_CS_AGENT"


def test_fake_agents_run_full_visit_flow(live, capsys):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--visit-date", "2026-10-14", "--conflicts", "1",
                        "--follow-up-id", "555", "--poll", "0.05", "--timeout", "30"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "結束：succeeded" in out
    assert "ADM 回 409 時段衝突" in out
    assert "交通費報帳已核准" in out and "NT$185" in out
    flows = env.user("admin").get("/api/flows", params={"type": "visit"}).json()["items"]
    assert [f["status"] for f in flows] == ["succeeded"]


def test_fake_agents_stall_leaves_flow_stuck(live, capsys, clock):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--visit-date", "2026-10-14", "--stall-at", "draft_vehicle",
                        "--follow-up-id", "556", "--poll", "0.05", "--timeout", "10"])
    assert code == 0
    assert "故意不回報" in capsys.readouterr().out
    clock.advance(minutes=6)
    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["counts"]["stuck"] == 1
    assert summary["stuck"][0]["stuck"]["reason"] == "admin-agent 已 6 分鐘沒有回報"


def test_fake_agents_wait_retry_resumes_after_admin_retry(live, capsys, clock):
    """P2.5：卡住 → admin 在前端按重試 → 假 agent 繼續跑完。"""
    script, base_url, env = live
    result: dict[str, int] = {}

    def run_script():
        result["code"] = script.main(["--base-url", base_url, "--visit-date", "2026-10-14",
                                      "--stall-at", "draft_vehicle", "--wait-retry",
                                      "--follow-up-id", "557", "--poll", "0.05", "--timeout", "30"])

    thread = threading.Thread(target=run_script, daemon=True)
    thread.start()
    admin = env.user("admin")
    flow = None
    for _ in range(200):
        items = admin.get("/api/flows", params={"type": "visit"}).json()["items"]
        if items and items[0]["current_step"] == "draft_vehicle":
            flow = items[0]
            break
        time.sleep(0.05)
    assert flow is not None
    clock.advance(minutes=6)
    summary = admin.get("/api/stats/summary").json()
    assert summary["stuck"][0]["stuck"]["code"] == "agent_no_report"

    detail = env.flow(flow["id"])
    resp = admin.post(f"/api/flows/{flow['id']}/steps/draft_vehicle/retry", json={"revision": detail["revision"]})
    assert resp.status_code == 200
    thread.join(timeout=30)
    out = capsys.readouterr().out
    assert result.get("code") == 0, out
    assert "偵測到重試" in out
    assert "結束：succeeded" in out
    assert "step.retry" in [entry["action"] for entry in env.flow(flow["id"])["audit"]]


def test_fake_agents_manual_approvals_wait_for_humans(live, capsys):
    """錄影用：--manual-approvals 不代按兩次確認，等人（這裡用確認頁 API）決定後才往下跑；--pace 放慢每步。"""
    script, base_url, env = live
    result: dict[str, int] = {}

    def run_script():
        result["code"] = script.main(["--base-url", base_url, "--visit-date", "2026-10-14", "--manual-approvals",
                                      "--pace", "0.05", "--follow-up-id", "558", "--poll", "0.05",
                                      "--timeout", "30"])

    thread = threading.Thread(target=run_script, daemon=True)
    thread.start()
    api = script.Api(base_url)
    admin = env.user("admin")

    def wait_waiting(step_name):
        for _ in range(400):
            items = admin.get("/api/flows", params={"type": "visit"}).json()["items"]
            if items and items[0]["current_step"] == step_name:
                detail = env.flow(items[0]["id"])
                step = next(s for s in detail["steps"] if s["name"] == step_name)
                if step["status"] == "waiting_approval":
                    return items[0]["id"]
            time.sleep(0.05)
        raise AssertionError(f"沒有停在 {step_name}")

    flow_id = wait_waiting("confirm_vehicle")
    time.sleep(0.3)  # 假 agent 不會自己代按
    assert env.flow(flow_id)["current_step"] == "confirm_vehicle"
    assert script.decide(api, "mjolmire-businessman", flow_id, "approve", "人在確認頁按的")
    wait_waiting("approve_claim")
    assert script.decide(api, "main", flow_id, "approve", "人在確認頁按的")
    thread.join(timeout=30)
    out = capsys.readouterr().out
    assert result.get("code") == 0, out
    assert "結束：succeeded" in out
    assert "OK，就這台" not in out  # 沒有由假 agent 代按


def test_fake_agents_demo_flow(live, capsys):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--demo", "--visit-date", "2026-10-12",
                        "--follow-up-id", "558", "--poll", "0.05", "--timeout", "30"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "［示範］" in out and "結束：succeeded" in out
    flows = env.user("admin").get("/api/flows", params={"type": "visit"}).json()["items"]
    assert flows[0]["demo"] is True


def test_fake_agents_attach_assistants_only_waits_for_humans(live, capsys):
    """課堂排練：只扮演助理 agent，停在人工確認等講師。"""
    script, base_url, env = live
    from tests.test_classroom_demo import _demo_input

    flow = env.agent("cs-agent").post("/api/flows", json={"type": "visit", "input": _demo_input()}).json()
    code = script.main(["--base-url", base_url, "--attach", flow["id"], "--assistants-only",
                        "--poll", "0.05", "--timeout", "1.5"])
    out = capsys.readouterr().out
    assert code == 1  # 沒有人確認，時間到就結束
    assert "接手流程" in out and "回報 draft_vehicle" in out and "等人處理「確認派車」" in out
    detail = env.flow(flow["id"])
    assert detail["current_step"] == "confirm_vehicle" and detail["status"] == "waiting_approval"


def test_fake_agents_attach_manual_approvals_with_pace(live, capsys):
    """錄影＋課堂 demo 相容：--attach 接手示範流程，--pace 放慢、--manual-approvals 停在確認等人。"""
    script, base_url, env = live
    from tests.test_classroom_demo import _demo_input

    flow = env.agent("cs-agent").post("/api/flows", json={"type": "visit", "input": _demo_input()}).json()
    code = script.main(["--base-url", base_url, "--attach", flow["id"], "--manual-approvals", "--pace", "0.05",
                        "--poll", "0.05", "--timeout", "1.5"])
    out = capsys.readouterr().out
    assert code == 1  # 沒有人確認，時間到就結束
    assert "接手流程" in out and "回報 draft_vehicle" in out and "等人處理「確認派車」（--manual-approvals）" in out
    assert out.count("等人處理「確認派車」") == 1  # 等人時只記一次
    detail = env.flow(flow["id"])
    assert detail["current_step"] == "confirm_vehicle" and detail["status"] == "waiting_approval"


def test_fake_agents_requires_tokens(monkeypatch):
    script = _load_script()
    monkeypatch.delenv("FLOW_CONTROLLER_TOKEN_CS_AGENT", raising=False)
    with pytest.raises(SystemExit, match="FLOW_CONTROLLER_TOKEN_CS_AGENT"):
        script.main(["--base-url", "http://127.0.0.1:9", "--visit-date", "2026-10-14"])


# ---------------------------------------------------------------- 流程 ②


def test_fake_agents_run_full_refund_flow_with_revise(live, capsys):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--flow", "refund", "--refund-amount", "1200", "--revise", "1",
                        "--case-id", "case-555", "--poll", "0.05", "--timeout", "30"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "結束：succeeded（outcome=replied）" in out
    assert "模擬對話（in）" in out and "模擬對話（out）" in out and "NT$1,200" in out
    flow = env.user("admin").get("/api/flows", params={"type": "refund"}).json()["items"][0]
    detail = env.flow(flow["id"])
    review = [a["decision"] for a in detail["approvals"] if a["step"] == "review_reply"]
    assert review == ["revise", "approve"]
    assert [m["body"] for m in detail["messages"]][0].endswith("（第 2 版，假資料）")


def test_fake_agents_refund_escalate(live, capsys):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--flow", "refund", "--refund-decision", "escalate",
                        "--case-id", "case-556", "--poll", "0.05", "--timeout", "30"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "outcome=escalated" in out


def test_fake_agents_refund_stall_at_decision(live, capsys, clock):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--flow", "refund", "--stall-at", "decide_refund",
                        "--case-id", "case-557", "--poll", "0.05", "--timeout", "10"])
    assert code == 0
    assert "故意不處理" in capsys.readouterr().out
    clock.advance(hours=4, minutes=1)
    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["stuck"][0]["stuck"]["code"] == "approval_waiting_too_long"


# ---------------------------------------------------------------- 流程 ③


def test_fake_agents_run_full_onboarding_flow(live, capsys):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--flow", "onboarding", "--start-date", "2026-10-19",
                        "--employee-id", "fake-new-1", "--poll", "0.05", "--timeout", "30"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "結束：succeeded" in out
    assert "laptop_assigned" in out
    flows = env.user("admin").get("/api/flows", params={"type": "onboarding"}).json()["items"]
    assert [f["status"] for f in flows] == ["succeeded"]


def test_fake_agents_onboarding_department_without_manager_stops(live, capsys):
    script, base_url, env = live
    code = script.main(["--base-url", base_url, "--flow", "onboarding", "--department", "資訊部",
                        "--department-id", "3", "--start-date", "2026-10-19", "--employee-id", "fake-new-2",
                        "--poll", "0.05", "--timeout", "10"])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "資訊部沒有部門主管" in out
    assert "shion" in out
