"""測試用的假 CRM、ADM、controller（只用標準函式庫的 http.server）。

只實作 skill 腳本會用到的端點，行為照真實系統的重點規則：
- CRM：CSRF cookie → 登入；session 寫入要帶 X-CSRFToken；follow-ups 篩選；分頁
- ADM：登入給 csrftoken；預約要 Idempotency-Key（同人同鍵同內容回原本那筆）；時段衝突 409
- controller：Bearer token；收件匣；一次性 token 確認；流程詳情；trip_report 事件
- HRM：登入給 sessionid、csrftoken；寫入要 X-CSRFToken、role 是 hr／admin；員工（department 篩選、分頁；和真的 HRM 一樣沒有 search）、
  部門、Employment（`has_employment_api=False` 模擬 HRM 還沒有這個端點）
"""

from __future__ import annotations

import json
import secrets
import socketserver
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _QuickServer(ThreadingHTTPServer):
    """HTTPServer.server_bind 會呼叫 socket.getfqdn()，在 macOS 上可能卡 30 秒以上；這裡略過。"""

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeServer:
    """route(method, path, query, body, headers, cookies) -> (status, body, set_cookies)。"""

    name = "fake"

    def __init__(self):
        self.calls: list = []
        # 依 (method, path) 注入錯誤：值是狀態碼列表，依序用掉；"drop" 代表直接斷線
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
                cookies = {}
                for part in (self.headers.get("Cookie") or "").split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        cookies[k] = v
                with server.lock:
                    server.calls.append((method, parsed.path, query, body, dict(self.headers)))
                    queued = server.faults.get((method, parsed.path))
                    fault = queued.pop(0) if queued else None
                if fault in ("drop", "drop_after"):
                    if fault == "drop_after":
                        # 伺服器端已經處理完，只是回應在路上遺失
                        with server.lock:
                            server.route(method, parsed.path, query, body, self.headers, cookies)
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                if fault is not None:
                    status, payload, set_cookies = fault, {"detail": "injected"}, {}
                else:
                    with server.lock:
                        status, payload, set_cookies = server.route(
                            method, parsed.path, query, body, self.headers, cookies)
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (set_cookies or {}).items():
                    self.send_header("Set-Cookie", f"{k}={v}; Path=/")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.httpd = _QuickServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def count(self, method: str, path: str) -> int:
        return sum(1 for call in self.calls if call[0] == method and call[1] == path)

    def route(self, method, path, query, body, headers, cookies):  # noqa: ANN001
        return 404, {"detail": "not found"}, {}


# --- CRM ----------------------------------------------------------------


