"""測試用的假 CRM 與假 controller（只用標準函式庫的 http.server）。

行為照真實系統的重點規則：
- CRM（FreeForJoy/crm 分支 feat/C3-C4-case-category-notes 的 docs/openclaw-api.md「案件內部備註（C4）」）：
  Bearer service token、scope 檢查（403）、跨 workspace／不存在 404；
  `POST /api/v1/cases/{id}/notes/` 必須帶 Idempotency-Key（缺 400），同鍵同內容重播原本的 201、
  同鍵不同內容 409；kind 不合法 400；備註依 created_at 由舊到新；備註不會出現在 context／互動紀錄。
- controller（controller/backend/app/flows/refund.py）：`GET /api/flows/{id}` 給寫入前核對；
  `POST /api/flows/{id}/steps/{step}/result` 檢查必填 output（422 missing_output_fields），
  assess_refund 的 amount_is_estimate 不是 true、金額不是 0～1,000,000 整數時 422 invalid_output。
"""

from __future__ import annotations

import json
import socketserver
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _QuickServer(ThreadingHTTPServer):
    """HTTPServer.server_bind 會呼叫 socket.getfqdn()，在 macOS 上可能卡 30 秒以上；這裡略過。"""

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class FakeServer:
    """route(method, path, query, body, headers) -> (status, body)。"""

    def __init__(self):
        self.calls: list = []
        # 依 (method, path) 注入錯誤：值是列表，依序用掉；整數＝回該狀態碼，
        # "drop"＝直接斷線（沒處理），"drop_after"＝處理完才斷線（回應遺失）
        self.faults: dict = {}
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - 測試時保持安靜
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
                data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_PATCH(self):  # noqa: N802
                self._handle("PATCH")

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

