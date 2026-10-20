#!/usr/bin/env python3
"""FreeForJoy ADM 行政模擬 CLI（計畫 9.3、9.4）。

7 隻員工 agent 以 session 帳號登入 ADM，扮演員工本人預約會議室與公務車，
讓 ADM 有正常的使用量與時段衝突：

  decide         每個工作日跑一次：10% 機率預約會議室；luminous 另外 5% 預約公務車（自己用）
  book-room      立刻預約一間會議室（demo 用，可指定日期、時間、人數）
  assign-laptop  IT 人員（souei）處理流程 ③ 的 IT 任務：在 ADM 把建議的筆電指派給新人（session staff），
                 再向 controller 送 laptop_assigned 事件（契約：controller/backend/docs/flows/onboarding.md）
  status         查自己的預約與狀態檔，除錯用

時段衝突（409）或沒有空房／空車時換一個時段，最多再試 2 次。
一律輸出單一行 JSON；成功 exit 0，失敗 exit 1。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402

SKILL = "agent-adm"
DEFAULT_ADM_BASE = "http://localhost:18001/api/v1"

ROOM_CHANCE = 0.10
VEHICLE_CHANCE = 0.05
VEHICLE_AGENTS = ("luminous",)
MAX_ATTEMPTS = 3  # 第一次＋換時段最多 2 次

ROOM_STARTS = tuple(f"{h:02d}:{m:02d}" for h in range(10, 17) for m in (0, 30) if (h, m) <= (16, 0))
ROOM_DURATIONS = (30, 60, 60, 90)
ROOM_CAPACITIES = (2, 2, 4, 4, 6, 8)
VEHICLE_STARTS = ("10:00", "10:30", "11:00", "13:30", "14:00", "14:30", "15:00")
VEHICLE_DURATIONS = (60, 90, 120, 180)

ROOM_PURPOSES = {
    "sales": ("客戶提案演練", "業務週會", "報價單內部討論"),
    "sales_manager": ("業務部週會", "業績檢討會議", "新人業務培訓"),
    "cs_manager": ("客訴案例檢討", "客服流程改善討論", "客服排班協調"),
    "it": ("系統維護說明會", "資安宣導", "設備採購評估"),
    "admin_finance": ("月結對帳討論", "供應商付款排程會議", "行政事務協調"),
    "hr": ("面試", "到職說明會籌備", "員工訓練規劃"),
}
VEHICLE_ERRANDS = (
    ("台北市中正區銀行", "銀行對帳與票據送件"),
    ("國稅局大安分局", "營業稅申報資料送件"),
    ("台北郵局", "寄送合約正本"),
    ("新北市三重區供應商", "包材供應商簽約"),
    ("新北市五股區倉庫", "盤點冷凍倉庫庫存"),
)


class Adm:
    """ADM session client。登入不需要 CSRF；之後的寫入要帶 X-CSRFToken。"""

    def __init__(self, base: str):
        self.http = c.HttpClient(base, "ADM")

    def login(self, username: str, password: str) -> dict:
        status, body = self.http.request("POST", "/auth/login/",
                                         {"username": username, "password": password},
                                         idempotent=True)
        if status != 200 or not isinstance(body, dict):
            raise c.ServiceError(f"ADM 登入失敗 {status}: {c.short(body)}",
                                 announce=status in (400, 401, 403), kind="login_failed")
        return body

    def get(self, path: str, params: dict | None = None):
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        status, body = self.http.request("GET", path)
        if status != 200:
            raise c.ServiceError(f"ADM GET {path} 失敗 {status}: {c.short(body)}",
                                 announce=status in (401, 403))
        return body

    def post(self, path: str, body: dict, key: str):
        # ADM 的預約有 Idempotency-Key（以預約人＋鍵判斷），所以逾時重送是安全的
        return self.http.request(
            "POST", path, body,
            headers={"X-CSRFToken": self.http.cookie("csrftoken") or "", "Idempotency-Key": key},
            idempotent=True,
        )


def adm_login(agent: str) -> Adm:
    password = c.env("ADM_AGENT_PASSWORD")
    if not password:
        raise c.ConfigError("沒有設定 ADM_AGENT_PASSWORD，無法登入 ADM")
    adm = Adm(c.env("ADM_API_BASE", DEFAULT_ADM_BASE))
    adm.login(agent, password)
    return adm


# --- 時段 ---------------------------------------------------------------


def candidate_days(today: date, now: datetime) -> list:
    """今天（還有時段的話）以及接下來 2 個工作天。"""
    days = []
    if c.is_workday(today) and now < c.at(today, "15:30"):
        days.append(today)
    days.append(c.add_workdays(today, 1))
    days.append(c.add_workdays(today, 2))
    return days


def pick_slot(kind: str, today: date, now: datetime, tried: list, *, day: date | None = None,
              start: str | None = None, duration: int | None = None) -> dict | None:
    """挑一個還沒試過、在未來 30 分鐘以後的時段。"""
    starts = ROOM_STARTS if kind == "room" else VEHICLE_STARTS
    durations = ROOM_DURATIONS if kind == "room" else VEHICLE_DURATIONS
    seen = {(t["start_at"], t["end_at"]) for t in tried}
    days = [day] if day else candidate_days(today, now)
    for _ in range(50):
        d = day or c.RNG.choice(days)
        s = start if (start and not tried) else c.RNG.choice(starts)
        minutes = duration or c.RNG.choice(durations)
        begin = c.at(d, s)
        end = begin + timedelta(minutes=minutes)
        if begin < now + timedelta(minutes=30):
            continue
        if end > c.at(d, "17:30"):
            continue
        key = (c.iso(begin), c.iso(end))
        if key in seen:
            continue
        return {"start_at": key[0], "end_at": key[1], "minutes": minutes}
    return None


# --- 預約 ---------------------------------------------------------------


def book(adm: Adm | None, agent: str, kind: str, record: dict, persist, today: date, now: datetime,
         dry_run: bool, *, capacity: int | None = None, purpose: str | None = None,
         day: date | None = None, start: str | None = None, duration: int | None = None) -> dict:
    """預約會議室或公務車；409／沒空位時換時段，最多 MAX_ATTEMPTS 次。

    每次嘗試（時段、對象、Idempotency-Key）都先寫進 record 再送出：
    重跑時沿用最後一次「結果未知」的嘗試（同鍵同內容 ADM 會回原本那筆），
    不會因為重新擲骰而產生第二筆預約。
    """
    role = c.AGENTS[agent]["role"]
    if record.get("booking_id"):
        return {"outcome": "already_booked", **record}
    record.setdefault("attempts", [])
    if kind == "room":
        record.setdefault("capacity", capacity or c.RNG.choice(ROOM_CAPACITIES))
        record.setdefault("purpose", purpose or c.RNG.choice(ROOM_PURPOSES[role]))
    else:
        destination, errand = c.RNG.choice(VEHICLE_ERRANDS)
        record.setdefault("seats", capacity or c.RNG.choice((1, 1, 2)))
        record.setdefault("destination", destination)
        record.setdefault("purpose", purpose or f"行政外出：{errand}")

    if dry_run:
        slot = pick_slot(kind, today, now, [], day=day, start=start, duration=duration)
        return {"outcome": f"would_book_{kind}", **record, "slot": slot}

    attempts = record["attempts"]
    while True:
        pending = attempts[-1] if attempts and attempts[-1].get("result") == "sent" else None
        if pending is None:
            if len(attempts) >= MAX_ATTEMPTS:
                record["gave_up"] = True
                persist()
                return {"outcome": f"{kind}_unavailable", **record}
            slot = pick_slot(kind, today, now, attempts, day=day, start=start, duration=duration)
            if slot is None:
                record["gave_up"] = True
                persist()
                return {"outcome": f"{kind}_unavailable", **record}
            if kind == "room":
                options = adm.get("/rooms/availability/", {"start_at": slot["start_at"],
                                                           "end_at": slot["end_at"],
                                                           "capacity": record["capacity"]})
            else:
                options = adm.get("/vehicles/availability/", {"start_at": slot["start_at"],
                                                              "end_at": slot["end_at"],
                                                              "seats": record["seats"]})
            if not options:
                attempts.append({**slot, "result": "none_available"})
                persist()
                continue
            choice = c.RNG.choice(options)
            pending = {
                **slot,
                "target": choice["id"],
                "target_name": choice.get("name") or choice.get("code") or choice.get("plate_number"),
                "key": f"{SKILL}:{agent}:{kind}:{c.stable_key(today, slot['start_at'], choice['id'], len(attempts))}",
                "result": "sent",
            }
            attempts.append(pending)
            persist()

        if kind == "room":
            path = "/room-bookings/"
            payload = {"room": pending["target"], "start_at": pending["start_at"],
                       "end_at": pending["end_at"], "purpose": record["purpose"]}
        else:
            path = "/vehicle-bookings/"
            payload = {"vehicle": pending["target"], "start_at": pending["start_at"],
                       "end_at": pending["end_at"], "purpose": record["purpose"],
                       "destination": record["destination"]}
        status, body = adm.post(path, payload, pending["key"])
        if status in (200, 201) and isinstance(body, dict):
            pending["result"] = "booked"
            record["booking_id"] = body.get("id")
            record.update(start_at=pending["start_at"], end_at=pending["end_at"],
                          target_name=pending.get("target_name"))
            persist()
            return {"outcome": f"{kind}_booked", **record}
        if status == 409:
            pending["result"] = "conflict"
            persist()
            continue
        if status == 400 and "future" in c.short(body):
            # 時段在送出前已經過去（重跑得太晚），換一個時段
            pending["result"] = "past"
            persist()
            continue
        if status >= 500:
            # 結果未知：保留 "sent"，重跑時用同一個鍵、同一份內容重送
            raise c.ServiceError(f"ADM 預約失敗 {status}: {c.short(body)}")
        pending["result"] = f"http_{status}"
        persist()
        raise c.ServiceError(f"ADM 預約失敗 {status}: {c.short(body)}",
                             announce=status in (400, 403))


def summarize(kind: str, outcome: dict) -> str:
    label = "會議室" if kind == "room" else "公務車"
    if outcome["outcome"].endswith("_booked") or outcome["outcome"] == "already_booked":
        when = f"{outcome.get('start_at', '')[:16].replace('T', ' ')}～{outcome.get('end_at', '')[11:16]}"
        return f"已預約{label} {outcome.get('target_name') or ''}（{when}）"
    if outcome["outcome"].endswith("_unavailable"):
        return f"{label}連試 {len(outcome.get('attempts', []))} 個時段都沒有空位，今天先不訂"
    if outcome["outcome"].startswith("would_book"):
        slot = outcome.get("slot") or {}
        when = f"{slot.get('start_at', '')[:16].replace('T', ' ')}～{slot.get('end_at', '')[11:16]}"
        return f"dry-run：會試著預約{label}（{when}），沒有真的送出"
    return f"{label}：{outcome['outcome']}"


# --- 子指令 -------------------------------------------------------------


def cmd_decide(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    if not c.is_workday(today) and not args.force:
        result.update(outcome="not_working_day", detail="今天不是工作日，不預約")
        return True
    path = c.state_path(SKILL, agent, f"{today.isoformat()}.json")
    state = c.load_json(path)

    def persist():
        if not args.dry_run:
            c.save_json(path, state)

    if "decision" not in state:
        room_roll = round(c.RNG.random(), 4)
        vehicle_roll = round(c.RNG.random(), 4) if agent in VEHICLE_AGENTS else None
        if args.force:
            want_room = args.force in ("room", "both")
            want_vehicle = args.force in ("vehicle", "both")
        else:
            want_room = room_roll < ROOM_CHANCE
            want_vehicle = vehicle_roll is not None and vehicle_roll < VEHICLE_CHANCE
        state.update(
            decision={"room": want_room, "vehicle": want_vehicle},
            rolls={"room": room_roll, "vehicle": vehicle_roll},
            decided_at=c.iso(now),
        )
        persist()
    decision = state["decision"]
    result.update(decision=decision, rolls=state.get("rolls"))

    if not decision["room"] and not decision["vehicle"]:
        result.update(outcome="no_booking", detail="今天沒有要預約的會議室或公務車")
        return True

    adm = None if args.dry_run else adm_login(agent)
    details = []
    outcomes = {}
    for kind in ("room", "vehicle"):
        if not decision[kind]:
            continue
        record = state.setdefault(kind, {})
        outcome = book(adm, agent, kind, record, persist, today, now, args.dry_run)
        outcomes[kind] = outcome
        details.append(summarize(kind, outcome))
    result.update(bookings=outcomes, detail="；".join(details))
    if args.dry_run:
        result["outcome"] = "would_book"
    elif all(o["outcome"] == "already_booked" for o in outcomes.values()):
        result["outcome"] = "already_decided"
    elif any(o["outcome"] in ("room_booked", "vehicle_booked", "already_booked")
             for o in outcomes.values()):
        result["outcome"] = "booked"
    else:
        result["outcome"] = "unavailable"
    return True


def cmd_book_room(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    day = date.fromisoformat(args.on) if args.on else None
    if args.start and not day:
        day = today
    # 手動預約也要冪等：同樣的參數重跑，沿用同一份紀錄
    key = c.stable_key(day, args.start, args.duration, args.capacity, args.purpose)
    path = c.state_path(SKILL, agent, f"{today.isoformat()}.json")
    state = c.load_json(path)
    manual = state.setdefault("manual", {})
    record = manual.setdefault(key, {})

    def persist():
        if not args.dry_run:
            c.save_json(path, state)

    adm = None if args.dry_run else adm_login(agent)
    outcome = book(adm, agent, "room", record, persist, today, now, args.dry_run,
                   capacity=args.capacity, purpose=args.purpose, day=day, start=args.start,
                   duration=args.duration)
    result.update(booking=outcome, outcome=outcome["outcome"], detail=summarize("room", outcome))
    return not outcome["outcome"].endswith("_unavailable")


# --- 子指令：assign-laptop（流程 ③ await_laptop）------------------------------

IT_ROLE = "it"
TASK_KIND = "task"
EVENT_LAPTOP_ASSIGNED = "laptop_assigned"
TERMINAL_FLOW = ("succeeded", "failed", "cancelled", "expired")


class Controller:
    """員工 agent 自己的 controller token（FLOW_CONTROLLER_TOKEN_<AGENT>）。"""

    def __init__(self, agent: str):
        self.http = c.HttpClient(c.controller_base(), "controller",
                                 {"Authorization": f"Bearer {c.controller_token(agent)}"})

    def get(self, path: str):
        return self.http.request("GET", f"/api{path}")

    def post(self, path: str, body: dict | None = None):
        # 事件回報只在連線失敗時重試；回應遺失時重跑會拿到 409 step_not_current，再查流程確認已收下
        return self.http.request("POST", f"/api{path}", body or {}, idempotent=False)


def _assign_key(agent: str, flow_id: str) -> str:
    return f"{SKILL}:{agent}:assign:{flow_id}"[:128]


def _laptop_candidates(state: dict) -> list:
    draft = ((state.get("steps") or {}).get("draft_laptop") or {})
    codes = [draft.get("asset_code")] + list(draft.get("alternatives") or [])
    seen, out = set(), []
    for code in codes:
        if isinstance(code, str) and code.strip() and code.strip() not in seen:
            seen.add(code.strip())
            out.append(code.strip())
    return out


def _new_hire_fields(fstate: dict) -> dict:
    """從流程 ③ 的 state.input 取新人姓名與員工編號，帶給 A4 的 custodian_display_name／custodian_employee_number。

    新人通常還沒有 ADM 帳號與 PersonLink；ADM 收到姓名時會自動建立「停用、不可登入」的佔位保管人再完成指派。
    已有帳號時 ADM 忽略這兩個欄位。姓名空白就不帶（ADM 會照舊回 400，交給人處理）。
    """
    data = fstate.get("input") or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}
    fields = {"custodian_display_name": name.strip()[:150]}
    number = data.get("employee_number")
    if isinstance(number, str) and number.strip():
        fields["custodian_employee_number"] = number.strip()[:64]
    return fields


def _placeholder_info(asset: dict) -> dict:
    """保管人是 ADM 自動建立、尚未啟用的佔位帳號時的資訊。

    「已指派」的路徑（例如上次指派成功但回報 controller 失敗、這輪重跑）也要帶，note 才不會漏。
    """
    return {"placeholder": asset.get("custodian_is_placeholder") is True,
            "custodian_username": asset.get("custodian_username")}


def _assign_on_adm(adm: Adm, agent: str, flow_id: str, employee_id: str, candidates: list,
                   new_hire: dict | None = None) -> dict:
    """用 IT 人員的 ADM session（staff）把筆電指派給新人。

    - 新人名下已經有筆電（上次已指派、或 IT 在網頁上手動指派過）→ 視為成功，不再指派第二台。
    - 依建議、備選的順序：已被別人指派、停用、不存在的跳過；沒有保管人的呼叫 A4 指派。
    - `new_hire`（_new_hire_fields）一併送出：新人還沒有 ADM 帳號時由 ADM 建立佔位保管人。
    - 回傳 {"status": "assigned"|"needs_human", "asset_code", "already", "placeholder", "tried", "reason"}。
    """
    tried = []
    # 只看啟用中的筆電：新人名下停用（報廢）的舊筆電不算已配發
    owned = [a for a in adm.get("/assets/", {"category": "laptop", "unassigned": "false", "is_active": "true"})
             if isinstance(a, dict) and a.get("custodian_hrm_employee_id") == employee_id]
    if owned:
        owned.sort(key=lambda a: (candidates.index(a["code"]) if a.get("code") in candidates else len(candidates),
                                  str(a.get("code"))))
        return {"status": "assigned", "asset_code": owned[0]["code"], "already": True, "tried": tried,
                **_placeholder_info(owned[0])}
    for code in candidates:
        rows = adm.get("/assets/", {"code": code, "is_active": "all"})
        asset = rows[0] if isinstance(rows, list) and rows else None
        if asset is None:
            tried.append({"asset_code": code, "result": "not_found"})
            continue
        if asset.get("custodian_hrm_employee_id") == employee_id:
            return {"status": "assigned", "asset_code": code, "already": True, "tried": tried,
                    **_placeholder_info(asset)}
        if asset.get("custodian") is not None:
            tried.append({"asset_code": code, "result": "assigned_to_other",
                          "custodian_username": asset.get("custodian_username")})
            continue
        if asset.get("is_active") is False:
            tried.append({"asset_code": code, "result": "inactive"})
            continue
        status, body = adm.post(f"/assets/{urllib.parse.quote(code, safe='')}/assign/",
                                {"custodian_hrm_employee_id": employee_id, **(new_hire or {})},
                                _assign_key(agent, flow_id))
        if status == 200 and isinstance(body, dict) and body.get("custodian_hrm_employee_id") == employee_id:
            return {"status": "assigned", "asset_code": code, "already": False, "tried": tried,
                    **_placeholder_info(body)}
        if status == 400:
            # 新人的 HRM ID 連到停用帳號（例如離職者）、或流程沒有新人姓名：換哪一台都一樣，交給人
            tried.append({"asset_code": code, "result": "http_400", "detail": c.short(body)})
            hint = "流程沒有新人姓名，ADM 無法自動建立保管人帳號" if not new_hire else \
                "新人的 HRM ID 在 ADM 連到停用的帳號？"
            return {"status": "needs_human", "tried": tried,
                    "reason": f"ADM 拒絕指派（{hint}）：{c.short(body)}"}
        if status in (401, 403):
            raise c.ServiceError(f"ADM 不允許 {agent} 指派保管人（{status}）：{c.short(body)}；"
                                 "IT 人員的 ADM 帳號要是 staff", announce=True, kind="forbidden")
        if status in (404, 409):
            tried.append({"asset_code": code, "result": f"http_{status}", "detail": c.short(body)})
            continue
        if status >= 500 or status == 429:
            raise c.ServiceError(f"ADM 指派 {code} 失敗 {status}: {c.short(body)}")
        tried.append({"asset_code": code, "result": f"http_{status}", "detail": c.short(body)})
        return {"status": "needs_human", "tried": tried, "reason": f"ADM 指派 {code} 回 {status}：{c.short(body)}"}
    return {"status": "needs_human", "tried": tried,
            "reason": f"建議與備選的筆電（{'、'.join(candidates) or '無'}）都已被指派、停用或不存在"}


def cmd_assign_laptop(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    # 指派保管人是 IT 人員的工作：不是 IT 的員工 agent 一律不做（--flow 也不能繞過）
    if c.AGENTS[agent]["role"] != IT_ROLE:
        result.update(outcome="not_it", detail=f"{agent} 不是 IT 人員，沒有配發筆電的任務")
        return True
    path = c.state_path(SKILL, agent, "assign-laptop.json")
    state = c.load_json(path)
    records = state.setdefault("flows", {})

    def persist():
        c.save_json(path, state)

    if args.dry_run:
        result.update(outcome="would_assign", records=records,
                      detail="dry-run：沒有連線 controller／ADM；實際執行會讀收件匣的 IT 任務、在 ADM 指派筆電並回報")
        return True

    ctl = Controller(agent)
    tasks: dict = {}
    if args.flow:
        tasks[args.flow] = []
    status, body = ctl.get("/inbox?" + urllib.parse.urlencode({"unread_only": "true", "limit": 200}))
    if status == 200 and isinstance(body, dict):
        for item in body.get("items") or []:
            if item.get("kind") != TASK_KIND or not item.get("flow_id") or item.get("read_at"):
                continue
            if args.flow and item["flow_id"] != args.flow:
                continue
            tasks.setdefault(str(item["flow_id"]), []).append(item.get("id"))
    elif not args.flow:
        raise c.ServiceError(f"讀 controller 收件匣失敗 {status}: {c.short(body)}",
                             announce=status in (401, 403))
    if not tasks:
        result.update(outcome="nothing_to_do", tasks=[], detail="收件匣沒有待處理的 IT 任務")
        return True

    adm = None
    done, skipped, needs_human, errors = [], [], [], []

    def mark_read(ids):
        for nid in ids:
            if nid is not None:
                ctl.post(f"/inbox/{nid}/read")

    for flow_id, notification_ids in tasks.items():
        status, flow = ctl.get(f"/flows/{urllib.parse.quote(flow_id, safe='')}")
        if status in (403, 404):
            skipped.append({"flow_id": flow_id, "reason": f"flow_http_{status}"})
            if status == 404:
                mark_read(notification_ids)
            continue
        if status != 200 or not isinstance(flow, dict):
            raise c.ServiceError(f"controller 查流程 {flow_id} 失敗 {status}: {c.short(flow)}")
        if flow.get("type") not in (None, "onboarding"):
            skipped.append({"flow_id": flow_id, "reason": "not_onboarding"})
            continue
        fstate = flow.get("state") or {}
        reported = ((fstate.get("steps") or {}).get("await_laptop") or {}).get("asset_code")
        record = records.setdefault(flow_id, {})
        if flow.get("status") in TERMINAL_FLOW and not reported:
            # 流程已結束（例如被取消）：current_step 可能還停在 await_laptop，絕不能再去指派
            record.setdefault("status", f"flow_{flow.get('status')}")
            persist()
            mark_read(notification_ids)
            skipped.append({"flow_id": flow_id, "reason": f"flow_{flow.get('status')}"})
            continue
        if flow.get("current_step") != "await_laptop":
            if reported or flow.get("status") in TERMINAL_FLOW:
                record.setdefault("status", "reported" if reported else f"flow_{flow.get('status')}")
                persist()
                mark_read(notification_ids)
                skipped.append({"flow_id": flow_id, "reason": "already_reported" if reported else
                                f"flow_{flow.get('status')}", "asset_code": reported})
            else:
                skipped.append({"flow_id": flow_id, "reason": "not_waiting",
                                "current_step": flow.get("current_step")})
            continue
        employee_id = (fstate.get("input") or {}).get("employee_id")
        candidates = _laptop_candidates(fstate)
        if not employee_id or not candidates:
            first = not record.get("announced")
            record.update(status="needs_human", reason="流程裡沒有新人 ID 或配發建議", announced=True)
            persist()
            needs_human.append({"flow_id": flow_id, "reason": record["reason"], "new": first})
            continue

        if adm is None:
            adm = adm_login(agent)
        outcome = _assign_on_adm(adm, agent, flow_id, employee_id, candidates, _new_hire_fields(fstate))
        record["tried"] = outcome.get("tried", [])
        if outcome["status"] != "assigned":
            first = not record.get("announced")
            record.update(status="needs_human", reason=outcome["reason"], announced=True)
            persist()
            needs_human.append({"flow_id": flow_id, "reason": outcome["reason"], "tried": outcome["tried"],
                                "new": first})
            continue
        code = outcome["asset_code"]
        suggested = candidates[0]
        record.update(status="assigned", asset_code=code, already=outcome["already"], assigned_at=c.iso(now))
        record.pop("reason", None)
        persist()

        notes = []
        if code != suggested:
            reasons = "、".join(f"{t['asset_code']} {t['result']}" for t in outcome.get("tried", []))
            notes.append(f"建議的 {suggested} 無法指派（{reasons or '已被指派'}），改指派 {code}")
        if outcome.get("placeholder"):
            record["placeholder_username"] = outcome.get("custodian_username")
            persist()
            notes.append(f"新人的 ADM 保管人帳號 {outcome.get('custodian_username')} 是自動建立、尚未啟用的佔位帳號"
                         "（之後啟用帳號時沿用）")
        event = {"type": EVENT_LAPTOP_ASSIGNED, "asset_code": code}
        if notes:
            event["note"] = "；".join(notes)
        status, resp = ctl.post(f"/flows/{urllib.parse.quote(flow_id, safe='')}/events", event)
        code_name = c.error_code(resp)
        if status == 409 and code_name == "step_not_current":
            # 可能是上次已送出但回應遺失：再查一次流程，確認收下的是同一台
            status2, again = ctl.get(f"/flows/{urllib.parse.quote(flow_id, safe='')}")
            got = (((again or {}).get("state") or {}).get("steps") or {}).get("await_laptop", {}) \
                if status2 == 200 and isinstance(again, dict) else {}
            if got.get("asset_code"):
                status = 200
        if status in (200, 202):
            record["status"] = "reported"
            persist()
            mark_read(notification_ids)
            entry = {"flow_id": flow_id, "asset_code": code, "already_assigned": outcome["already"],
                     "matches_suggestion": code == suggested}
            if outcome.get("placeholder"):
                entry["placeholder_custodian"] = outcome.get("custodian_username")
            done.append(entry)
        elif status >= 500 or status in (0, 429):
            errors.append({"flow_id": flow_id, "asset_code": code, "announce": False,
                           "error": f"回報 controller 失敗 {status}: {c.short(resp)}（重跑會再送一次）"})
        else:
            errors.append({"flow_id": flow_id, "asset_code": code, "announce": True,
                           "error": f"controller 拒絕 laptop_assigned（{status} {code_name}）: {c.short(resp)}"})

    result.update(assigned=done, skipped=skipped, needs_human=needs_human, errors=errors)
    announce_items = [n for n in needs_human if n.get("new", True)] + [e for e in errors if e["announce"]]
    if announce_items:
        result.update(announce=True, announce_kind="needs_human" if needs_human else "error")
    parts = []
    if done:
        parts.append("已指派並回報：" + "、".join(f"{d['asset_code']}（流程 {d['flow_id']}）" for d in done))
    if needs_human:
        parts.append("需要人處理：" + "；".join(n["reason"] for n in needs_human))
    if errors:
        parts.append("回報失敗：" + "；".join(e["error"] for e in errors))
    if errors:
        result["outcome"] = "error"
    elif needs_human:
        result["outcome"] = "needs_human"
    elif done:
        result["outcome"] = "assigned"
    else:
        result["outcome"] = "nothing_to_do"
    result["detail"] = "；".join(parts) or "IT 任務都已處理過或還不能處理"
    return not errors


def cmd_status(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    result.update(
        outcome="status",
        state=c.load_json(c.state_path(SKILL, agent, f"{today.isoformat()}.json")),
        detail="今日狀態查詢",
    )
    if args.dry_run:
        return True
    adm = adm_login(agent)

    def upcoming(rows):
        return [
            {k: r.get(k) for k in ("id", "room", "vehicle", "start_at", "end_at", "purpose", "status")}
            for r in rows
            if r.get("status") in ("active", "picked_up")
            and (c.parse_dt(r.get("end_at")) or now) >= now
        ]

    result["room_bookings"] = upcoming(adm.get("/room-bookings/"))
    result["vehicle_bookings"] = upcoming(adm.get("/vehicle-bookings/"))
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ADM 行政模擬（會議室與公務車預約）")
    parser.add_argument("command", choices=["decide", "book-room", "assign-laptop", "status"])
    parser.add_argument("--agent", help="OpenClaw agent id（預設讀 OPENCLAW_AGENT_ID）")
    parser.add_argument("--dry-run", action="store_true", help="不連網、不寫入，只顯示決策")
    parser.add_argument("--date", help="覆寫今天的日期 YYYY-MM-DD（測試用）")
    parser.add_argument("--now", help="覆寫現在時間（ISO，測試用）")
    parser.add_argument("--seed", type=int, help="固定亂數種子（測試用）")
    parser.add_argument("--force", choices=["room", "vehicle", "both", "none"],
                        help="decide：跳過擲骰直接指定")
    parser.add_argument("--on", help="book-room：預約日期 YYYY-MM-DD（預設今天起 3 個工作天內隨機）")
    parser.add_argument("--start", help="book-room：開始時間 HH:MM（衝突時會換時段）")
    parser.add_argument("--duration", type=int, help="book-room：分鐘數")
    parser.add_argument("--capacity", type=int, help="book-room：人數")
    parser.add_argument("--purpose", help="book-room：用途")
    parser.add_argument("--flow", help="assign-laptop：只處理這條流程（預設讀收件匣裡所有 IT 任務）")
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    if args.seed is not None:
        c.RNG.seed(args.seed)
    result: dict = {"command": args.command, "agent": args.agent or os.environ.get("OPENCLAW_AGENT_ID"),
                    "announce": False, "dry_run": args.dry_run}

    def handler() -> bool:
        agent = c.resolve_agent(args.agent)
        now = c.now_tz(args.now)
        today = date.fromisoformat(args.date) if args.date else now.date()
        result.update(agent=agent, date=today.isoformat())
        if args.command == "decide":
            return cmd_decide(agent, today, now, args, result)
        if args.command == "book-room":
            return cmd_book_room(agent, today, now, args, result)
        if args.command == "assign-laptop":
            return cmd_assign_laptop(agent, today, now, args, result)
        return cmd_status(agent, today, now, args, result)

    c.run_main(handler, result)


if __name__ == "__main__":
    main()