class FakeCrm(FakeServer):
    name = "CRM"

    def __init__(self, users: dict, customers: list):
        # users: username -> {"password", "id", "hrm"}
        self.users = users
        self.customers = customers
        self.followups: list = []
        self.sessions: dict = {}
        super().__init__()

    def route(self, method, path, query, body, headers, cookies):
        if path == "/api/auth/csrf/":
            return 200, {"status": "ok"}, {"csrftoken": secrets.token_hex(8)}
        if path == "/api/auth/login/" and method == "POST":
            if not cookies.get("csrftoken") or headers.get("X-CSRFToken") != cookies.get("csrftoken"):
                return 403, {"detail": "CSRF Failed"}, {}
            user = self.users.get(body.get("username"))
            if not user or user["password"] != body.get("password"):
                return 400, {"detail": "Invalid credentials"}, {}
            sid = secrets.token_hex(8)
            self.sessions[sid] = body["username"]
            # Django 登入後會換新的 csrftoken
            return 200, {"username": body["username"]}, {"sessionid": sid,
                                                         "csrftoken": secrets.token_hex(8)}
        username = self.sessions.get(cookies.get("sessionid"))
        if not username:
            return 403, {"detail": "Authentication credentials were not provided."}, {}
        if method == "POST" and headers.get("X-CSRFToken") != cookies.get("csrftoken"):
            return 403, {"detail": "CSRF Failed: CSRF token missing."}, {}
        if path == "/api/me/":
            return 200, {"username": username, "memberships": []}, {}
        if path == "/api/v1/customers/":
            rows = self.customers
            if query.get("search"):
                rows = [c for c in rows if query["search"] in c["name"]
                        or query["search"] in (c.get("external_customer_id") or "")]
            return 200, self._page(rows, query, path), {}
        if path.startswith("/api/v1/customers/"):
            cid = path.rstrip("/").split("/")[-1]
            for row in self.customers:
                if row["id"] == cid:
                    return 200, row, {}
            return 404, {"detail": "Not found."}, {}
        if path == "/api/v1/follow-ups/" and method == "GET":
            rows = list(self.followups)
            for key in ("kind", "status", "assignee_hrm_employee_id"):
                if query.get(key):
                    rows = [r for r in rows if str(r.get(key)) == query[key]]
            if query.get("due_after"):
                rows = [r for r in rows if parse_dt(r["due_at"]) >= parse_dt(query["due_after"])]
            rows.sort(key=lambda r: r["due_at"])
            return 200, self._page(rows, query, path), {}
        if path == "/api/v1/follow-ups/" and method == "POST":
            assignee = body.get("assignee")
            hrm = next((u["hrm"] for u in self.users.values() if u["id"] == assignee), None)
            if assignee is not None and hrm is None:
                return 400, {"assignee": ["Invalid pk"]}, {}
            row = {
                "id": f"00000000-0000-4000-8000-{len(self.followups) + 1:012d}",
                "customer": body["customer"],
                "case": None,
                "title": body["title"],
                "kind": body.get("kind", "general"),
                "due_at": body["due_at"],
                "status": "pending",
                "assignee": assignee,
                "assignee_hrm_employee_id": hrm,
            }
            self.followups.append(row)
            return 201, row, {}
        return 404, {"detail": "not found"}, {}

    def _page(self, rows, query, path, size: int = 2):
        page = int(query.get("page", 1))
        chunk = rows[(page - 1) * size: page * size]
        nxt = None
        if page * size < len(rows):
            q = dict(query, page=page + 1)
            nxt = f"{self.url}{path}?{urllib.parse.urlencode(q)}"
        return {"count": len(rows), "next": nxt, "previous": None, "results": chunk}


# --- ADM ----------------------------------------------------------------


