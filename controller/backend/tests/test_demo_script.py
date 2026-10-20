"""controller/deploy/demo/demo.py（課堂 demo 腳本）：對真的 uvicorn（FakeDispatcher）走一次。

crm.py 的 visit 與 openclaw 用替身（不連 CRM、不叫 agent）；report-trip 用 repo 裡真的 agent-crm crm.py
（帶 --pickup-odometer、--distance，不用連 ADM）。助理 agent 的步驟由測試直接用 token 回報，
人工確認由測試扮演講師（admin 讀收件匣、開確認連結）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

from tests.test_classroom_demo import _step
from tests.test_fake_agents_script import live  # noqa: F401  共用 fixture
from tests.test_visit_flow import CLAIM, CLOSE, DRAFT, HRM

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "controller" / "deploy" / "demo" / "demo.py"
REAL_CRM = REPO / "skills" / "agent-crm" / "scripts" / "crm.py"
NOW = "2026-10-12T10:00:00+08:00"  # 和 conftest 的時鐘一樣


def _load():
    spec = importlib.util.spec_from_file_location("classroom_demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CRM_STUB = textwrap.dedent('''
    import json, os, subprocess, sys
    args = sys.argv[1:]
    if args[0] == "visit":
        time = args[args.index("--time") + 1]
        duration = int(args[args.index("--duration") + 1])
        print(json.dumps({"ok": True, "outcome": "visit_scheduled", "follow_up_id": "fu-demo-1",
                          "customer": "客戶 C（假資料）", "due_at": f"2026-10-12T{time}:00+08:00",
                          "location": "台北市內湖區瑞光路", "seats": 1, "duration_minutes": duration}))
    else:
        sys.exit(subprocess.call([sys.executable, os.environ["REAL_CRM"], *args]))
''')

OPENCLAW_STUB = textwrap.dedent('''
    import json, os, re, sys, urllib.request
    args = sys.argv[1:]
    message = open(args[args.index("--message-file") + 1], encoding="utf-8").read()
    follow_up = re.search(r"--follow-up (\\S+)", message).group(1)
    assert "--demo" in message and args[args.index("--agent") + 1] == "cs-agent"
    data = json.loads(os.environ["STUB_FLOW_INPUT"])
    data["follow_up_id"] = follow_up
    data["demo"] = True
    request = urllib.request.Request(os.environ["FLOW_CONTROLLER_URL"] + "/api/flows", method="POST",
        data=json.dumps({"type": "visit", "input": data}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["FLOW_CONTROLLER_TOKEN_CS_AGENT"]})
    flow = json.loads(urllib.request.urlopen(request).read())
    print(json.dumps({"status": "ok", "result": {"payloads": [{"text": "已啟動 " + flow["id"]}]}}))
''')


@pytest.fixture
def demo(live, tmp_path, monkeypatch):  # noqa: F811
    script, base_url, env = live
    crm_stub = tmp_path / "crm_stub.py"
    crm_stub.write_text(CRM_STUB, encoding="utf-8")
    openclaw = tmp_path / "openclaw"
    openclaw.write_text(f"#!{sys.executable}\n" + OPENCLAW_STUB, encoding="utf-8")
    openclaw.chmod(0o755)
    monkeypatch.setenv("FLOW_CONTROLLER_URL", base_url)
    monkeypatch.setenv("FLOW_CONTROLLER_PUBLIC_URL", "http://gateway-host.test:18090")
    monkeypatch.setenv("FLOW_CONTROLLER_ADMIN_USER", "admin")
    monkeypatch.setenv("FLOW_CONTROLLER_ADMIN_PASSWORD", "pw-admin")
    monkeypatch.setenv("FREEFORJOY_ENV_FILE", "")
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path / "demo-state"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "openclaw-state"))
    monkeypatch.setenv("DEMO_CRM_SCRIPT", str(crm_stub))
    monkeypatch.setenv("REAL_CRM", str(REAL_CRM))
    monkeypatch.setenv("DEMO_OPENCLAW_BIN", str(openclaw))
    monkeypatch.setenv("STUB_FLOW_INPUT", json.dumps({
        "customer": {"name": "客戶 C（假資料）", "external_id": None},
        "visit_start_at": "2026-10-12T10:20:00+08:00", "visit_end_at": "2026-10-12T11:20:00+08:00",
        "location": "台北市內湖區瑞光路", "seats": 1, "assignee_hrm_employee_id": HRM["gobta"]}))
    return _load(), env


def _decide_as_teacher(env, agent_id: str, decision: str = "approve"):
    approval_id, token = env.latest_approval_link(HRM[agent_id])
    resp = env.anon.post(f"/api/approvals/{approval_id}/decision",
                         json={"token": token, "decision": decision, "comment": "講師親自確認"})
    assert resp.status_code == 200, resp.text


def _mode(env, agent_id: str) -> str:
    return env.agent(agent_id).get("/api/inbox/confirm-mode").json()["confirm_mode"]


def test_dry_run_does_not_touch_anything(demo, capsys, tmp_path):
    script, env = demo
    for argv in (["prepare"], ["start"], ["start", "--no-agent"], ["trip"], ["status"], ["reset", "--cancel"]):
        assert script.main([*argv, "--dry-run", "--now", NOW]) == 0, argv
    out = capsys.readouterr().out
    assert "[dry-run] PUT /api/inbox/confirm-mode?recipient=gobta" in out
    assert "visit --agent gobta --days 0 --time 10:20 --duration 60" in out
    assert "--date 2026-10-12 --demo --follow-up" in out
    assert "講師在前端要做的事" in out
    assert not (tmp_path / "demo-state").exists()
    assert _mode(env, "gobta") == "agent"
    assert env.user("admin").get("/api/flows").json()["total"] == 0


def test_full_classroom_demo_with_fake_agents(demo, capsys):
    script, env = demo

    # prepare：main、gobta 改成真人確認，建立今天 10:20 的拜訪
    assert script.main(["prepare", "--now", NOW]) == 0
    out = capsys.readouterr().out
    assert _mode(env, "gobta") == "human" and _mode(env, "main") == "human"
    assert "fu-demo-1" in out and "10:20 開始" in out
    assert f"http://gateway-host.test:18090/inbox?recipient={HRM['gobta']}" in out

    # start：cs-agent（替身）以示範模式啟動
    assert script.main(["start", "--now", NOW, "--wait", "5"]) == 0
    out = capsys.readouterr().out
    flow_id = json.loads((Path(script.state_path())).read_text(encoding="utf-8"))["flow_id"]
    assert f"flow_id：{flow_id}" in out
    assert f"http://gateway-host.test:18090/flows/{flow_id}?demo=1" in out
    detail = env.flow(flow_id)
    assert detail["demo"] is True and detail["title"].startswith("［示範］")

    # admin-agent 查空車 → 講師核准派車（gobta 的 agent 拿不到連結）→ admin-agent 代訂
    env.report(flow_id, "draft_vehicle", "admin-agent", output=DRAFT, summary="草稿")
    items = env.agent("gobta").get("/api/inbox").json()["items"]
    assert all(item["link"] is None for item in items)
    _decide_as_teacher(env, "gobta")
    env.report(flow_id, "book_vehicle", "admin-agent", summary="已代訂",
               output={"booking_id": 41, "vehicle_id": 2, "start_at": DRAFT["start_at"], "end_at": DRAFT["end_at"]})

    # trip：太早（10:00，ADM 10:05 才開放取車）會擋下；10:06 以 gobta 回報（真的 crm.py report-trip）
    assert script.main(["trip", "--now", NOW, "--pickup-odometer", "12000", "--distance", "37"]) == 1
    assert "還不能回報" in capsys.readouterr().out
    assert script.main(["trip", "--now", "2026-10-12T10:06:00+08:00",
                        "--pickup-odometer", "12000", "--distance", "37"]) == 0
    out = capsys.readouterr().out
    assert "取車 12000 km、還車 12037 km" in out
    assert env.flow(flow_id)["current_step"] == "record_trip"

    env.report(flow_id, "record_trip", "admin-agent", summary="已登記",
               output={"booking_id": 41, "pickup_odometer": 12000, "return_odometer": 12037, "distance_km": 37})
    env.report(flow_id, "draft_claim", "finance-agent", output=CLAIM, summary="草稿")
    _decide_as_teacher(env, "main")
    env.report(flow_id, "close_visit", "cs-agent", output=CLOSE, summary="完成")
    detail = env.flow(flow_id)
    assert detail["status"] == "succeeded"
    assert _step(detail, "notify_finance")["status"] == "succeeded"
    assert any(a["action"] == "flow.demo_started" for a in detail["audit"])

    assert script.main(["status"]) == 0
    assert "succeeded" in capsys.readouterr().out

    # reset：切回 agent 代理，狀態檔改名保存
    assert script.main(["reset"]) == 0
    out = capsys.readouterr().out
    assert _mode(env, "gobta") == "agent" and _mode(env, "main") == "agent"
    assert "改回 agent 代理" in out
    assert not Path(script.state_path()).exists()


def test_start_without_agent_and_reset_cancel(demo, capsys):
    """備援：cs-agent 叫不起來時以 admin 直接啟動；課後 reset --cancel 取消沒跑完的流程。"""
    script, env = demo
    assert script.main(["prepare", "--now", NOW]) == 0
    assert script.main(["start", "--no-agent", "--now", NOW, "--wait", "1"]) == 0
    flow_id = json.loads(Path(script.state_path()).read_text(encoding="utf-8"))["flow_id"]
    detail = env.flow(flow_id)
    assert detail["created_by"] == "user:admin" and detail["demo"] is True
    assert script.main(["trip", "--now", "2026-10-12T10:06:00+08:00", "--pickup-odometer", "1"]) == 1
    assert "還沒到等業務回報里程" in capsys.readouterr().out
    assert script.main(["reset", "--cancel"]) == 0
    assert env.flow(flow_id)["status"] == "cancelled"


def test_start_reports_agent_failure(demo, capsys, tmp_path, monkeypatch):
    script, env = demo
    assert script.main(["prepare", "--now", NOW]) == 0
    broken = tmp_path / "openclaw-broken"
    broken.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({{'status': 'error'}}))\nraise SystemExit(1)\n",
                      encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setenv("DEMO_OPENCLAW_BIN", str(broken))
    assert script.main(["start", "--now", NOW]) == 1
    assert "demo.py start --no-agent" in capsys.readouterr().out


def test_check_reports_each_item(demo, capsys, tmp_path, monkeypatch):
    script, _env = demo
    pong = tmp_path / "openclaw-pong"
    pong.write_text(f"#!{sys.executable}\nimport json\n"
                    "print(json.dumps({'status': 'ok', 'result': {'payloads': [{'text': 'PONG'}]}}))\n",
                    encoding="utf-8")
    pong.chmod(0o755)
    monkeypatch.setenv("DEMO_OPENCLAW_BIN", str(pong))
    monkeypatch.setenv("DEMO_VISIT_LIST_SCRIPT", str(REAL_CRM))  # 只檢查檔案存在
    monkeypatch.setenv("FLOW_CONTROLLER_GATEWAY_URL", "http://127.0.0.1:9")  # 連不上
    assert script.main(["check", "--ping"]) == 1
    out = capsys.readouterr().out
    assert "✘ OpenClaw gateway /readyz" in out
    assert "✔ controller /healthz" in out and "✔ controller admin 登入" in out
    assert "✔ cs-agent 回 PONG" in out and "✔ finance-agent 回 PONG" in out
    assert script.main(["check", "--dry-run", "--ping"]) == 0


def test_missing_admin_password(demo, capsys, monkeypatch):
    script, _env = demo
    monkeypatch.delenv("FLOW_CONTROLLER_ADMIN_PASSWORD")
    assert script.main(["prepare", "--now", NOW]) == 1
    assert "FLOW_CONTROLLER_ADMIN_PASSWORD" in capsys.readouterr().out


def test_visit_time_rounds_up_to_five_minutes():
    script = _load()
    from datetime import datetime

    assert script.visit_time(datetime(2026, 10, 12, 10, 3, tzinfo=script.TPE), 20).strftime("%H:%M") == "10:25"
    assert script.visit_time(datetime(2026, 10, 12, 10, 0, tzinfo=script.TPE), 20).strftime("%H:%M") == "10:20"
    assert script.visit_time(datetime(2026, 10, 12, 10, 41, tzinfo=script.TPE), 20).strftime("%H:%M") == "11:05"


def test_prepare_rejects_lead_beyond_demo_window(demo, capsys):
    script, _env = demo
    assert script.main(["prepare", "--dry-run", "--now", NOW, "--lead-minutes", "200"]) == 1
    assert "3 小時內" in capsys.readouterr().out
