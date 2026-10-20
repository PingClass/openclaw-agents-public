"""測試用的假 HRM（service token）與假 controller；共用 skill 用 repo 裡真正的 flow-start／flow-report。

onboarding-list、onboarding-notice 的 tests/ 各有一份完全相同的副本。只用標準函式庫。

- 假 HRM（照 HRM docs/agent-service-api.md）：Bearer token；scope 檢查；
  `GET /api/employees/?start_date_after&start_date_before&status`（含邊界、分頁、只回 7 個欄位）；
  `GET /api/departments/{id}/managers/`（不分頁，沒有主管回 []）；任何寫入 403
- 假 controller（照 controller/backend/app/flows/onboarding.py）：`POST /api/flows`（只有 hr-agent 能啟動、
  去重鍵固定 hrm:employee:<id>、必填欄位檢查）、`POST /api/flows/{id}/steps/{step}/result`
  （onboarding_notice 的必填 output、stale attempt）
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", ".."))
REPO_SKILLS = os.path.join(REPO, "skills")


class _QuickServer(ThreadingHTTPServer):
    """略過 socket.getfqdn()（macOS 上可能卡很久）。"""

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class FakeServer:
    """route(method, path, query, body, headers) -> (status, body)。

    faults[(method, path)]：依序用掉的錯誤，狀態碼或 "drop"（直接斷線）。
    """

    def __init__(self):
        self.calls: list = []
        self.faults: dict = {}
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _handle(self, method):
                parsed = urllib.parse.urlsplit(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                with server.lock:
                    server.calls.append((method, parsed.path, query, body, dict(self.headers)))
                    queued = server.faults.get((method, parsed.path))
                    fault = queued.pop(0) if queued else None
                if fault == "drop":
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                if fault is not None:
                    status, payload = fault, {"detail": "injected"}
                else:
                    with server.lock:
                        status, payload = server.route(method, parsed.path, query, body, self.headers)
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.httpd = _QuickServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def count(self, method: str, path: str) -> int:
        return sum(1 for call in self.calls if call[0] == method and call[1] == path)

    def route(self, method, path, query, body, headers):  # noqa: ANN001
        return 404, {"detail": "not found"}


# --- HRM ----------------------------------------------------------------


class FakeHrm(FakeServer):
    TOKEN = "hrm_test_token"
    SCOPES = {"employee:read", "department:read"}
    PAGE_SIZE = 2

    def __init__(self):
        self.employees: list = []
        self.departments: dict = {}   # id -> {"id", "code", "name"}
        self.managers: dict = {}      # department id -> [manager]
        super().__init__()

    def add_department(self, dept_id: str, name: str, managers: list | None = None):
        self.departments[dept_id] = {"id": dept_id, "code": dept_id.upper(), "name": name, "is_active": True}
        self.managers[dept_id] = managers or []

    def add_employee(self, emp_id: str, name: str, number: str, dept_id: str | None, start_date: str | None,
                     status: str = "active"):
        self.employees.append({
            "id": emp_id, "employee_number": number, "full_name": name, "department": dept_id,
            "department_name": self.departments[dept_id]["name"] if dept_id else None,
            "start_date": start_date, "employment_type": "full_time" if start_date else None,
            "_status": status, "_phone": "0912-345-678",   # 真的 HRM token 不會回電話
        })

    def route(self, method, path, query, body, headers):
        if headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return 401, {"error": {"status": 401, "details": {"detail": "Invalid token."}}}
        if method != "GET":
            return 403, {"error": {"status": 403, "details": {"detail": "read only"}}}
        if path == "/api/employees/":
            rows = list(self.employees)
            for key in ("start_date_after", "start_date_before"):
                if key in query:
                    try:
                        date.fromisoformat(query[key])
                    except ValueError:
                        return 400, {"error": {"status": 400, "details": {key: ["Enter a valid date."]}}}
            if "start_date_after" in query:
                rows = [r for r in rows if r["start_date"] and r["start_date"] >= query["start_date_after"]]
            if "start_date_before" in query:
                rows = [r for r in rows if r["start_date"] and r["start_date"] <= query["start_date_before"]]
            if "status" in query:
                rows = [r for r in rows if r["_status"] == query["status"]]
            rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
            page = int(query.get("page", 1))
            chunk = rows[(page - 1) * self.PAGE_SIZE: page * self.PAGE_SIZE]
            nxt = None
            if page * self.PAGE_SIZE < len(rows):
                nxt = f"{self.url}/api/employees/?{urllib.parse.urlencode(dict(query, page=page + 1))}"
            return 200, {"count": len(rows), "next": nxt, "previous": None, "results": chunk}
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "departments"] and parts[3] == "managers":
            dept = urllib.parse.unquote(parts[2])
            if dept not in self.departments:
                return 404, {"error": {"status": 404, "details": {"detail": "Not found."}}}
            return 200, self.managers[dept]
        return 403, {"error": {"status": 403, "details": {"detail": "no scope"}}}


# --- controller ----------------------------------------------------------


class FakeController(FakeServer):
    TOKEN = "ctl-hr-token"
    REQUIRED_INPUT = ("employee_id", "name", "employee_number", "department_id", "department_name",
                      "start_date")
    REQUIRED_OUTPUTS = {"onboarding_notice": ("manager_hrm_employee_ids", "checklist")}

    def __init__(self):
        self.flows: dict = {}
        self.reports: list = []
        self.current_attempts: dict = {}
        self.next_id = 1
        super().__init__()

    def route(self, method, path, query, body, headers):
        if headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return 401, {"detail": {"code": "unauthorized", "message": "token 無效"}}
        if method == "POST" and path == "/api/flows":
            if body.get("type") != "onboarding":
                return 403, {"detail": {"code": "not_allowed_to_start", "message": "x"}}
            data = body.get("input") or {}
            for field in self.REQUIRED_INPUT:
                if data.get(field) in (None, ""):
                    return 422, {"detail": {"code": "invalid_input", "message": f"{field} 必填"}}
            dedupe = f"hrm:employee:{data['employee_id']}"   # 固定，不採用呼叫端的鍵
            for flow in self.flows.values():
                if flow["dedupe_key"] == dedupe:
                    return 200, {**flow, "created": False}
            fid = f"fl_{self.next_id:016x}"
            self.next_id += 1
            flow = {"id": fid, "type": "onboarding", "status": "running", "dedupe_key": dedupe,
                    "title": f"到職 {data['name']}", "input": data, "client_dedupe_key": body.get("dedupe_key")}
            self.flows[fid] = flow
            return 201, {**flow, "created": True}
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 6 and parts[:2] == ["api", "flows"] and parts[3] == "steps":
            output = (body or {}).get("output") or {}
            required = self.REQUIRED_OUTPUTS.get(parts[4], ())
            missing = [f for f in required if output.get(f) is None]
            if body.get("status") == "ok" and missing:
                return 422, {"detail": {"code": "missing_output_fields", "message": ",".join(missing)}}
            current = self.current_attempts.get((parts[2], parts[4]))
            if current is not None and body.get("attempt") is not None and body["attempt"] != current:
                return 409, {"detail": {"code": "stale_attempt", "message": f"目前 attempt {current}"}}
            self.reports.append({"flow_id": parts[2], "step": parts[4], **(body or {})})
            return 200, {"id": parts[2], "status": "running", "current_step": "confirm_manager"}
        return 404, {"detail": {"code": "not_found", "message": "x"}}


def load_script(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SkillTestCase(unittest.TestCase):
    """起假 HRM、假 controller；環境變數指向它們；共用 skill 用 repo 裡的版本；重試不等待。"""

    SCRIPT: str = ""
    MODULE: str = ""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_script(cls.SCRIPT, cls.MODULE)
        cls.c = sys.modules["ffj_common"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hr-skill-test-")
        self.hrm = FakeHrm()
        self.ctl = FakeController()
        self.addCleanup(self.hrm.stop)
        self.addCleanup(self.ctl.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {
            "FREEFORJOY_ENV_FILE": "",
            "HRM_API_BASE": f"{self.hrm.url}/api",
            "HRM_API_TOKEN": FakeHrm.TOKEN,
            "FLOW_CONTROLLER_URL": self.ctl.url,
            "FLOW_CONTROLLER_TOKEN_HR_AGENT": FakeController.TOKEN,
            "FLOW_SKILLS_DIR": REPO_SKILLS,
            "FLOW_SKILL_RETRY_DELAYS": "0,0,0",
            "OPENCLAW_AGENT_ID": "hr-agent",
            "OPENCLAW_STATE_DIR": os.path.join(self.tmp, "state"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(self.c, "SLEEP", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_script(self, *argv, stdin: str | None = None) -> tuple:
        """在同一個行程裡執行 main()，回傳 (exit code, 輸出 JSON)。"""
        out = io.StringIO()
        code = 0
        with mock.patch.object(sys, "stdin", io.StringIO(stdin or "")), contextlib.redirect_stdout(out):
            try:
                self.mod.main(list(argv))
            except SystemExit as exc:
                code = exc.code or 0
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"應該只輸出一行 JSON：{out.getvalue()}")
        return code, json.loads(lines[0])