class FakeAdm(FakeServer):
    name = "ADM"

    def __init__(self, users: dict, rooms: list, vehicles: list, assets: list | None = None,
                 links: dict | None = None):
        self.users = users  # username -> {"password", "id", "staff"（選填）}
        self.rooms = rooms
        self.vehicles = vehicles
        self.room_bookings: list = []
        self.vehicle_bookings: list = []
        self.sessions: dict = {}
        # 測試用：強制前 N 次預約回 409（模擬別人剛好搶走）
        self.force_conflicts = 0
        # 資產（A3／A4）：custodian 是 ADM user id；links：HRM 員工 UUID -> ADM user id（PersonLink）
        self.assets = assets or []
        self.links = links or {}
        self.audit: list = []
        super().__init__()

    def _asset_view(self, asset: dict) -> dict:
        user = next((name for name, u in self.users.items() if u["id"] == asset["custodian"]), None)
        hrm = next((h for h, uid in self.links.items() if uid == asset["custodian"]), None)
        placeholder = bool(asset["custodian"] is not None and user and self.users[user].get("placeholder"))
        return {**asset, "custodian_username": user, "custodian_hrm_employee_id": hrm,
                "custodian_is_placeholder": placeholder}

    def _assets(self, method, path, query, body, username):
        if method == "GET" and path == "/api/v1/assets/":
            rows = list(self.assets)
            is_active = query.get("is_active", "true")
            if is_active != "all":
                rows = [a for a in rows if a["is_active"] == (is_active == "true")]
            if query.get("code"):
                rows = [a for a in rows if a["code"] == query["code"]]
            if query.get("category"):
                rows = [a for a in rows if a["category"].lower() == query["category"].lower()]
            if query.get("unassigned") is not None:
                rows = [a for a in rows if (a["custodian"] is None) == (query["unassigned"] == "true")]
            return 200, [self._asset_view(a) for a in rows], {}
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 5 and parts[4] == "assign":
            if not self.users[username].get("staff"):
                return 403, {"detail": "You do not have permission to perform this action."}, {}
            asset = next((a for a in self.assets if a["code"] == parts[3]), None)
            if asset is None:
                return 404, {"detail": "Not found."}, {}
            hrm = (body or {}).get("custodian_hrm_employee_id")
            user_id = None
            if hrm is not None:
                user_id = self.links.get(hrm)
                name = str((body or {}).get("custodian_display_name") or "").strip()
                if user_id is None and not name:
                    return 400, {"custodian_hrm_employee_id": ["No active user is linked to this HRM employee"]}, {}
                if user_id is None:
                    # A4 補強：沒有 PersonLink 但有姓名 → 建立停用、不可登入的佔位保管人
                    if not asset["is_active"]:
                        return 409, {"detail": "Inactive assets cannot be assigned"}, {}
                    number = str((body or {}).get("custodian_employee_number") or "").strip()
                    placeholder = f"hrm-{number or hrm.replace('-', '')[:8]}"
                    user_id = max(u["id"] for u in self.users.values()) + 1
                    self.users[placeholder] = {"password": None, "id": user_id, "placeholder": True,
                                               "display_name": name}
                    self.links[hrm] = user_id
                    self.audit.append({"action": "person.placeholder_created", "username": placeholder,
                                       "display_name": name, "actor": username})
            if not asset["is_active"]:
                return 409, {"detail": "Inactive assets cannot be assigned"}, {}
            if asset["custodian"] != user_id:
                self.audit.append({"action": "asset.assigned", "code": asset["code"], "actor": username,
                                   "previous_custodian_id": asset["custodian"], "custodian_id": user_id})
                asset["custodian"] = user_id
            return 200, self._asset_view(asset), {}
        return 404, {"detail": "not found"}, {}

    def route(self, method, path, query, body, headers, cookies):
        if path.startswith("/api/v1/assets/") and (headers.get("Authorization") or "").startswith("Bearer "):
            # A4：任何 agent token 一律 403；這個假 ADM 也不接受 token 讀資產（員工 agent 只用 session）
            return 403, {"detail": "Agent tokens cannot assign custodians"}, {}
        if path == "/api/v1/auth/login/":
            user = self.users.get(body.get("username"))
            if not user or user["password"] != body.get("password"):
                return 400, {"detail": "Invalid credentials"}, {}
            sid = secrets.token_hex(8)
            self.sessions[sid] = body["username"]
            return 200, {"id": user["id"], "username": body["username"]}, {
                "sessionid": sid, "csrftoken": secrets.token_hex(8)}
        username = self.sessions.get(cookies.get("sessionid"))
        if not username:
            return 403, {"detail": "not authenticated"}, {}
        if method == "POST" and headers.get("X-CSRFToken") != cookies.get("csrftoken"):
            return 403, {"detail": "CSRF Failed"}, {}
        if path.startswith("/api/v1/assets/"):
            return self._assets(method, path, query, body, username)
        if path == "/api/v1/rooms/availability/":
            start, end = parse_dt(query["start_at"]), parse_dt(query["end_at"])
            cap = int(query.get("capacity", 1))
            return 200, [r for r in self.rooms if r["capacity"] >= cap
                         and not self._overlap(self.room_bookings, "room", r["id"], start, end)], {}
        if path == "/api/v1/vehicles/availability/":
            start, end = parse_dt(query["start_at"]), parse_dt(query["end_at"])
            seats = int(query.get("seats", 1))
            return 200, [v for v in self.vehicles if v["seats"] >= seats
                         and not self._overlap(self.vehicle_bookings, "vehicle", v["id"], start, end)], {}
        if path == "/api/v1/vehicles/":
            return 200, self.vehicles, {}
        if path in ("/api/v1/room-bookings/", "/api/v1/vehicle-bookings/"):
            kind = "room" if "room" in path else "vehicle"
            store = self.room_bookings if kind == "room" else self.vehicle_bookings
            if method == "GET":
                return 200, [b for b in store if username in (b["booked_by_username"],
                                                              b.get("booked_for_username"))], {}
            key = headers.get("Idempotency-Key")
            if not key:
                return 400, {"Idempotency-Key": "required"}, {}
            digest = json.dumps(body, sort_keys=True)
            for b in store:
                if b["booked_by_username"] == username and b["_key"] == key:
                    if b["_digest"] != digest:
                        return 409, {"detail": "Idempotency key already used with different content"}, {}
                    return 200, b, {}
            if self.force_conflicts > 0:
                self.force_conflicts -= 1
                return 409, {"detail": "Room is already booked"}, {}
            start, end = parse_dt(body["start_at"]), parse_dt(body["end_at"])
            if self._overlap(store, kind, body[kind], start, end):
                return 409, {"detail": "already booked"}, {}
            booking = {"id": len(store) + 1, kind: body[kind], "start_at": body["start_at"],
                       "end_at": body["end_at"], "purpose": body["purpose"], "status": "active",
                       "booked_by_username": username, "booked_for_username": None,
                       "_key": key, "_digest": digest}
            if kind == "vehicle":
                booking.update(destination=body["destination"], pickup_odometer=None)
            store.append(booking)
            return 201, booking, {}
        return 404, {"detail": "not found"}, {}

    @staticmethod
    def _overlap(store, field, target, start, end):
        return any(
            b[field] == target and b["status"] in ("active", "picked_up")
            and parse_dt(b["start_at"]) < end and parse_dt(b["end_at"]) > start
            for b in store
        )


