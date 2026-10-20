"""測試用的假 ADM 與假 controller（只用標準函式庫的 http.server）。

行為照真實系統的重點規則（ADM：FreeForJoy/adm/backend/apps/vehicles/services.py；
controller：controller/backend/app/engine.py 的 report_result）：
- ADM：Bearer agent token；代訂要 delegate scope 與 PersonLink；Idempotency-Key 綁定
  (擁有者, 鍵)，同內容回 200 原本那筆、不同內容 409、其他 token 重放 409；時段重疊 409；
  取車只能在「開始前 15 分鐘～結束」之間（409 Pickup is too early／Booking window has ended）；
  里程不能倒退（400）；delegate token 只看得到、只能操作自己代訂的預約。
  會議室（apps/rooms/services.py）規則相同；資產 A3 篩選（category／unassigned／is_active），
  A4 指派（`POST /assets/{code}/assign/`）對任何 agent token 一律 403。
- controller：`POST /api/flows/{id}/steps/{step}/result`，status=ok 時檢查必填 output 欄位（422）。
"""

from __future__ import annotations

import hashlib
import json
import socketserver
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
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


# --- ADM ----------------------------------------------------------------


class FakeAdm(FakeServer):
    """假 ADM 的公務車 API（/api/v1）。"""

    def __init__(self, tokens: dict, links: dict, vehicles: list, rooms: list | None = None,
                 assets: list | None = None):
        # tokens: token -> {"name", "owner", "scopes": set}
        self.tokens = tokens
        self.links = links  # HRM 員工 UUID -> ADM 使用者名稱
        self.vehicles = vehicles
        self.bookings: list = []
        self.rooms = rooms or []
        self.room_bookings: list = []
        # 測試用：前 N 次會議室預約回 409（模擬查完空房後被別人搶走）
        self.force_room_conflicts = 0
        self.assets = assets or []
        self.now = datetime.now(timezone.utc)
        super().__init__()

    def add_booking(self, *, vehicle: int, start_at: str, end_at: str, token: str | None = None,
                    booked_for: str | None = None, status: str = "active", **extra) -> dict:
        info = self.tokens.get(token) if token else None
        booking = {
            "id": len(self.bookings) + 1, "vehicle": vehicle, "start_at": start_at, "end_at": end_at,
            "purpose": extra.get("purpose", "其他人的預約"), "destination": extra.get("destination", "台北"),
            "status": status, "booked_by_username": info["owner"] if info else "someone",
            "booked_for_username": booked_for, "agent_token_name": info["name"] if info else None,
            "pickup_odometer": extra.get("pickup_odometer"), "return_odometer": extra.get("return_odometer"),
            "_owner": info["owner"] if info else "someone", "_token": token, "_key": extra.get("key"),
            "_digest": None,
        }
        self.bookings.append(booking)
        return booking

    def booking(self, booking_id: int) -> dict:
        return next(b for b in self.bookings if b["id"] == booking_id)

    def vehicle(self, vehicle_id: int) -> dict:
        return next(v for v in self.vehicles if v["id"] == vehicle_id)

    @staticmethod
    def public(booking: dict) -> dict:
        return {k: v for k, v in booking.items() if not k.startswith("_")}

    def _overlap(self, vehicle_id, start, end) -> bool:
        return any(
            b["vehicle"] == vehicle_id and b["status"] in ("active", "picked_up")
            and parse_dt(b["start_at"]) < end and parse_dt(b["end_at"]) > start
            for b in self.bookings
        )

    def route(self, method, path, query, body, headers):
        auth = headers.get("Authorization") or ""
        info = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        token = auth[7:] if info else None
        if info is None:
            return 401, {"detail": "Invalid token."}
        scopes = info["scopes"]
        if path.startswith("/api/v1/assets/"):
            return self._assets(method, path, query, scopes)
        if path.startswith("/api/v1/room") and path.split("/")[3] in ("rooms", "room-bookings"):
            return self._rooms(method, path, query, body, headers, info, token)
        can_read = bool(scopes & {"vehicles:read", "vehicles:delegate"})
        if path == "/api/v1/vehicles/availability/" and method == "GET":
            if not can_read:
                return 403, {"detail": "missing scope"}
            try:
                start, end = parse_dt(query["start_at"]), parse_dt(query["end_at"])
            except (KeyError, ValueError):
                return 400, {"start_at": ["This field is required."]}
            if end <= start:
                return 400, {"non_field_errors": ["end_at must be after start_at"]}
            seats = int(query.get("seats", 1))
            rows = [v for v in sorted(self.vehicles, key=lambda v: v["code"])
                    if v["is_active"] and v["seats"] >= seats and not self._overlap(v["id"], start, end)]
            return 200, rows
        if path == "/api/v1/vehicle-bookings/" and method == "GET":
            rows = [self.public(b) for b in self.bookings
                    if b["_token"] == token or b["_owner"] == info["owner"] and b["_token"] is None]
            return 200, rows
        if path == "/api/v1/vehicle-bookings/" and method == "POST":
            return self._create(body, headers, info, token)
        parts = path.strip("/").split("/")
        if (method == "POST" and len(parts) == 5 and parts[:3] == ["api", "v1", "vehicle-bookings"]
                and parts[4] in ("pickup", "return")):
            return self._handover(int(parts[3]), parts[4], body or {}, info, token)
        return 404, {"detail": "Not found."}

    def _create(self, body, headers, info, token):
        key = headers.get("Idempotency-Key")
        if not key or len(key) > 128:
            return 400, {"Idempotency-Key": "A key of 1 to 128 characters is required"}
        hrm = body.get("booked_for_hrm_employee_id")
        if hrm is not None and "vehicles:delegate" not in info["scopes"]:
            return 403, {"detail": "delegate scope required"}
        if hrm is None and "vehicles:write" not in info["scopes"]:
            return 403, {"detail": "write scope required"}
        booked_for = None
        if hrm is not None:
            booked_for = self.links.get(hrm)
            if booked_for is None:
                return 400, {"booked_for_hrm_employee_id": ["No active user is linked to this HRM employee"]}
        try:
            vehicle = self.vehicle(body["vehicle"])
        except StopIteration:
            return 400, {"vehicle": ["Invalid pk - object does not exist."]}
        start, end = parse_dt(body["start_at"]), parse_dt(body["end_at"])
        digest = hashlib.sha256(json.dumps(
            {"vehicle": body["vehicle"], "start_at": start.isoformat(), "end_at": end.isoformat(),
             "purpose": body["purpose"], "destination": body["destination"], "booked_for": booked_for},
            sort_keys=True).encode()).hexdigest()
        for b in self.bookings:
            if b["_owner"] == info["owner"] and b["_key"] == key:
                if b["_digest"] != digest:
                    return 409, {"detail": "Idempotency key already used with different content"}
                if booked_for is not None and b["_token"] != token:
                    return 409, {"detail": "Idempotency key already used by another agent token"}
                return 200, self.public(b)
        if start < self.now:
            return 400, ["start_at must be in the future"]
        if not vehicle["is_active"]:
            return 409, {"detail": "Vehicle is inactive"}
        if self._overlap(vehicle["id"], start, end):
            return 409, {"detail": "Vehicle is already booked"}
        booking = self.add_booking(vehicle=vehicle["id"], start_at=body["start_at"], end_at=body["end_at"],
                                   token=token, booked_for=booked_for, purpose=body["purpose"],
                                   destination=body["destination"], key=key)
        booking["_digest"] = digest
        return 201, self.public(booking)

    def _handover(self, booking_id, action, body, info, token):
        try:
            booking = self.booking(booking_id)
        except StopIteration:
            return 404, {"detail": "Not found."}
        if booking["_token"] != token:
            return 403, {"detail": "You do not have permission to perform this action."}
        odometer = body.get("odometer")
        if isinstance(odometer, bool) or not isinstance(odometer, int) or odometer < 0:
            return 400, {"odometer": ["A valid integer is required."]}
        vehicle = self.vehicle(booking["vehicle"])
        if action == "pickup":
            if booking["status"] != "active":
                return 409, {"detail": "Only an active booking can be picked up"}
            if self.now < parse_dt(booking["start_at"]) - timedelta(minutes=15):
                return 409, {"detail": "Pickup is too early"}
            if self.now >= parse_dt(booking["end_at"]):
                return 409, {"detail": "Booking window has ended"}
            if odometer < vehicle["odometer"]:
                return 400, {"odometer": "Cannot be less than current vehicle mileage"}
            booking.update(status="picked_up", pickup_odometer=odometer,
                           pickup_condition=body.get("condition_notes", ""))
            return 200, self.public(booking)
        if booking["status"] != "picked_up":
            return 409, {"detail": "Only a picked-up vehicle can be returned"}
        if odometer < booking["pickup_odometer"]:
            return 400, {"odometer": "Cannot be less than pickup mileage"}
        booking.update(status="returned", return_odometer=odometer,
                       return_condition=body.get("condition_notes", ""))
        vehicle["odometer"] = odometer
        return 200, self.public(booking)

    # --- 資產（A3 篩選、A4 指派）---------------------------------------------

    def _assets(self, method, path, query, scopes):
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 5 and parts[4] == "assign":
            # A4：僅限 session 登入的 staff；任何 agent token 一律 403（不論 scope）
            return 403, {"detail": "You do not have permission to perform this action."}
        if method != "GET" or path != "/api/v1/assets/":
            return 404, {"detail": "Not found."}
        if "assets:read" not in scopes:
            return 403, {"detail": "missing scope"}
        rows = list(self.assets)
        unassigned = query.get("unassigned")
        is_active = query.get("is_active", "true")
        if unassigned not in (None, "true", "false") or is_active not in ("true", "false", "all"):
            return 400, {"unassigned": ["Must be a valid boolean."]}
        if is_active != "all":
            rows = [a for a in rows if a["is_active"] == (is_active == "true")]
        if query.get("code"):
            rows = [a for a in rows if a["code"] == query["code"]]
        if query.get("category"):
            rows = [a for a in rows if a["category"].lower() == query["category"].lower()]
        if unassigned is not None:
            rows = [a for a in rows if (a["custodian"] is None) == (unassigned == "true")]
        return 200, sorted(rows, key=lambda a: a["code"])

    # --- 會議室（含 A1／A2 代訂）---------------------------------------------

    def room(self, room_id: int) -> dict:
        return next(r for r in self.rooms if r["id"] == room_id)

    def _room_overlap(self, room_id, start, end) -> bool:
        return any(b["room"] == room_id and b["status"] == "active"
                   and parse_dt(b["start_at"]) < end and parse_dt(b["end_at"]) > start
                   for b in self.room_bookings)

    def add_room_booking(self, *, room: int, start_at: str, end_at: str, token: str | None = None,
                         booked_for: str | None = None, purpose: str = "其他人的會議", key=None,
                         digest=None) -> dict:
        info = self.tokens.get(token) if token else None
        booking = {
            "id": len(self.room_bookings) + 1, "room": room, "start_at": start_at, "end_at": end_at,
            "purpose": purpose, "status": "active",
            "booked_by_username": info["owner"] if info else "someone", "booked_for_username": booked_for,
            "agent_token_name": info["name"] if info else None,
            "_owner": info["owner"] if info else "someone", "_token": token, "_key": key, "_digest": digest,
        }
        self.room_bookings.append(booking)
        return booking

    def _rooms(self, method, path, query, body, headers, info, token):
        scopes = info["scopes"]
        can_read = bool(scopes & {"rooms:read", "rooms:delegate"})
        if path == "/api/v1/rooms/" and method == "GET":
            if "rooms:read" not in scopes:
                return 403, {"detail": "missing scope"}
            return 200, [r for r in self.rooms if r["is_active"]]
        if path == "/api/v1/rooms/availability/" and method == "GET":
            if not can_read:
                return 403, {"detail": "missing scope"}
            try:
                start, end = parse_dt(query["start_at"]), parse_dt(query["end_at"])
            except (KeyError, ValueError):
                return 400, {"start_at": ["This field is required."]}
            capacity = int(query.get("capacity", 1))
            return 200, [r for r in sorted(self.rooms, key=lambda r: r["code"])
                         if r["is_active"] and r["capacity"] >= capacity
                         and not self._room_overlap(r["id"], start, end)]
        if path == "/api/v1/room-bookings/" and method == "GET":
            if not can_read:
                return 403, {"detail": "missing scope"}
            return 200, [self.public(b) for b in self.room_bookings
                         if b["_token"] == token or b["_owner"] == info["owner"] and b["_token"] is None]
        if path == "/api/v1/room-bookings/" and method == "POST":
            return self._create_room(body, headers, info, token)
        return 404, {"detail": "Not found."}

    def _create_room(self, body, headers, info, token):
        key = headers.get("Idempotency-Key")
        if not key or len(key) > 128:
            return 400, {"Idempotency-Key": "A key of 1 to 128 characters is required"}
        hrm = body.get("booked_for_hrm_employee_id")
        if hrm is not None and "rooms:delegate" not in info["scopes"]:
            return 403, {"detail": "delegate scope required"}
        if hrm is None and "rooms:write" not in info["scopes"]:
            return 403, {"detail": "write scope required"}
        booked_for = None
        if hrm is not None:
            booked_for = self.links.get(hrm)
            if booked_for is None:
                return 400, {"booked_for_hrm_employee_id": ["No active user is linked to this HRM employee"]}
        try:
            room = self.room(body["room"])
        except StopIteration:
            return 400, {"room": ["Invalid pk - object does not exist."]}
        start, end = parse_dt(body["start_at"]), parse_dt(body["end_at"])
        digest = hashlib.sha256(json.dumps(
            {"room": body["room"], "start_at": start.isoformat(), "end_at": end.isoformat(),
             "purpose": body["purpose"], "booked_for": booked_for}, sort_keys=True).encode()).hexdigest()
        for b in self.room_bookings:
            if b["_owner"] == info["owner"] and b["_key"] == key:
                if b["_digest"] != digest:
                    return 409, {"detail": "Idempotency key already used with different content"}
                if booked_for is not None and b["_token"] != token:
                    return 409, {"detail": "Idempotency key already used by another agent token"}
                return 200, self.public(b)
        if start < self.now:
            return 400, ["start_at must be in the future"]
        if self.force_room_conflicts > 0:
            self.force_room_conflicts -= 1
            return 409, {"detail": "Room is already booked"}
        if not room["is_active"]:
            return 409, {"detail": "Room is inactive"}
        if self._room_overlap(room["id"], start, end):
            return 409, {"detail": "Room is already booked"}
        booking = self.add_room_booking(room=room["id"], start_at=body["start_at"], end_at=body["end_at"],
                                        token=token, booked_for=booked_for, purpose=body["purpose"],
                                        key=key, digest=digest)
        return 201, self.public(booking)


