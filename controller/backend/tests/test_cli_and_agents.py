"""管理指令與 agent 狀態 API。"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.cli import main
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import Agent, AgentToken, Identity, User
from app.security import sha256_hex, verify_password


def _db(db_url):
    return make_session_factory(make_engine(db_url))()


def test_cli_commands(tmp_path, monkeypatch, capsys):
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("FLOW_CONTROLLER_DB_URL", db_url)

    assert main(["init-db"]) == 0
    assert main(["seed-agents"]) == 0
    assert main(["upsert-agent", "milim", "--role", "simulator", "--display-name", "米琳",
                 "--emoji", "🎭"]) == 0
    capsys.readouterr()

    # 發 token：只印一次，資料庫只存 SHA-256
    assert main(["issue-agent-token", "milim", "--can-simulate", "--name", "test"]) == 0
    raw = capsys.readouterr().out.strip()
    assert raw.startswith("fca_")
    assert main(["issue-agent-token", "ghost"]) == 1

    identities = tmp_path / "identities.json"
    identities.write_text(json.dumps([
        {"hrm_employee_id": "u-1", "employee_number": "E001", "display_name": "王經理",
         "role": "sales", "phone": "0912", "address": "台北"},
        {"hrm_employee_id": "u-2", "display_name": "李主任", "agent_id": "main"},
    ]), encoding="utf-8")
    assert main(["import-identities", str(identities)]) == 0
    assert "新增 2 筆" in capsys.readouterr().out
    identities.write_text(json.dumps({"identities": [{"hrm_employee_id": "u-1", "display_name": "王協理"}]}),
                          encoding="utf-8")
    assert main(["import-identities", str(identities)]) == 0
    assert "更新 1 筆" in capsys.readouterr().out

    assert main(["create-user", "boss", "--role", "admin", "--password", "s3cret",
                 "--identity", "u-1"]) == 0
    monkeypatch.setenv("FLOW_CONTROLLER_NEW_PASSWORD", "env-pass")
    assert main(["create-user", "watcher"]) == 0
    assert main(["create-user", "x", "--password", "p", "--identity", "nobody"]) == 1

    with _db(db_url) as db:
        assert {a.id for a in db.scalars(select(Agent))} == {
            "cs-agent", "admin-agent", "finance-agent", "it-agent", "hr-agent", "milim"}
        assert db.get(Agent, "milim").role == "simulator"
        token = db.scalar(select(AgentToken).where(AgentToken.agent_id == "milim"))
        assert token.token_hash == sha256_hex(raw)
        assert token.can_simulate is True
        u1 = db.scalar(select(Identity).where(Identity.hrm_employee_id == "u-1"))
        assert u1.display_name == "王協理"
        assert u1.employee_number == "E001"
        boss = db.scalar(select(User).where(User.username == "boss"))
        assert boss.role == "admin" and boss.identity_id == u1.id
        assert verify_password("s3cret", boss.password_hash)
        watcher = db.scalar(select(User).where(User.username == "watcher"))
        assert watcher.role == "viewer"
        assert verify_password("env-pass", watcher.password_hash)
        assert not verify_password("wrong", watcher.password_hash)

    assert main(["revoke-agent-token", "milim"]) == 0
    assert "已停用 1 個 token" in capsys.readouterr().out


def test_verify_password_rejects_garbage():
    assert not verify_password("x", "plain")
    assert not verify_password("x", "bcrypt$1$2$3$4$5")


def test_agents_api(env):
    flow_id = env.start_demo()["id"]
    viewer = env.user("viewer")
    agents = viewer.get("/api/agents").json()["items"]
    assert [a["id"] for a in agents] == ["admin-agent", "cs-agent", "finance-agent", "hr-agent", "it-agent"]
    admin_agent = next(a for a in agents if a["id"] == "admin-agent")
    assert admin_agent["status"] == "running"
    assert admin_agent["current_steps"][0]["flow_id"] == flow_id
    assert viewer.get("/api/agents", params={"role": "all"}).json()["items"][-1]["id"] in {"milim", "main"}

    env.clock.advance(minutes=2)
    env.report(flow_id, "draft", "admin-agent", output={})
    card = viewer.get("/api/agents/admin-agent").json()
    assert card["status"] == "idle"
    assert card["today"] == {"succeeded": 1, "failed": 0}
    assert card["avg_duration_seconds"] == 120
    assert card["last_seen_at"] is not None
    assert card["steps"][0]["flow_id"] == flow_id
    assert card["steps"][0]["duration_seconds"] == 120
    assert card["steps"][0]["error_kind"] is None
    assert card["gateway"]["status"] in {"ok", "offline", "degraded", "unknown"}
    assert viewer.get("/api/agents/admin-agent", params={"result": "failed"}).json()["steps"] == []
    assert len(viewer.get("/api/agents/admin-agent", params={"result": "succeeded"}).json()["steps"]) == 1
    assert viewer.get("/api/agents/admin-agent", params={"result": "running"}).json()["steps"] == []
    bad = viewer.get("/api/agents/admin-agent", params={"result": "weird"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "invalid_result"
    assert viewer.get("/api/agents/admin-agent", params={"from": "2026-10-13"}).json()["steps"] == []
    assert len(viewer.get("/api/agents/admin-agent", params={"to": "2026-10-12"}).json()["steps"]) == 1
    assert viewer.get("/api/agents/nobody").status_code == 404
    assert env.anon.get("/api/agents").status_code == 401
    assert env.agent("cs-agent").get("/api/agents").status_code == 401


def test_cli_settings_default():
    assert Settings().dispatcher == "openclaw"