# --- controller ---------------------------------------------------------


class FakeController(FakeServer):
    name = "controller"

    def __init__(self, tokens: dict):
        self.tokens = tokens  # token -> agent id
        self.notifications: dict = {}  # agent -> [notification]
        self.approvals: dict = {}  # id -> {"token", "options", "context", "status", ...}
        self.flows: dict = {}  # id -> detail dict
        self.flow_viewers: dict = {}  # flow id -> set(agent)，其餘 agent 403
        self.events: list = []
        # 確認者由誰確認（GET /api/inbox/confirm-mode）：agent -> "agent"／"human"；
        # confirm_mode_supported=False 模擬還沒有這個端點的舊版 controller（404 not_found）
        self.confirm_modes: dict = {}
        self.confirm_mode_supported = True
        super().__init__()

    def add_approval(self, agent: str, approval_id: str, *, question: str, step_title: str,
                     options=None, context=None, flow_id: str = "flow-1", flow_title: str = "流程",
                     created_at: str = "2026-10-12T01:00:00+00:00", fields=None) -> str:
        token = f"apt_{secrets.token_hex(6)}"
        self.approvals[approval_id] = {
            "token": token, "status": "pending", "question": question, "step_title": step_title,
            "flow_title": flow_title, "flow_id": flow_id,
            "options": options or [{"value": "approve", "label": "核准"},
                                   {"value": "reject", "label": "退回"}],
            "context": context or {}, "fields": fields or [], "decision": None, "comment": None,
        }
        box = self.notifications.setdefault(agent, [])
        box.append({
            "id": len(box) + 1 + 100 * len(self.notifications), "kind": "approval_request",
            "title": f"待確認：{flow_title}", "body": question,
            "link": f"http://frontend/approvals/{approval_id}?token={token}",
            "flow_id": flow_id, "approval_id": approval_id,
            "created_at": created_at, "read_at": None,
        })
        return token

    def add_onboarding(self, flow_id: str, *, it_agent: str = "souei", employee_id: str,
                       asset_code: str = "NB-004", alternatives=("NB-005", "NB-001"),
                       current_step: str = "await_laptop", notify: bool = True) -> dict:
        """流程 ③ 停在 await_laptop（照 controller onboarding.py 的 state 結構），並放一則 IT 任務通知。"""
        flow = {
            "id": flow_id, "type": "onboarding", "status": "running", "current_step": current_step,
            "title": "10/19 到職 王小明（業務部）",
            "state": {
                "input": {"employee_id": employee_id, "name": "王小明", "employee_number": "E2026101",
                          "department_id": 1, "department_name": "業務部", "job_title": "業務專員",
                          "start_date": "2026-10-19"},
                "parties": {"it": {"agent_id": it_agent, "role": "it"}},
                "participants": [it_agent],
                "steps": {"draft_laptop": {"asset_code": asset_code, "asset_name": "15 吋業務筆電",
                                           "reason": "業務外出用", "alternatives": list(alternatives)}},
            },
            "steps": [{"name": "draft_laptop", "kind": "agent"}, {"name": "notify_it", "kind": "controller"},
                      {"name": "await_laptop", "kind": "wait"}, {"name": "book_room", "kind": "agent"}],
            "_it_agent": it_agent,
        }
        self.flows[flow_id] = flow
        self.flow_viewers.setdefault(flow_id, set()).add(it_agent)
        if notify:
            box = self.notifications.setdefault(it_agent, [])
            box.append({
                "id": 500 + len(box), "kind": "task", "title": "IT 任務：配發筆電給新人 王小明",
                "body": f"建議資產 {asset_code}", "link": None, "flow_id": flow_id, "approval_id": None,
                "created_at": "2026-10-12T01:00:00+00:00", "read_at": None,
            })
        return flow

    def _onboarding_event(self, flow_id, flow, agent, body):
        """POST /api/flows/{id}/events（laptop_assigned）：規則照 onboarding.md 的 await_laptop。"""
        if body.get("type") != "laptop_assigned":
            return 422, {"detail": {"code": "unknown_event"}}, {}
        if flow["current_step"] != "await_laptop":
            return 409, {"detail": {"code": "step_not_current", "message": "流程目前不在等指派"}}, {}
        if agent != flow.get("_it_agent"):
            return 403, {"detail": {"code": "not_reporter"}}, {}
        code = body.get("asset_code")
        if not isinstance(code, str) or not code.strip():
            return 422, {"detail": {"code": "invalid_event"}}, {}
        if body.get("note") is not None and not isinstance(body["note"], str):
            return 422, {"detail": {"code": "invalid_event"}}, {}
        self.events.append((flow_id, agent, body))
        suggested = flow["state"]["steps"]["draft_laptop"]["asset_code"]
        flow["state"]["steps"]["await_laptop"] = {
            "asset_code": code, "suggested_asset_code": suggested, "matches_suggestion": code == suggested,
            "custodian_hrm_employee_id": flow["state"]["input"]["employee_id"], "note": body.get("note") or ""}
        flow["current_step"] = "book_room"
        return 200, {"id": flow_id, "status": "running", "current_step": "book_room"}, {}

    def route(self, method, path, query, body, headers, cookies):
        auth = headers.get("Authorization") or ""
        agent = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if path.startswith("/api/approvals/"):
            parts = path.split("/")
            approval = self.approvals.get(parts[3])
            if approval is None:
                return 404, {"detail": {"code": "approval_not_found"}}, {}
            token = query.get("token") if method == "GET" else (body or {}).get("token")
            if token != approval["token"]:
                return 403, {"detail": {"code": "invalid_token", "message": "x"}}, {}
            if approval["status"] != "pending":
                return 410, {"detail": {"code": "token_used", "message": "x"}}, {}
            if method == "GET":
                return 200, {"id": parts[3], "flow_title": approval["flow_title"],
                             "step_title": approval["step_title"], "question": approval["question"],
                             "options": approval["options"], "context": approval["context"],
                             "fields": approval.get("fields") or []}, {}
            if body["decision"] not in {o["value"] for o in approval["options"]}:
                return 422, {"detail": {"code": "invalid_decision"}}, {}
            approval.update(status="decided", decision=body["decision"], comment=body.get("comment"))
            return 200, {"id": parts[3], "status": "decided", "decision": body["decision"]}, {}
        if agent is None:
            return 401, {"detail": {"code": "unauthorized"}}, {}
        if path == "/api/inbox/confirm-mode" and method == "GET" and self.confirm_mode_supported:
            mode = self.confirm_modes.get(agent, "agent")
            return 200, {"recipient": {"id": 1}, "confirm_mode": mode, "human": mode == "human",
                         "agent_id": agent}, {}
        if path == "/api/inbox":
            items = self.notifications.get(agent, [])
            if query.get("unread_only") == "true":
                items = [n for n in items if not n.get("read_at")]
            if self.confirm_modes.get(agent) == "human":
                # 和 controller 一樣：真人確認時 agent 拿不到確認連結
                items = [{**n, "link": None, "link_hidden": bool(n.get("link"))} for n in items]
            return 200, {"recipient": {"id": 1}, "unread_count": 0, "items": items}, {}
        if path.startswith("/api/inbox/") and path.endswith("/read") and method == "POST":
            nid = int(path.split("/")[3])
            for owner, box in self.notifications.items():
                for n in box:
                    if n["id"] == nid:
                        if owner != agent:
                            return 403, {"detail": {"code": "not_your_inbox"}}, {}
                        n["read_at"] = n.get("read_at") or "2026-10-12T02:00:00+00:00"
                        return 200, {"id": nid, "read_at": n["read_at"]}, {}
            return 404, {"detail": {"code": "notification_not_found"}}, {}
        if path == "/api/flows" and method == "GET":
            return 401, {"detail": {"code": "unauthorized", "message": "需要登入"}}, {}
        if path.startswith("/api/flows/"):
            parts = path.split("/")
            flow_id = parts[3]
            flow = self.flows.get(flow_id)
            if flow is None:
                return 404, {"detail": {"code": "flow_not_found"}}, {}
            if path.endswith("/events") and method == "POST" and flow.get("type") == "onboarding":
                return self._onboarding_event(flow_id, flow, agent, body or {})
            if path.endswith("/events") and method == "POST":
                current = next(s for s in flow["steps"] if s["name"] == flow["current_step"])
                if current["kind"] != "wait":
                    return 409, {"detail": {"code": "step_not_current"}}, {}
                if agent != flow.get("_sales_agent"):
                    return 403, {"detail": {"code": "not_your_flow"}}, {}
                self.events.append((flow_id, agent, body))
                flow["current_step"] = "expense_draft"
                return 202, {"status": "accepted"}, {}
            if method == "GET" and len(parts) == 4:
                if agent not in self.flow_viewers.get(flow_id, set()):
                    return 403, {"detail": {"code": "not_involved"}}, {}
                return 200, {k: v for k, v in flow.items() if not k.startswith("_")}, {}
        return 404, {"detail": {"code": "not_found"}}, {}