NOTE_KINDS = ("assessment", "decision", "internal")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeCrm(FakeServer):
    """假 CRM 的 /api/v1（客戶 context、案件、案件內部備註）。"""

    def __init__(self, tokens: dict):
        # tokens: token -> {"name": credential 名稱, "username", "scopes": set, "workspace"}
        self.tokens = tokens
        self.customers: dict = {}  # id -> customer（含 _workspace）
        self.cases: dict = {}  # id -> case
        self.interactions: list = []
        self.notes: list = []
        self.idem: dict = {}  # (token, key) -> (method, path, payload, status, body)
        super().__init__()

    def add_customer(self, name: str = "陳小姐", workspace: str = "w1", **extra) -> dict:
        cid = str(uuid.uuid4())
        customer = {"id": cid, "name": name, "external_customer_id": extra.get("external_customer_id", "C-007"),
                    "email": "chen@example.com", "phone": "0912-345-678", "status": extra.get("status", "active"),
                    "tags": [], "notes": "", "owner": None, "created_at": _now(), "updated_at": _now(),
                    "_workspace": workspace}
        self.customers[cid] = customer
        return customer

    def add_case(self, customer: dict, subject: str, description: str = "", category: str = "refund",
                 status: str = "open", **extra) -> dict:
        case_id = str(uuid.uuid4())
        case = {"id": case_id, "customer": customer["id"], "subject": subject, "description": description,
                "category": category, "legacy_category": "", "priority": extra.get("priority", "high"),
                "status": status, "assignee": None, "resolved_at": None,
                "created_at": extra.get("created_at", _now()), "updated_at": _now(),
                "_workspace": customer["_workspace"]}
        self.cases[case_id] = case
        return case

    def add_interaction(self, customer: dict, summary: str, occurred_at: str | None = None) -> dict:
        row = {"id": str(uuid.uuid4()), "customer": customer["id"], "summary": summary,
               "external_message_id": None, "occurred_at": occurred_at or _now(), "created_at": _now()}
        self.interactions.append(row)
        return row

    def add_note(self, case: dict, kind: str, body: str, *, author: str = "hinata", credential=None) -> dict:
        note = {"id": str(uuid.uuid4()), "case": case["id"], "kind": kind, "body": body,
                "author_type": "service" if credential else "user", "author_username": author,
                "author_credential": credential, "created_at": _now()}
        self.notes.append(note)
        return note

    def case_notes(self, case_id: str, kind: str | None = None) -> list:
        return [n for n in self.notes if n["case"] == case_id and (kind is None or n["kind"] == kind)]

    @staticmethod
    def _public(row: dict) -> dict:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    def route(self, method, path, query, body, headers):
        auth = headers.get("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else None
        info = self.tokens.get(token)
        if info is None:
            return 401, {"detail": "Invalid token."}
        ws = info.get("workspace", "w1")
        scopes = info["scopes"]
        parts = path.strip("/").split("/")
        if parts[:2] != ["api", "v1"]:
            return 404, {"detail": "Not found."}
        parts = parts[2:]

        def need(scope):
            return None if scope in scopes else (403, {"detail": "You do not have permission to perform this action."})

        # GET /customers/{id}/context/
        if len(parts) == 3 and parts[0] == "customers" and parts[2] == "context" and method == "GET":
            denied = need("customer:read")
            if denied:
                return denied
            customer = self.customers.get(parts[1])
            if not customer or customer["_workspace"] != ws:
                return 404, {"detail": "Not found."}
            interactions = sorted([i for i in self.interactions if i["customer"] == customer["id"]],
                                  key=lambda i: i["occurred_at"], reverse=True)[:20]
            open_cases = [self._public(c) for c in self.cases.values()
                          if c["customer"] == customer["id"] and c["status"] in ("open", "in_progress")][:20]
            return 200, {"customer": self._public(customer), "interactions": interactions,
                         "open_cases": open_cases, "pending_followups": []}
        # GET /cases/?customer=
        if len(parts) == 1 and parts[0] == "cases" and method == "GET":
            denied = need("case:read")
            if denied:
                return denied
            rows = [self._public(c) for c in self.cases.values() if c["_workspace"] == ws
                    and (not query.get("customer") or c["customer"] == query["customer"])]
            return 200, {"count": len(rows), "next": None, "previous": None, "results": rows}
        # GET /cases/{id}/
        if len(parts) == 2 and parts[0] == "cases" and method == "GET":
            denied = need("case:read")
            if denied:
                return denied
            case = self.cases.get(parts[1])
            if not case or case["_workspace"] != ws:
                return 404, {"detail": "Not found."}
            return 200, self._public(case)
        # /cases/{id}/notes/
        if len(parts) == 3 and parts[0] == "cases" and parts[2] == "notes":
            case = self.cases.get(parts[1])
            if method == "GET":
                denied = need("case_note:read")
                if denied:
                    return denied
                if not case or case["_workspace"] != ws:
                    return 404, {"detail": "Not found."}
                kind = query.get("kind")
                if kind is not None and kind not in NOTE_KINDS:
                    return 400, {"kind": ["Select a valid choice."]}
                rows = self.case_notes(case["id"], kind)
                return 200, {"count": len(rows), "next": None, "previous": None, "results": rows}
            if method == "POST":
                denied = need("case_note:write")
                if denied:
                    return denied
                key = headers.get("Idempotency-Key")
                if not key:
                    return 400, {"detail": "Idempotency-Key header is required."}
                payload = json.dumps(body, sort_keys=True)
                stored = self.idem.get((token, key))
                if stored:
                    if stored[:3] != (method, path, payload):
                        return 409, {"detail": "Idempotency-Key was used for a different request."}
                    return stored[3], stored[4]
                if not case or case["_workspace"] != ws:
                    return 404, {"detail": "Not found."}
                if not isinstance(body, dict) or body.get("kind") not in NOTE_KINDS:
                    return 400, {"kind": ["Select a valid choice."]}
                if not isinstance(body.get("body"), str) or not body["body"].strip():
                    return 400, {"body": ["Body cannot be blank"]}
                note = self.add_note(case, body["kind"], body["body"], author=info["username"],
                                     credential=info["name"])
                self.idem[(token, key)] = (method, path, payload, 201, note)
                return 201, note
            return 403, {"detail": "Case notes are append-only."}
        return 404, {"detail": "Not found."}


# --- controller -----------------------------------------------------------

REQUIRED_OUTPUTS = {
    "assess_refund": ("note_id", "suggested_amount_twd", "amount_is_estimate", "basis"),
    "record_decision": ("note_id",),
}


def _validate(step: str, output: dict) -> str | None:
    """照 refund.py 的 _validate_assessment。"""
    if step == "assess_refund":
        amount = output.get("suggested_amount_twd")
        if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 1_000_000:
            return "suggested_amount_twd 必須是 0～1,000,000 的整數（NT$）"
        if output.get("amount_is_estimate") is not True:
            return "amount_is_estimate 必須是 true"
    return None


class FakeController(FakeServer):
    """flow-report 用的 POST /api/flows/{id}/steps/{step}/result，以及寫入前核對用的 GET /api/flows/{id}。"""

    def __init__(self, tokens: dict):
        self.tokens = tokens  # token -> agent id
        self.results: list = []  # (flow_id, step, agent, body)
        self.flows: dict = {}
        super().__init__()

    def dispatch(self, flow_id: str, step: str, data, *, attempt: int = 1, agent: str = "finance-agent",
                 status: str = "dispatched", flow_status: str = "running") -> dict:
        """模擬 controller 派工：流程目前步驟＝step，step.input 照 engine 的格式 {instruction, skill, input}。"""
        flow = {"id": flow_id, "type": "refund", "status": flow_status, "current_step": step,
                "participants": [], "audit": [],
                "steps": [{"name": step, "assignee": agent, "status": status, "attempts": attempt,
                           "input": {"instruction": "…", "skill": "refund-x", "input": data}}]}
        self.flows[flow_id] = flow
        return flow

    def route(self, method, path, query, body, headers):
        auth = headers.get("Authorization") or ""
        agent = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if agent is None:
            return 401, {"detail": {"code": "unauthorized", "message": "token 無效"}}
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "flows"]:
            flow = self.flows.get(parts[2])
            if flow is None:
                return 404, {"detail": {"code": "not_found", "message": "沒有這個流程"}}
            if not any(s["assignee"] == agent for s in flow["steps"]):
                return 403, {"detail": {"code": "not_involved", "message": "不是參與者"}}
            return 200, flow
        if method == "POST" and len(parts) == 6 and parts[:2] == ["api", "flows"] and parts[3] == "steps" \
                and parts[5] == "result":
            flow_id, step = parts[2], parts[4]
            output = body.get("output") or {}
            if body.get("status") == "ok":
                missing = [k for k in REQUIRED_OUTPUTS.get(step, ()) if k not in output]
                if missing:
                    return 422, {"detail": {"code": "missing_output_fields", "message": ", ".join(missing)}}
                problem = _validate(step, output)
                if problem:
                    return 422, {"detail": {"code": "invalid_output", "message": problem}}
            self.results.append((flow_id, step, agent, body))
            flow = self.flows.get(flow_id)
            if flow:
                flow["audit"].append({"action": "step.reported", "actor": f"agent:{agent}",
                                      "detail": {"step": step, "attempt": body.get("attempt"),
                                                 "status": body.get("status")}})
            return 200, {"id": flow_id, "status": "running", "current_step": step, "revision": 2}
        return 404, {"detail": {"code": "not_found", "message": "not found"}}
