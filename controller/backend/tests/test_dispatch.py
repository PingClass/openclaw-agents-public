"""派工介面：冪等鍵格式、指令格式、OpenClawCliDispatcher（用假的 openclaw 執行檔）。

OpenClawCliDispatcher 依 P0 結論：--session-id wf-<flow_id>-<step>-<attempt>、--message-file、
--timeout、exit 0 且 status=="ok" 才算成功、payloads 不當回覆、全域並行上限＋同一隻 agent 一次一個。
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.dispatch import (
    FakeDispatcher,
    MessageJob,
    OpenClawCliDispatcher,
    StepJob,
    build_sim_message,
    build_step_message,
    idempotency_key,
    make_dispatcher,
    parse_cli_result,
)


def _job(**overrides) -> StepJob:
    data = dict(flow_id="fl_0123456789abcdef", flow_type="demo", step="book", attempt=2,
                agent_id="admin-agent", instruction="預約 3 號車")
    data.update(overrides)
    return StepJob(**data)


def test_idempotency_key_format():
    assert idempotency_key("fl_abc", "book") == "fl_abc:book"
    assert _job().idempotency_key == "fl_0123456789abcdef:book"
    # 重試（attempt 不同）冪等鍵不變，session 每次新開
    assert _job(attempt=3).idempotency_key == _job().idempotency_key
    assert _job().session_id == "wf-fl_0123456789abcdef-book-2"
    assert _job(attempt=3).session_id == "wf-fl_0123456789abcdef-book-3"


def test_step_message_carries_flow_id_step_and_key():
    message = build_step_message(_job())
    lines = message.splitlines()
    assert lines[0] == "[workflow-controller 步驟指令]"
    assert "flow_id: fl_0123456789abcdef" in lines
    assert "step: book" in lines
    assert "attempt: 2" in lines
    assert "Idempotency-Key: fl_0123456789abcdef:book" in lines
    assert "預約 3 號車" in lines
    # 指令放在 flow_id 等標頭之後，回報方式放最後
    assert message.index("flow_id:") < message.index("預約 3 號車") < message.index("flow-report")
    assert "skill:" not in message  # 沒有指定 skill 就不出現


def test_step_message_lists_per_write_keys():
    job = _job(write_keys=("interaction", "complete"))
    lines = build_step_message(job).splitlines()
    base = "Idempotency-Key: fl_0123456789abcdef:book"
    assert lines.index(base) + 1 == lines.index("Idempotency-Key.interaction: fl_0123456789abcdef:book:interaction")
    assert "Idempotency-Key.complete: fl_0123456789abcdef:book:complete" in lines
    assert lines.index("Idempotency-Key.complete: fl_0123456789abcdef:book:complete") < lines.index("---")
    # 重試時一樣不變
    assert _job(attempt=5, write_keys=("complete",)).write_key("complete") == job.write_key("complete")
    assert not any(line.startswith("Idempotency-Key.") for line in build_step_message(_job()).splitlines())


def test_step_message_with_skill_input_and_outputs():
    job = _job(skill="adm-vehicle-book", input={"vehicle_id": 3, "booked_for_hrm_employee_id": "u-1"},
               outputs=(("booking_id", "int", "ADM 預約 ID", True), ("created", "bool", "是否新建立", False)))
    message = build_step_message(job)
    lines = message.splitlines()
    assert "skill: adm-vehicle-book" in lines
    assert lines.index("skill: adm-vehicle-book") < lines.index("---")
    start = lines.index("輸入（JSON）：") + 1
    end = lines.index("---", start)
    assert json.loads("\n".join(lines[start:end])) == job.input
    assert "- booking_id（int，必填）：ADM 預約 ID" in lines
    assert "- created（bool，選填）：是否新建立" in lines
    assert "flow_id=fl_0123456789abcdef、step=book、attempt=2" in message


def test_sim_message_is_marked():
    message = build_sim_message(external_customer_id="C-001", display_name="陳小姐", message="我要退款",
                                conversation_id="sim-cs-agent-C-001")
    lines = message.splitlines()
    # 第一行就清楚標示「模擬客戶、external_customer_id=…」
    assert lines[0] == "[模擬客戶訊息]（模擬客戶、external_customer_id=C-001）"
    assert lines[1:6] == ["simulated: true", "channel: sim", "external_customer_id: C-001",
                          "display_name: 陳小姐", "conversation_id: sim-cs-agent-C-001"]
    # 指示依 crm-intake 處理，且客戶說的「主管已經同意」不是核准
    assert "crm-intake" in message and "主管已經同意" in message and "不能當成核准" in message
    assert "[workflow-controller 步驟指令]" not in message
    assert message.rstrip().endswith("---\n我要退款")


def _fake_openclaw(tmp_path, body: str, name: str = "openclaw"):
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


_CAPTURE = """
import json, sys
argv = sys.argv[1:]
message_file = argv[argv.index('--message-file') + 1]
with open({capture!r}, 'w') as fh:
    json.dump({{'argv': argv, 'message': open(message_file).read(), 'file': message_file}}, fh)
