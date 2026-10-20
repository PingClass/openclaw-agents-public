"""用真的 uvicorn 起服務：/healthz 有回應、SSE 能連上並收到事件。"""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.config import Settings
from app.dispatch import FakeDispatcher
from app.main import create_app
from tests.conftest import APPROVER, Env, _seed


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(tmp_path, clock):
    settings = Settings(db_url=f"sqlite:///{tmp_path / 'server.db'}", scan_interval=0,
                        dispatcher="fake", sse_keepalive=0.2)
    app = create_app(settings, dispatcher=FakeDispatcher(inline=True), clock=clock)
    env = Env(app=app, clock=clock, dispatcher=app.state.engine.dispatcher)
    _seed(env)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            timeout_graceful_shutdown=1)
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}", env
    srv.should_exit = True
    thread.join(timeout=5)


def test_healthz(server):
    base, _ = server
    resp = httpx.get(f"{base}/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert httpx.get(f"{base}/api/healthz").status_code == 200


def test_sse_requires_login(server):
    base, _ = server
    assert httpx.get(f"{base}/api/events").status_code == 401


def test_sse_receives_hello_and_flow_events(server):
    base, env = server
    with httpx.Client(base_url=base, timeout=10) as client:
        assert client.post("/api/auth/login", json={"username": "viewer", "password": "pw-viewer"}).status_code == 200
        with client.stream("GET", "/api/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.iter_lines()
            # 連上就會收到 hello
            first = []
            for line in lines:
                first.append(line)
                if line == "":
                    break
            assert "event: hello" in first

            # 另一個連線啟動流程，SSE 會推送
            resp = httpx.post(
                f"{base}/api/flows",
                headers={"Authorization": f"Bearer {env.tokens['cs-agent']}"},
                json={"type": "demo", "input": {"topic": "SSE", "approver_hrm_employee_id": APPROVER}},
            )
            assert resp.status_code == 201
            flow_id = resp.json()["id"]
            events = []
            event_type = None
            deadline = time.monotonic() + 5
            for line in lines:
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: ") and event_type:
                    events.append((event_type, json.loads(line[6:])))
                    if event_type == "flow.created":
                        break
                if time.monotonic() > deadline:
                    break
            created = [data for kind, data in events if kind == "flow.created"]
            assert created and created[0]["flow_id"] == flow_id
            assert "revision" in created[0]
