"""測試用的假 CRM（service token）、假 controller、假的共用 skill（flow-start、flow-report）。

cs-agent 每個 skill 的 tests/ 各有一份完全相同的副本（crm-visit-list 的測試檢查一致）。只用標準函式庫。

- 假 CRM：Bearer token；customers 搜尋／新增／context、cases（C3 分類與篩選）、案件內部備註（C4，只讀）、
  follow-ups 新增與篩選（kind、status、due_after、due_before）與分頁；
  寫入要 Idempotency-Key，同鍵同 method＋path＋內容重播第一次的結果，不同就 409（照 CRM 的 idempotency.py）；
  external_message_id 重複回 400；已完成的待辦再 complete 回 400
- 假 controller：POST /api/flows（visit 去重鍵固定 crm:follow-up:<id>；refund 固定 crm:case:<case_id>、輸入白名單）、
  POST /api/flows/{id}/steps/{step}/result（必填 output、draft_reply 的金額檢查、stale_attempt）
- 假共用 skill：寫到暫存目錄的 flow-start/flow-report 腳本，照主 session 定的介面呼叫假 controller
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import unicodedata
import unittest
import urllib.parse
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


class _QuickServer(ThreadingHTTPServer):
    """略過 socket.getfqdn()（macOS 上可能卡很久）。"""

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeServer:
    """route(method, path, query, body, headers) -> (status, body)。

    faults[(method, path)]：依序用掉的錯誤，狀態碼、"drop"（直接斷線）或
    "drop_after"（伺服器已經處理，只是回應遺失）。
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
                if fault in ("drop", "drop_after"):
                    if fault == "drop_after":
                        with server.lock:
                            server.route(method, parsed.path, query, body, self.headers)
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


# --- CRM ----------------------------------------------------------------

CASE_CATEGORIES = ("general", "refund", "compensation", "complaint", "inquiry")
CASE_PRIORITIES = ("low", "normal", "high", "urgent")
NOTE_KINDS = ("assessment", "decision", "internal")