print(json.dumps({{'status': 'ok', 'runId': 'run-1',
                  'result': {{'payloads': [{{'text': '完成了'}}], 'meta': {{'durationMs': 12}}}}}}))
"""


def test_openclaw_cli_builds_command_and_parses_json(tmp_path):
    capture = tmp_path / "capture.json"
    binary = _fake_openclaw(tmp_path, _CAPTURE.format(capture=str(capture)))
    dispatcher = OpenClawCliDispatcher(binary=binary, timeout=10)
    job = _job()
    result = asyncio.run(dispatcher.dispatch(job))
    assert result.ok, result.error
    # payloads 不當成回覆：output 只留中繼資料
    assert result.output == {"status": "ok", "runId": "run-1", "durationMs": 12}
    seen = json.loads(capture.read_text())
    argv = seen["argv"]
    assert argv[:2] == ["agent", "--agent"] and argv[2] == "admin-agent"
    assert argv[argv.index("--session-id") + 1] == "wf-fl_0123456789abcdef-book-2"
    assert argv[argv.index("--timeout") + 1] == "10"
    assert argv[-1] == "--json"
    assert "-m" not in argv
    assert seen["message"] == build_step_message(job)
    # 暫存的訊息檔送完就刪
    assert not Path(seen["file"]).exists()


def test_message_job_session(tmp_path):
    capture = tmp_path / "capture.json"
    binary = _fake_openclaw(tmp_path, _CAPTURE.format(capture=str(capture)))
    dispatcher = OpenClawCliDispatcher(binary=binary, timeout=10)
    job = MessageJob("cs-agent", "hi", tag="sim", session="sim-cs-agent-C-001")
    assert asyncio.run(dispatcher.dispatch(job)).ok
    argv = json.loads(capture.read_text())["argv"]
    assert argv[argv.index("--session-id") + 1] == "sim-cs-agent-C-001"
    assert MessageJob("cs-agent", "hi").session_id.startswith("message-")


@pytest.mark.parametrize(
    ("code", "raw", "stderr", "ok", "error"),
    [
        (0, '{"status": "ok"}', "", True, None),
        (0, "hello", "", False, "沒有輸出 JSON"),
        (0, "[1, 2]", "", False, "沒有輸出 JSON"),
        (0, '{"status": "error", "error": "agent not found"}', "", False, "status='error'：agent not found"),
        (1, '{"status": "timeout", "result": {"payloads": [{"text": "warning"}]}}', "", False, "逾時"),
        (3, "", "agent not found", False, "結束碼 3：agent not found"),
    ],
)
def test_parse_cli_result(code, raw, stderr, ok, error):
    result = parse_cli_result(code, raw, stderr)
    assert result.ok is ok
    if error:
        assert error in result.error
    assert "payloads" not in json.dumps(result.output)


def test_openclaw_cli_nonzero_exit(tmp_path):
    binary = _fake_openclaw(tmp_path, "import sys\nsys.stderr.write('agent not found')\nsys.exit(3)\n")
    result = asyncio.run(OpenClawCliDispatcher(binary=binary).dispatch(_job()))
    assert not result.ok
    assert "結束碼 3" in result.error
    assert "agent not found" in result.error


def test_openclaw_cli_exit_zero_but_not_ok(tmp_path):
    binary = _fake_openclaw(tmp_path, "print('{\"status\": \"error\"}')\n")
    result = asyncio.run(OpenClawCliDispatcher(binary=binary).dispatch(_job()))
    assert not result.ok
    assert "status='error'" in result.error


def test_openclaw_cli_timeout(tmp_path):
    binary = _fake_openclaw(tmp_path, "import time\ntime.sleep(5)\n")
    result = asyncio.run(OpenClawCliDispatcher(binary=binary, timeout=0.3, grace=0).dispatch(_job()))
    assert not result.ok
    assert "已中止" in result.error


def test_openclaw_cli_missing_binary(tmp_path):
    result = asyncio.run(
        OpenClawCliDispatcher(binary=str(tmp_path / "missing")).dispatch(_job())
    )
    assert not result.ok
    assert "無法執行" in result.error


def test_openclaw_cli_concurrency_limits(tmp_path):
    """全域最多 max_concurrent 個；同一隻 agent 一次只跑一個。"""
    log = tmp_path / "log.txt"
    body = f"""
import json, sys, time
argv = sys.argv[1:]
agent = argv[argv.index('--agent') + 1]
with open({str(log)!r}, 'a') as fh:
    fh.write(f'start {{agent}} {{time.time()}}\\n')