# --- HRM ----------------------------------------------------------------


class FakeHrm(FakeServer):
    name = "HRM"
    COMPANY = "c0c0c0c0-0000-4000-8000-000000000001"

    def __init__(self, users: dict, departments: list, has_employment_api: bool = True):
        self.users = users  # username -> {"password", "role"}
        self.departments = departments  # [{"id", "code", "name", "is_active"}]
        self.has_employment_api = has_employment_api
        self.employees: list = []
        self.employments: list = []
        self.sessions: dict = {}
        super().__init__()

    def _employee_view(self, row):
        primary = [e for e in self.employments if e["employee"] == row["id"] and e["is_primary"]]
        primary.sort(key=lambda e: e["start_date"], reverse=True)
        return {**row, "full_name": row["last_name"] + row["first_name"],
                "start_date": primary[0]["start_date"] if primary else None,
                "employment_type": primary[0]["employment_type"] if primary else None}

    def route(self, method, path, query, body, headers, cookies):
        if path == "/api/auth/login/" and method == "POST":
            user = self.users.get(body.get("username"))
            if not user or user["password"] != body.get("password"):
                return 400, {"detail": "帳號或密碼錯誤"}, {}
            sid = secrets.token_hex(8)
            self.sessions[sid] = body["username"]
            return 200, {"id": 9, "username": body["username"], "role": user["role"],
                         "data_scope": "company", "company": self.COMPANY,
                         "employee": {"id": "e-self"}}, {"sessionid": sid, "csrftoken": secrets.token_hex(8)}
        username = self.sessions.get(cookies.get("sessionid"))
        if not username:
            return 403, {"detail": "Authentication credentials were not provided."}, {}
        if method == "POST":
            if headers.get("X-CSRFToken") != cookies.get("csrftoken"):
                return 403, {"detail": "CSRF Failed: CSRF token missing."}, {}
            if self.users[username]["role"] not in ("hr", "admin"):
                return 403, {"detail": "You do not have permission to perform this action."}, {}
        if path == "/api/departments/" and method == "GET":
            return 200, self._page(self.departments, query, path), {}
        if path == "/api/employees/" and method == "GET":
            rows = [self._employee_view(r) for r in self.employees]
            # 和真的 HRM 一樣：只有 DjangoFilterBackend，沒有 search（帶了也被忽略）
            if query.get("department"):
                rows = [r for r in rows if r["department"] == query["department"]]
            return 200, self._page(rows, query, path), {}
        if path == "/api/employees/" and method == "POST":
            if any(r["employee_number"] == body["employee_number"] for r in self.employees):
                return 400, {"non_field_errors": ["employee_number 重複"]}, {}
            if body.get("company") != self.COMPANY:
                return 400, {"company": ["不可寫入其他公司資料"]}, {}
            if body.get("department") not in {d["id"] for d in self.departments}:
                return 400, {"department": ["Invalid pk"]}, {}
            row = {"id": f"e0000000-0000-4000-8000-{len(self.employees) + 1:012d}",
                   "company": body["company"], "department": body["department"],
                   "employee_number": body["employee_number"], "first_name": body["first_name"],
                   "last_name": body["last_name"], "email": body.get("email", ""),
                   "phone": body.get("phone", ""), "status": body.get("status", "active")}
            self.employees.append(row)
            return 201, self._employee_view(row), {}
        if path.startswith("/api/employees/") and method == "GET":
            eid = path.rstrip("/").split("/")[-1]
            row = next((r for r in self.employees if r["id"] == eid), None)
            return (200, self._employee_view(row), {}) if row else (404, {"detail": "Not found."}, {})
        if path == "/api/employments/" and self.has_employment_api:
            if method == "GET":
                return 200, self._page(self.employments, query, path), {}
            if body.get("employee") not in {r["id"] for r in self.employees}:
                return 400, {"employee": ["Invalid pk"]}, {}
            row = {"id": f"m{len(self.employments) + 1}", **body}
            self.employments.append(row)
            return 201, row, {}
        return 404, {"detail": "Not found."}, {}

    def _page(self, rows, query, path, size: int = 2):
        page = int(query.get("page", 1))
        chunk = rows[(page - 1) * size: page * size]
        nxt = None
        if page * size < len(rows):
            q = dict(query, page=page + 1)
            nxt = f"{self.url}{path}?{urllib.parse.urlencode(q)}"
        return {"count": len(rows), "next": nxt, "previous": None, "results": chunk}
