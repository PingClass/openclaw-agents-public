"""OpenClaw gateway 健康狀態（/readyz）、Identity 的部門欄位與補欄位、deploy/identities.json。"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.db import init_db, make_engine, make_session_factory
from app.gateway import GatewayMonitor, interpret, urllib_fetch
from app.models import Agent, Identity
from app.sanitize import is_sensitive_key

IDENTITIES_FILE = Path(__file__).resolve().parents[2] / "deploy" / "identities.json"


def _check(env, fetch):
    gateway = env.app.state.engine.gateway
    gateway.fetch = fetch
    return asyncio.run(gateway.check())


def _offline(url, timeout):
    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


def test_summary_shows_gateway_offline(env, clock):
    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["gateway"]["status"] == "unknown"

    seen = {}

    def offline(url, timeout):
        seen.update(url=url, timeout=timeout)
        return _offline(url, timeout)

    snapshot = _check(env, offline)
    assert seen == {"url": "http://127.0.0.1:18789/readyz", "timeout": 3.0}
    assert snapshot["status"] == "offline"
    summary = env.user("viewer").get("/api/stats/summary").json()
    assert summary["gateway"]["status"] == "offline"
    assert summary["gateway"]["message"].startswith("gateway 離線")
    assert summary["gateway"]["checked_at"] == "2026-10-12T02:00:00Z"
    agents = env.user("viewer").get("/api/agents").json()
    assert agents["gateway"]["status"] == "offline"


def test_gateway_degraded_and_ok(env):
    snapshot = _check(env, lambda url, timeout: (503, '{"ready": false, "failing": ["line"]}'))
    assert snapshot["status"] == "degraded"
    assert snapshot["message"] == "頻道異常：line"
    assert env.user("viewer").get("/api/agents").json()["gateway"]["failing"] == ["line"]

    snapshot = _check(env, lambda url, timeout: (200, '{"ready": true, "failing": []}'))
    assert snapshot == {"status": "ok", "message": "gateway 正常", "failing": [],
                        "checked_at": "2026-10-12T02:00:00Z"}

    def slow(url, timeout):
        raise TimeoutError("timed out")

    assert _check(env, slow)["status"] == "offline"


@pytest.mark.parametrize(
    ("code", "body", "status", "message"),
    [
        (200, '{"ready": true, "failing": []}', "ok", "gateway 正常"),
        (200, "", "ok", "gateway 正常"),
        (200, '{"ready": true, "failing": [{"id": "discord"}]}', "degraded", "頻道異常：discord"),
        (503, "not json", "degraded", "頻道異常：HTTP 503"),
        (200, '{"ready": false}', "degraded", "頻道異常：HTTP 200"),
        (200, "[]", "ok", "gateway 正常"),
    ],
)
def test_interpret_readyz(code, body, status, message):
    result = interpret(code, body)
    assert result["status"] == status
    assert result["message"] == message


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_urllib_fetch_against_real_http_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code = 200 if self.path == "/readyz" else 503
            body = json.dumps({"ready": code == 200, "failing": [] if code == 200 else ["line"]}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert urllib_fetch(f"http://127.0.0.1:{port}/readyz", 3)[0] == 200
        assert urllib_fetch(f"http://127.0.0.1:{port}/other", 3)[0] == 503
        from datetime import UTC, datetime

        monitor = GatewayMonitor(url=f"http://127.0.0.1:{port}", timeout=3,
                                 clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
        assert asyncio.run(monitor.check())["status"] == "ok"
    finally:
        server.shutdown()
    # 連不上：gateway 離線
    monitor = GatewayMonitor(url=f"http://127.0.0.1:{_free_port()}", timeout=1,
                             clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    assert asyncio.run(monitor.check())["status"] == "offline"


def test_gateway_run_loop_checks_periodically():
    from datetime import UTC, datetime

    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("意外錯誤")  # run 迴圈要吞掉例外繼續
        return 200, "{}"

    monitor = GatewayMonitor(url="http://gw", timeout=1, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
                             fetch=fetch)

    async def run():
        task = asyncio.create_task(monitor.run(0.01))
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 2
    assert monitor.snapshot()["status"] == "ok"


def test_gateway_protocol_error_counts_as_offline():
    """port 被別的服務佔用、回應不是 HTTP（BadStatusLine 等）也要顯示離線，不能停在舊狀態。"""
    import http.client
    from datetime import UTC, datetime

    def fetch(url, timeout):
        raise http.client.BadStatusLine("garbage")

    monitor = GatewayMonitor(url="http://gw", timeout=1, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
                             fetch=fetch)
    assert asyncio.run(monitor.check())["status"] == "offline"


# ---------------------------------------------------------------- Identity


def test_identities_file_has_seven_agents_without_personal_data():
    records = json.loads(IDENTITIES_FILE.read_text(encoding="utf-8"))["identities"]
    assert len(records) == 7
    assert {r["agent_id"] for r in records} == {
        "mjolmire-businessman", "gobta", "main", "hinata", "souei", "luminous", "shion"}
    for record in records:
        assert record["employee_number"].startswith("AGT-")
        assert set(record) == {"hrm_employee_id", "employee_number", "display_name", "role", "department",
                               "agent_id"}
        assert not any(is_sensitive_key(key) for key in record)
    by_agent = {r["agent_id"]: r for r in records}
    assert by_agent["main"]["role"] == "manager" and by_agent["main"]["department"] == "業務部"
    assert by_agent["hinata"]["role"] == "manager" and by_agent["hinata"]["department"] == "客服部"
    assert by_agent["luminous"]["role"] == "finance"
    assert by_agent["shion"]["role"] == "hr"
    assert by_agent["mjolmire-businessman"]["hrm_employee_id"] == "da1b72d2-89cb-4879-9c50-73e03a1ae993"


def test_import_identities_with_agents_cli(tmp_path, monkeypatch, capsys):
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("FLOW_CONTROLLER_DB_URL", db_url)
    assert cli_main(["upsert-agent", "main", "--role", "employee", "--display-name", "既有名稱"]) == 0
    assert cli_main(["import-identities", str(IDENTITIES_FILE), "--with-agents"]) == 0
    assert "新增 7 筆，更新 0 筆；員工 agent 7 隻" in capsys.readouterr().out
    assert cli_main(["import-identities", str(IDENTITIES_FILE)]) == 0
    assert "新增 0 筆，更新 7 筆" in capsys.readouterr().out
    factory = make_session_factory(make_engine(db_url))
    with factory() as db:
        agents = {a.id: a for a in db.scalars(select(Agent))}
        assert set(agents) == {"mjolmire-businessman", "gobta", "main", "hinata", "souei", "luminous", "shion"}
        assert all(a.role == "employee" for a in agents.values())
        assert agents["main"].display_name == "既有名稱"
        assert agents["gobta"].display_name == "Gobta"
        luminous = db.scalar(select(Identity).where(Identity.agent_id == "luminous"))
        assert luminous.department == "管理部"


def test_init_db_adds_department_column_to_existing_table(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    # 骨架版本（PR #1）的 identities 表，沒有 department
    conn.execute(
        "CREATE TABLE identities (id INTEGER PRIMARY KEY, hrm_employee_id VARCHAR(64) UNIQUE, "
        "employee_number VARCHAR(32), display_name VARCHAR(100), line_user_id VARCHAR(64), "
        "adm_user_id VARCHAR(64), crm_user_id VARCHAR(64), role VARCHAR(64), agent_id VARCHAR(64), "
        "created_at DATETIME, updated_at DATETIME)"
    )
    conn.execute("INSERT INTO identities (hrm_employee_id, display_name, role) VALUES ('u-1', '舊資料', 'sales')")
    conn.commit()
    conn.close()

    engine = make_engine(f"sqlite:///{path}")
    assert init_db(engine) == ["identities.department", "identities.confirm_mode"]
    assert init_db(engine) == []  # 重跑不會再加
    with make_session_factory(engine)() as db:
        identity = db.scalar(select(Identity).where(Identity.hrm_employee_id == "u-1"))
        assert identity.display_name == "舊資料"
        assert identity.department == ""
        # 既有的確認者一律是 agent 代理
        assert identity.confirm_mode == "agent"


def test_init_db_cli_reports_added_columns(tmp_path, monkeypatch, capsys):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE identities (id INTEGER PRIMARY KEY, hrm_employee_id VARCHAR(64))")
    conn.commit()
    conn.close()
    monkeypatch.setenv("FLOW_CONTROLLER_DB_URL", f"sqlite:///{path}")
    assert cli_main(["init-db"]) == 0
    assert "identities.department" in capsys.readouterr().out
