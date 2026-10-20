from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.cli import import_identities
from app.config import Settings
from app.dispatch import FakeDispatcher
from app.main import create_app
from app.models import Agent, AgentToken, User
from app.security import hash_password, new_token, sha256_hex

APPROVER = "hrm-emp-001"


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class Env:
    app: object
    clock: FakeClock
    dispatcher: FakeDispatcher
    tokens: dict[str, str] = field(default_factory=dict)
    clients: dict[str, TestClient] = field(default_factory=dict)

    @property
    def session_factory(self):
        return self.app.state.session_factory

    def agent(self, agent_id: str) -> TestClient:
        """帶 agent token 的 client。"""
        key = f"agent:{agent_id}"
        if key not in self.clients:
            client = TestClient(self.app)
            client.headers["Authorization"] = f"Bearer {self.tokens[agent_id]}"
            self.clients[key] = client
        return self.clients[key]

    def user(self, username: str) -> TestClient:
        key = f"user:{username}"
        if key not in self.clients:
            client = TestClient(self.app)
            resp = client.post("/api/auth/login", json={"username": username, "password": "pw-" + username})
            assert resp.status_code == 200, resp.text
            self.clients[key] = client
        return self.clients[key]

    @property
    def anon(self) -> TestClient:
        if "anon" not in self.clients:
            self.clients["anon"] = TestClient(self.app)
        return self.clients["anon"]

    def start_demo(self, topic: str = "測試主題", **extra) -> dict:
        resp = self.agent("cs-agent").post(
            "/api/flows",
            json={"type": "demo", "input": {"topic": topic, "approver_hrm_employee_id": APPROVER}, **extra},
        )
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    def report(self, flow_id: str, step: str, agent_id: str, **body):
        body.setdefault("status", "ok")
        return self.agent(agent_id).post(f"/api/flows/{flow_id}/steps/{step}/result", json=body)

    def flow(self, flow_id: str) -> dict:
        resp = self.user("admin").get(f"/api/flows/{flow_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    def latest_approval_link(self, recipient: str = APPROVER) -> tuple[str, str]:
        """從收件匣取出最新的確認連結，回傳 (approval_id, token)。"""
        resp = self.user("admin").get("/api/inbox", params={"recipient": recipient})
        assert resp.status_code == 200, resp.text
        for item in resp.json()["items"]:
            if item["kind"] == "approval_request" and item["link"]:
                parsed = urlparse(item["link"])
                approval_id = parsed.path.rsplit("/", 1)[-1]
                return approval_id, parse_qs(parsed.query)["token"][0]
        raise AssertionError("收件匣沒有確認連結")


def _seed(env: Env) -> None:
    with env.session_factory() as db:
        for agent_id, role in [
            ("cs-agent", "assistant"),
            ("admin-agent", "assistant"),
            ("finance-agent", "assistant"),
            ("it-agent", "assistant"),
            ("hr-agent", "assistant"),
            ("main", "employee"),
            ("milim", "simulator"),
        ]:
            db.add(Agent(id=agent_id, display_name=agent_id, role=role))
        db.flush()
        for agent_id in ["cs-agent", "admin-agent", "finance-agent", "it-agent", "hr-agent", "main", "milim"]:
            raw = new_token("fca")
            env.tokens[agent_id] = raw
            db.add(AgentToken(agent_id=agent_id, token_hash=sha256_hex(raw),
                              can_simulate=agent_id == "milim"))
        db.commit()
        import_identities(
            db,
            [
                {"hrm_employee_id": APPROVER, "employee_number": "E001", "display_name": "王經理",
                 "role": "sales_manager", "agent_id": "main", "phone": "0912-345-678"},
                {"hrm_employee_id": "hrm-emp-002", "employee_number": "E002", "display_name": "李主任",
                 "role": "cs_manager"},
            ],
        )
        for username, role in [("admin", "admin"), ("viewer", "viewer")]:
            db.add(User(username=username, role=role, display_name=username,
                        password_hash=hash_password("pw-" + username)))
        db.commit()


@pytest.fixture
def clock() -> FakeClock:
    # 台北時間 2026-10-12（一）10:00
    return FakeClock(datetime(2026, 10, 12, 2, 0, tzinfo=UTC))


@pytest.fixture
def env(tmp_path, clock) -> Env:
    settings = Settings(
        db_url=f"sqlite:///{tmp_path / 'test.db'}",
        public_url="http://controller.test",
        dispatcher="fake",
        scan_interval=0,
    )
    dispatcher = FakeDispatcher(inline=True)
    app = create_app(settings, dispatcher=dispatcher, clock=clock)
    environment = Env(app=app, clock=clock, dispatcher=dispatcher)
    _seed(environment)
    yield environment
    for client in environment.clients.values():
        client.close()