class FakeCrm(FakeServer):
    """照 FreeForJoy CRM（含 C3 案件分類、C4 案件內部備註）的 service credential API。"""

    TOKEN = "crm-cs-token"
    PAGE_SIZE = 2

    def __init__(self):
        self.customers: dict = {}
        self.followups: dict = {}
        self.interactions: list = []
        self.cases: dict = {}
        self.notes: list = []
        self.idem: dict = {}
        # cs-agent 有 case_note:read；設成 False 模擬還沒補發 scope（403）
        self.note_scope = True
        super().__init__()

    # 資料
    def add_customer(self, name: str, external_id: str = "", **extra) -> str:
        cid = str(uuid.uuid4())
        self.customers[cid] = {"id": cid, "name": name, "external_customer_id": external_id,
                               "email": "", "phone": "0912345678", "status": "active", "tags": [],
                               "notes": "", **extra}
        return cid

    def add_followup(self, customer: str, title: str, due_at: str, *, kind: str = "visit",
                     status: str = "pending", hrm: str | None = "hrm-sales-1", case: str | None = None) -> str:
        fid = str(uuid.uuid4())
        self.followups[fid] = {"id": fid, "customer": customer, "case": case, "title": title,
                               "kind": kind, "due_at": due_at, "status": status, "assignee": 7,
                               "assignee_hrm_employee_id": hrm, "completed_at": None}
        return fid

    def add_case(self, customer: str, subject: str = "舊案件", *, category: str = "general",
                 priority: str = "normal", status: str = "open") -> str:
        cid = str(uuid.uuid4())
        self.cases[cid] = {"id": cid, "customer": customer, "subject": subject, "description": "",
                           "category": category, "legacy_category": "", "priority": priority,
                           "status": status, "assignee": None}
        return cid

    def add_note(self, case: str, kind: str, body: str) -> str:
        nid = str(uuid.uuid4())
        self.notes.append({"id": nid, "case": case, "kind": kind, "body": body, "author_type": "service",
                           "author_username": "finance-agent", "author_credential": "OpenClaw finance-agent",
                           "created_at": f"2026-10-12T02:00:{len(self.notes):02d}+00:00"})
        return nid

    # 路由
    def route(self, method, path, query, body, headers):
        if headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return 401, {"detail": "Invalid token"}
        if method == "GET":
            return self._get(path, query)
        key = headers.get("Idempotency-Key")
        if not key or len(key) > 128:
            return 400, {"Idempotency-Key": ["Required, maximum 128 characters"]}
        payload_hash = hashlib.sha256(
            json.dumps(body or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record = self.idem.get(key)
        if record:
            if record[:3] != (method, path, payload_hash):
                return 409, {"detail": "Idempotency key was already used for a different request"}
            return record[3], record[4]
        status, resp = self._post(path, body or {})
        if status < 400:
            self.idem[key] = (method, path, payload_hash, status, resp)
        return status, resp

    def _page(self, rows, query, path):
        offset = int(query.get("offset", 0))
        page = rows[offset:offset + self.PAGE_SIZE]
        nxt = None
        if offset + self.PAGE_SIZE < len(rows):
            q = dict(query, offset=str(offset + self.PAGE_SIZE))
            nxt = f"{self.url}/api{path}?{urllib.parse.urlencode(q)}"
        return {"count": len(rows), "next": nxt, "previous": None, "results": page}

    def _context(self, cid):
        customer = self.customers.get(cid)
        if not customer:
            return 404, {"detail": "Not found."}
        interactions = [r for r in self.interactions if r["customer"] == cid][::-1][:20]
        open_cases = [r for r in self.cases.values()
                      if r["customer"] == cid and r["status"] not in ("resolved", "closed")][:20]
        pending = [r for r in self.followups.values() if r["customer"] == cid and r["status"] == "pending"][:20]
        # 內部備註（C4）不會出現在 context
        return 200, {"customer": customer, "interactions": interactions, "open_cases": open_cases,
                     "pending_followups": pending}

    def _get(self, path, query):
        parts = path.strip("/").split("/")  # ["api", "v1", <resource>, <id>?, <sub>?]
        resource = parts[2] if len(parts) > 2 else ""
        rid = parts[3] if len(parts) > 3 else None
        sub = parts[4] if len(parts) > 4 else None
        if resource == "follow-ups" and rid is None:
            rows = sorted(self.followups.values(), key=lambda r: r["due_at"])
            for field in ("kind", "status", "customer"):
                if field in query:
                    rows = [r for r in rows if r[field] == query[field]]
            if "due_after" in query:
                rows = [r for r in rows if parse_dt(r["due_at"]) >= parse_dt(query["due_after"])]
            if "due_before" in query:
                rows = [r for r in rows if parse_dt(r["due_at"]) < parse_dt(query["due_before"])]
            return 200, self._page(rows, query, "/v1/follow-ups/")
        if resource == "follow-ups":
            row = self.followups.get(rid)
            return (200, row) if row else (404, {"detail": "Not found."})
        if resource == "customers" and rid is None:
            term = (query.get("search") or "").lower()
            rows = [r for r in self.customers.values()
                    if not term or any(term in str(r.get(f) or "").lower()
                                       for f in ("name", "email", "phone", "external_customer_id"))]
            return 200, self._page(rows, query, "/v1/customers/")
        if resource == "customers" and sub == "context":
            return self._context(rid)
        if resource == "customers":
            row = self.customers.get(rid)
            return (200, row) if row else (404, {"detail": "Not found."})
        if resource == "interactions":
            rows = [r for r in self.interactions if r["customer"] == query.get("customer", r["customer"])]
            return 200, self._page(rows, query, "/v1/interactions/")
        if resource == "cases" and rid is None:
            if query.get("category") and query["category"] not in CASE_CATEGORIES:
                return 400, {"category": ["Invalid"]}
            if query.get("priority") and query["priority"] not in CASE_PRIORITIES:
                return 400, {"priority": ["Invalid"]}
            rows = list(self.cases.values())
            for field in ("customer", "status", "category", "priority"):
                if field in query:
                    rows = [r for r in rows if r[field] == query[field]]
            return 200, self._page(rows, query, "/v1/cases/")
        if resource == "cases" and sub == "notes":
            if not self.note_scope:
                return 403, {"detail": "You do not have permission to perform this action."}
            if rid not in self.cases:
                return 404, {"detail": "Not found."}
            if query.get("kind") and query["kind"] not in NOTE_KINDS:
                return 400, {"kind": ["Invalid"]}
            rows = [n for n in self.notes if n["case"] == rid and n["kind"] == query.get("kind", n["kind"])]
            return 200, {"count": len(rows), "results": rows}
        if resource == "cases":
            row = self.cases.get(rid)
            return (200, row) if row else (404, {"detail": "Not found."})
        return 404, {"detail": "not found"}

    def _post(self, path, body):
        if path == "/api/v1/customers/":
            if not (body.get("name") or "").strip():
                return 400, {"name": ["This field may not be blank."]}
            if set(body) & {"status", "owner"}:
                return 400, {"detail": "Agents cannot set status or owner"}
            ext = body.get("external_customer_id") or ""
            if ext and any(r["external_customer_id"] == ext for r in self.customers.values()):
                return 400, {"external_customer_id": ["External customer ID already exists"]}
            cid = self.add_customer(body["name"], ext, email=body.get("email") or "",
                                    phone=body.get("phone") or "", tags=body.get("tags") or [],
                                    notes=body.get("notes") or "")
            return 201, self.customers[cid]
        if path == "/api/v1/interactions/":
            if body.get("customer") not in self.customers:
                return 400, {"customer": ["Invalid pk"]}
            ext = body.get("external_message_id") or ""
            if ext and any(r["external_message_id"] == ext for r in self.interactions):
                return 400, {"external_message_id": ["External message ID already exists"]}
            row = {"id": str(uuid.uuid4()), "customer": body["customer"], "summary": body["summary"],
                   "external_message_id": ext, "occurred_at": "2026-10-15T18:00:00+08:00"}
            self.interactions.append(row)
            return 201, row
        if path == "/api/v1/cases/":
            if body.get("customer") not in self.customers:
                return 400, {"customer": ["Invalid pk"]}
            if not (body.get("subject") or "").strip():
                return 400, {"subject": ["This field may not be blank."]}
            category = body.get("category", "general")
            if category not in CASE_CATEGORIES:
                return 400, {"category": [f'"{category}" is not a valid choice.']}
            if body.get("priority", "normal") not in CASE_PRIORITIES:
                return 400, {"priority": ["Invalid"]}
            if "assignee" in body or "status" in body:
                return 400, {"detail": "Agents cannot set assignee"}
            cid = self.add_case(body["customer"], body["subject"], category=category,
                                priority=body.get("priority", "normal"))
            self.cases[cid]["description"] = body.get("description") or ""
            return 201, self.cases[cid]
        if path == "/api/v1/follow-ups/":
            if body.get("customer") not in self.customers:
                return 400, {"customer": ["Invalid pk"]}
            if body.get("kind", "general") not in ("general", "visit"):
                return 400, {"kind": ["Invalid"]}
            case = body.get("case")
            if case and (case not in self.cases or self.cases[case]["customer"] != body["customer"]):
                return 400, {"case": "Case must belong to the customer"}
            if not body.get("title") or not body.get("due_at"):
                return 400, {"due_at": ["Invalid"]}
            fid = self.add_followup(body["customer"], body["title"], body["due_at"],
                                    kind=body.get("kind", "general"), hrm=None, case=case)
            self.followups[fid]["assignee"] = None
            return 201, self.followups[fid]
        if path.startswith("/api/v1/follow-ups/") and path.endswith("/complete/"):
            fid = path.split("/")[4]
            row = self.followups.get(fid)
            if not row:
                return 404, {"detail": "Not found."}
            if row["status"] == "done":
                return 400, ["Follow-up is already complete"]
            row["status"] = "done"
            row["completed_at"] = "2026-10-15T18:00:00+08:00"
            return 200, dict(row)
        return 404, {"detail": "not found"}


# --- controller ----------------------------------------------------------

# 和 controller app/flows/refund.py 的 amounts_in 相同（fake 自己一份，不 import skill 的版本）
_AMOUNT_IN_TEXT = re.compile(r"(?:NT\$|NTD|新台幣|台幣|\$)\s*([\d,]+)|([\d,]+)\s*(?:元|塊)")


def controller_amounts(text: str) -> list:
    found = []
    for match in _AMOUNT_IN_TEXT.finditer(unicodedata.normalize("NFKC", text or "")):
        digits = (match.group(1) or match.group(2) or "").replace(",", "")
        if digits.isdigit():
            found.append(int(digits))
    return found


class FakeController(FakeServer):
    TOKEN = "ctl-cs-token"
    # 照 controller/backend/app/flows/visit.py、refund.py 的 outputs（必填欄位）
    REQUIRED_OUTPUTS = {
        "close_visit": ("interaction_id", "follow_up_id", "follow_up_status"),
        "draft_reply": ("reply_text",),
        "log_reply": ("interaction_id", "follow_up_id"),
    }
    # 照 refund.py validate_input 的白名單
    REFUND_FIELDS = ("case_id", "customer_id", "external_customer_id", "customer_name", "category", "summary")

    def __init__(self):
        self.flows: dict = {}
        self.reports: list = []
        # (flow_id, step) → controller 目前的 attempt；回報帶過期的 attempt 回 409 stale_attempt
        self.current_attempts: dict = {}
        # flow_id → {"decision", "amount_twd"}：draft_reply 回報時照 refund.py 檢查金額
        self.refund_decisions: dict = {}
        self.next_id = 1
        super().__init__()

    def _start(self, body):
        flow_type = body.get("type")
        data = body.get("input") or {}
        if flow_type == "visit":
            for field in ("follow_up_id", "customer", "visit_start_at", "visit_end_at",
                          "location", "assignee_hrm_employee_id"):
                if not data.get(field):
                    return 422, {"detail": {"code": "invalid_input", "message": f"{field} 必填"}}
            dedupe = f"crm:follow-up:{data['follow_up_id']}"
        elif flow_type == "refund":
            if data.get("category") not in ("refund", "compensation"):
                return 422, {"detail": {"code": "invalid_input",
                                        "message": "category 必須是 refund 或 compensation"}}
            for field in ("case_id", "customer_id", "summary"):
                if not data.get(field):
                    return 422, {"detail": {"code": "invalid_input", "message": f"{field} 必填"}}
            data = {k: data.get(k) for k in self.REFUND_FIELDS}
            dedupe = f"crm:case:{data['case_id']}"
        else:
            return 422, {"detail": {"code": "unknown_flow_type", "message": str(flow_type)}}
        for flow in self.flows.values():
            if flow["dedupe_key"] == dedupe:
                return 200, {**flow, "created": False}
        fid = f"fl_{self.next_id:016x}"
        self.next_id += 1
        flow = {"id": fid, "type": flow_type, "status": "running", "dedupe_key": dedupe,
                "input": data, "raw_input": body.get("input")}
        self.flows[fid] = flow
        return 201, {**flow, "created": True}

    def _check_reply(self, flow_id, output):
        text = output.get("reply_text")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            return "reply_text 不能是空的或超過 1000 字"
        rule = self.refund_decisions.get(flow_id)
        if rule is None:
            return None
        amounts = controller_amounts(text)
        if rule["decision"] == "approve":
            wrong = [a for a in amounts if a != rule["amount_twd"]]
            if wrong:
                return f"回覆裡的金額 {wrong} 和主管核定的不一致"
        elif amounts:
            return "這次不退款，回覆不得出現任何金額"
        return None

    def route(self, method, path, query, body, headers):
        if headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return 401, {"detail": {"code": "unauthorized"}}
        if method == "POST" and path == "/api/flows":
            return self._start(body or {})
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 6 and parts[:2] == ["api", "flows"] and parts[3] == "steps":
            required = self.REQUIRED_OUTPUTS.get(parts[4], ())
            output = (body or {}).get("output") or {}
            missing = [f for f in required if output.get(f) is None]
            if body.get("status") == "ok" and missing:
                return 422, {"detail": {"code": "missing_output_fields", "message": ",".join(missing)}}
            if body.get("status") == "ok" and parts[4] == "draft_reply":
                problem = self._check_reply(parts[2], output)
                if problem:
                    return 422, {"detail": {"code": "invalid_output", "message": problem}}
            current = self.current_attempts.get((parts[2], parts[4]))
            if current is not None and body.get("attempt") is not None and body["attempt"] != current:
                return 409, {"detail": {"code": "stale_attempt", "message": f"目前 attempt {current}"}}
            self.reports.append({"flow_id": parts[2], "step": parts[4], **(body or {})})
            return 200, {"id": parts[2], "status": "running"}
        return 404, {"detail": {"code": "not_found"}}


# --- 假的共用 skill（照主 session 定的呼叫介面）---------------------------

FLOW_START = r'''#!/usr/bin/env python3
import argparse, json, os, sys, urllib.request, urllib.error
p = argparse.ArgumentParser()
p.add_argument("--type", required=True)
p.add_argument("--input-file", required=True)
p.add_argument("--dedupe-key")
p.add_argument("--agent")
a = p.parse_args()
data = json.load(sys.stdin) if a.input_file == "-" else json.load(open(a.input_file))
agent = a.agent or os.environ.get("OPENCLAW_AGENT_ID")
token = os.environ["FLOW_CONTROLLER_TOKEN_" + agent.upper().replace("-", "_")]
body = {"type": a.type, "input": data}
if a.dedupe_key:
    body["dedupe_key"] = a.dedupe_key
req = urllib.request.Request(os.environ["FLOW_CONTROLLER_URL"].rstrip("/") + "/api/flows",
                             data=json.dumps(body).encode(), method="POST",
                             headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read())
except urllib.error.HTTPError as e:
    print(json.dumps({"ok": False, "error": e.read().decode()}, ensure_ascii=False))
    sys.exit(1)
print(json.dumps({"ok": True, "flow_id": out["id"], "created": out["created"]}))
'''

FLOW_REPORT = r'''#!/usr/bin/env python3
import argparse, json, os, sys, urllib.request, urllib.error
p = argparse.ArgumentParser()
p.add_argument("--flow", required=True)
p.add_argument("--step", required=True)
p.add_argument("--status", required=True, choices=["ok", "failed", "needs_human"])
p.add_argument("--error-kind", choices=["business", "system"])
p.add_argument("--summary", required=True)
p.add_argument("--output-file", required=True)
p.add_argument("--attempt", type=int)
p.add_argument("--error")
p.add_argument("--agent")
a = p.parse_args()
output = json.load(sys.stdin) if a.output_file == "-" else json.load(open(a.output_file))
agent = a.agent or os.environ.get("OPENCLAW_AGENT_ID")
token = os.environ["FLOW_CONTROLLER_TOKEN_" + agent.upper().replace("-", "_")]
body = {"status": a.status, "summary": a.summary, "output": output}
if a.error_kind:
    body["error_kind"] = a.error_kind
if a.attempt is not None:
    body["attempt"] = a.attempt
if os.environ.get("FAKE_FLOW_REPORT_FAIL"):
    print(json.dumps({"ok": False, "error": "boom"}))
    sys.exit(1)
url = "%s/api/flows/%s/steps/%s/result" % (os.environ["FLOW_CONTROLLER_URL"].rstrip("/"), a.flow, a.step)
req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                             headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read())
except urllib.error.HTTPError as e:
    print(json.dumps({"ok": False, "error": e.read().decode()}))
    sys.exit(1)
print(json.dumps({"ok": True, "flow_id": a.flow, "step": a.step, "flow_status": out["status"]}))
'''


def load_script(path: str, name: str):
    """載入 skill 腳本（各 skill 的 scripts 目錄有自己的 ffj_common、cs_common）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SkillTestCase(unittest.TestCase):
    """起假 CRM、假 controller、假共用 skill；環境變數指向它們；重試不等待。"""

    SCRIPT: str = ""
    MODULE: str = ""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_script(cls.SCRIPT, cls.MODULE)
        cls.c = sys.modules["ffj_common"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cs-skill-test-")
        skills_dir = os.path.join(self.tmp, "skills")
        for skill, script, code in (("flow-start", "flow_start.py", FLOW_START),
                                    ("flow-report", "flow_report.py", FLOW_REPORT)):
            os.makedirs(os.path.join(skills_dir, skill, "scripts"))
            with open(os.path.join(skills_dir, skill, "scripts", script), "w", encoding="utf-8") as fh:
                fh.write(code)
        self.crm = FakeCrm()
        self.ctl = FakeController()
        self.addCleanup(self.crm.stop)
        self.addCleanup(self.ctl.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {
            "FREEFORJOY_ENV_FILE": "",
            "CRM_API_BASE": f"{self.crm.url}/api/v1",
            "CRM_API_TOKEN": FakeCrm.TOKEN,
            "FLOW_CONTROLLER_URL": self.ctl.url,
            "FLOW_CONTROLLER_TOKEN_CS_AGENT": FakeController.TOKEN,
            "FLOW_SKILLS_DIR": skills_dir,
            "OPENCLAW_AGENT_ID": "cs-agent",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("FAKE_FLOW_REPORT_FAIL",):
            os.environ.pop(name, None)
        sleep = mock.patch.object(self.c, "SLEEP", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_script(self, *argv, stdin: str | None = None) -> tuple:
        """在同一個行程裡執行 main()，回傳 (exit code, 輸出 JSON)。"""
        out = io.StringIO()
        code = 0
        stdin_patch = mock.patch.object(sys, "stdin", io.StringIO(stdin or ""))
        with stdin_patch, contextlib.redirect_stdout(out):
            try:
                self.mod.main(list(argv))
            except SystemExit as exc:
                code = exc.code or 0
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        return code, json.loads(lines[-1])