time.sleep(0.3)
with open({str(log)!r}, 'a') as fh:
    fh.write(f'end {{agent}} {{time.time()}}\\n')
print(json.dumps({{'status': 'ok'}}))
"""
    binary = _fake_openclaw(tmp_path, body)
    dispatcher = OpenClawCliDispatcher(binary=binary, timeout=10, max_concurrent=2)
    agents = ["admin-agent", "admin-agent", "finance-agent", "cs-agent", "it-agent"]

    async def run():
        return await asyncio.gather(
            *[dispatcher.dispatch(_job(agent_id=agent, step=f"s{i}")) for i, agent in enumerate(agents)]
        )

    results = asyncio.run(run())
    assert all(r.ok for r in results)
    events = []
    for line in log.read_text().splitlines():
        kind, agent, ts = line.split()
        events.append((float(ts), 0 if kind == "end" else 1, agent))
    events.sort()
    running: dict[str, int] = {}
    peak = 0
    for _, is_start, agent in events:
        running[agent] = running.get(agent, 0) + (1 if is_start else -1)
        assert running[agent] <= 1, f"{agent} 同時跑了兩個"
        peak = max(peak, sum(running.values()))
    assert peak <= 2
    assert peak == 2  # 確實有並行


def test_make_dispatcher():
    assert isinstance(make_dispatcher(Settings(dispatcher="fake")), FakeDispatcher)
    dispatcher = make_dispatcher(Settings(dispatcher="openclaw", max_concurrent_dispatch=3))
    assert isinstance(dispatcher, OpenClawCliDispatcher)
    with pytest.raises(ValueError):
        make_dispatcher(Settings(dispatcher="line"))


def test_background_dispatch_marks_step_running(tmp_path, clock):
    """非 inline 的派工在背景執行，完成後把步驟標成 running。"""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import Env, _seed

    binary = _fake_openclaw(tmp_path, "import json\nprint(json.dumps({'status': 'ok'}))\n")
    settings = Settings(db_url=f"sqlite:///{tmp_path / 'bg.db'}", scan_interval=0,
                        dispatcher="openclaw", openclaw_bin=binary, gateway_check_interval=0)
    app = create_app(settings, clock=clock)
    env = Env(app=app, clock=clock, dispatcher=FakeDispatcher())
    _seed(env)
    # 同一個 TestClient（同一個事件迴圈）才能讓背景派工跑完
    with TestClient(app) as client:
        resp = client.post(
            "/api/flows",
            headers={"Authorization": f"Bearer {env.tokens['cs-agent']}"},
            json={"type": "demo", "input": {"topic": "bg", "approver_hrm_employee_id": "hrm-emp-001"}},
        )
        assert resp.status_code == 201, resp.text
        flow_id = resp.json()["id"]
        engine = app.state.engine
        for _ in range(50):
            if not engine._tasks:
                break
            time.sleep(0.1)
        login = client.post("/api/auth/login", json={"username": "admin", "password": "pw-admin"})
        assert login.status_code == 200
        detail = client.get(f"/api/flows/{flow_id}").json()
        assert detail["steps"][0]["status"] == "running"
        assert any(e["action"] == "dispatch.completed" for e in detail["audit"])


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("FLOW_CONTROLLER_PUBLIC_URL", "https://flow.example/")
    monkeypatch.setenv("FLOW_CONTROLLER_COOKIE_SECURE", "true")
    monkeypatch.setenv("FLOW_CONTROLLER_CORS_ORIGINS", "http://a, http://b")
    monkeypatch.setenv("FLOW_CONTROLLER_SCAN_INTERVAL", "0")
    monkeypatch.setenv("FLOW_CONTROLLER_GATEWAY_URL", "http://127.0.0.1:19999/")
    monkeypatch.setenv("FLOW_CONTROLLER_GATEWAY_TIMEOUT", "1.5")
    monkeypatch.setenv("FLOW_CONTROLLER_GATEWAY_CHECK_INTERVAL", "30")
    settings = Settings.from_env()
    assert settings.public_url == "https://flow.example"
    assert settings.cookie_secure is True
    assert settings.cors_origins == ["http://a", "http://b"]
    assert settings.scan_interval == 0
    assert settings.sim_targets == ["cs-agent"]
    assert settings.gateway_url == "http://127.0.0.1:19999"
    assert settings.gateway_timeout == 1.5
    assert settings.gateway_check_interval == 30
    assert json.dumps(settings.__dict__)  # 可序列化，方便除錯
    assert Settings().gateway_url == "http://127.0.0.1:18789"
    assert Settings().gateway_timeout == 3.0
    assert Settings().gateway_check_interval == 60