# --- controller ---------------------------------------------------------

REQUIRED_OUTPUTS = {
    "draft_vehicle": ("vehicle_id", "vehicle_code", "start_at", "end_at", "destination", "purpose"),
    "book_vehicle": ("booking_id", "vehicle_id", "start_at", "end_at"),
    "record_trip": ("booking_id", "pickup_odometer", "return_odometer", "distance_km"),
    # 流程 ③（controller/backend/app/flows/onboarding.py 的 LAPTOP_OUTPUTS、ROOM_OUTPUTS）
    "draft_laptop": ("asset_code", "asset_name", "reason"),
    "book_room": ("booking_id", "room_id", "room_name", "start_at", "end_at"),
}


class FakeController(FakeServer):
    """flow-report 用的 POST /api/flows/{id}/steps/{step}/result，以及寫入前核對用的 GET /api/flows/{id}。"""

    def __init__(self, tokens: dict):
        self.tokens = tokens  # token -> agent id
        self.results: list = []  # (flow_id, step, agent, body)
        self.flows: dict = {}  # flow_id -> flow detail（只放 skill 核對用到的欄位）
        super().__init__()

    def dispatch(self, flow_id: str, step: str, data, *, attempt: int = 1, agent: str = "admin-agent",
                 status: str = "dispatched", flow_status: str = "running") -> dict:
        """模擬 controller 派工：流程目前步驟＝step，step.input 照 engine 的格式 {instruction, skill, input}。"""
        flow = {"id": flow_id, "status": flow_status, "current_step": step, "participants": [],
                "steps": [{"name": step, "assignee": agent, "status": status, "attempts": attempt,
                           "input": {"instruction": "…", "skill": "adm-vehicle-x", "input": data}}]}
        self.flows[flow_id] = flow
        return flow

    def route(self, method, path, query, body, headers):
        auth = headers.get("Authorization") or ""
        agent = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if agent is None:
            return 401, {"detail": {"code": "unauthorized"}}
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "flows"]:
            flow = self.flows.get(parts[2])
            if flow is None:
                return 404, {"detail": {"code": "not_found"}}
            if not any(s["assignee"] == agent for s in flow["steps"]):
                return 403, {"detail": {"code": "not_involved"}}
            return 200, flow
        if method == "POST" and len(parts) == 6 and parts[:2] == ["api", "flows"] and parts[3] == "steps" \
                and parts[5] == "result":
            flow_id, step = parts[2], parts[4]
            if body.get("status") == "ok":
                missing = [k for k in REQUIRED_OUTPUTS.get(step, ()) if k not in (body.get("output") or {})]
                if missing:
                    return 422, {"detail": {"code": "missing_output_fields", "message": ", ".join(missing)}}
            self.results.append((flow_id, step, agent, body))
            return 200, {"id": flow_id, "status": "running"}
        return 404, {"detail": {"code": "not_found"}}
