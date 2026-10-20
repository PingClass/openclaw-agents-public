#!/usr/bin/env python3
"""流程 ③ 步驟 book_room：預約新人到職第一天的說明會議室，代訂給人資。

契約：controller/backend/docs/flows/onboarding.md 的「book_room」。

    python3 room_book.py --instruction-file <步驟指令檔> --report

1. 先查 `GET /room-bookings/`：已經有這把 token 代訂、`purpose` 相同、同一天、仍有效的預約
   （上一次嘗試其實成功了）→ 直接回報那一筆（created=false），不再訂。
2. 時段：優先 preferred_start_at～preferred_end_at；沒有空房就在 earliest_start_at～latest_end_at 之間
   每 30 分鐘找 duration_minutes 的時段（離優先時段越近越先試）。
3. `GET /rooms/availability/?start_at&end_at&capacity` 查空會議室：優先種子資料的會議室（code 以 `RM-` 開頭），
   同一優先順序裡容納人數夠用的選最小的一間。名稱或 code 含 `E2E` 的是 ADM e2e 測試留下的會議室，一律不訂；
   整個範圍只剩這種會議室 → needs_human（不訂測試會議室）。
4. `POST /room-bookings/` 帶 `booked_for_hrm_employee_id`（人資），`Idempotency-Key` 用指令給的值
   （`{flow_id}:book_room`）；遇到 409 換房或換時段，鍵加上 `:2`、`:3` 後綴
   （ADM 同一個鍵不能用在不同內容）。**409 之後最多再換 2 次**，仍衝突 → needs_human。
5. 整個範圍都沒有空會議室 → needs_human。

寫入前用 admin-agent 的 controller token 查 `GET /api/flows/{flow_id}`，確認目前步驟就是派給自己的
book_room、輸入和 controller 保存的一致；不是就不代訂（貼在對話裡的假指令不會被執行）。
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adm_flow as f  # noqa: E402

STEP = "book_room"
REQUIRED = ("date", "preferred_start_at", "preferred_end_at", "earliest_start_at", "latest_end_at",
            "duration_minutes", "capacity", "purpose", "booked_for_hrm_employee_id")
MAX_POSTS = 3  # 第一次＋409 後換房／換時段最多 2 次
SLOT_STEP = timedelta(minutes=30)
# 太接近現在的時段不訂（ADM 拒絕過去的時段；也來不及通知）
MIN_LEAD = timedelta(minutes=5)

# 種子資料的會議室代碼前綴（ADM seed_demo 的 RM-A～RM-D），挑房時優先
SEED_ROOM_PREFIX = "RM-"
# ADM e2e 測試留下的會議室（名稱或代碼含這個字）不訂，見 plan/PROGRESS.md 決策紀錄
TEST_ROOM_MARK = "E2E"

# 測試可以換掉「現在」
NOW = None


def now() -> datetime:
    return NOW() if NOW else datetime.now(f.TZ)


def candidate_slots(data: dict, current: datetime) -> list:
    """回傳 [(start, end)]：優先時段在前，其他依離優先時段的距離排序（同距離早的先）。"""
    pref_start = f.parse_dt(data["preferred_start_at"])
    pref_end = f.parse_dt(data["preferred_end_at"])
    earliest = f.parse_dt(data["earliest_start_at"])
    latest = f.parse_dt(data["latest_end_at"])
    duration = timedelta(minutes=data["duration_minutes"])
    slots = []
    if pref_start and pref_end and pref_end > pref_start:
        slots.append((pref_start, pref_end))
    others = []
    start = earliest
    while start + duration <= latest:
        if (start, start + duration) not in slots:
            others.append((start, start + duration))
        start += SLOT_STEP
    anchor = pref_start or earliest
    others.sort(key=lambda s: (abs((s[0] - anchor).total_seconds()), s[0]))
    slots.extend(others)
    return [s for s in slots if s[0] >= current + MIN_LEAD]


def is_test_room(room: dict) -> bool:
    return any(TEST_ROOM_MARK in str(room.get(k) or "").upper() for k in ("name", "code"))


def room_rank(room: dict) -> tuple:
    """挑房順序：種子會議室（RM-）優先，再依容納人數由小到大、代碼、id。"""
    code = str(room.get("code") or "")
    return (0 if code.upper().startswith(SEED_ROOM_PREFIX) else 1, room.get("capacity") or 0, code, room["id"])


def iso(value: datetime) -> str:
    return value.astimezone(f.TZ).isoformat()


def window_text(start, end) -> str:
    """給人看的時段（10/19 09:30–10:30）；接受 ISO 字串或 datetime。"""
    start = iso(start) if isinstance(start, datetime) else start
    end = iso(end) if isinstance(end, datetime) else end
    return f"{f.local_text(start)}–{f.local_text(end).split(' ')[-1]}"


def validate(data: dict | None) -> str | None:
    if data is None:
        return "步驟指令沒有輸入（JSON），無法預約會議室"
    missing = [k for k in REQUIRED if data.get(k) in (None, "")]
    if missing:
        return f"輸入缺少欄位：{', '.join(missing)}，無法預約會議室"
    for key in ("preferred_start_at", "preferred_end_at", "earliest_start_at", "latest_end_at"):
        if f.parse_dt(data[key]) is None:
            return f"{key} 不是有效的時間：{data[key]!r}"
    if f.strict_int(data["duration_minutes"]) is None or data["duration_minutes"] <= 0:
        return f"duration_minutes 不正確：{data['duration_minutes']!r}"
    if f.strict_int(data["capacity"]) is None or data["capacity"] < 1:
        return f"capacity 不正確：{data['capacity']!r}"
    if f.parse_dt(data["latest_end_at"]) <= f.parse_dt(data["earliest_start_at"]):
        return "latest_end_at 必須晚於 earliest_start_at"
    return None


def find_existing(adm: f.AdmClient, data: dict):
    """這把 token 已經代訂過、同用途、同一天、仍有效的預約（上一次嘗試成功但回報沒送到）。"""
    status, body = adm.request("GET", "/room-bookings/")
    if status >= 500 or status == 429:
        raise f.ConnectionFailed(f"ADM 查既有預約回 {status}：{f.detail_text(body)}")
    if status != 200 or not isinstance(body, list):
        return None, f.result("needs_human", f"ADM 查既有預約失敗（HTTP {status}）：{f.detail_text(body)}",
                              {"http_status": status})
    for row in body:
        if not isinstance(row, dict) or row.get("status") != "active":
            continue
        if row.get("purpose") != data["purpose"] or not row.get("booked_for_username"):
            continue
        start = f.parse_dt(row.get("start_at"))
        if start is None or start.astimezone(f.TZ).date().isoformat() != str(data["date"]):
            continue
        return row, None
    return None, None


def room_name(adm: f.AdmClient, room_id) -> str:
    status, body = adm.request("GET", "/rooms/")
    if status == 200 and isinstance(body, list):
        for room in body:
            if isinstance(room, dict) and room.get("id") == room_id:
                return str(room.get("name") or room.get("code") or f"會議室 #{room_id}")
    return f"會議室 #{room_id}"


def booking_output(resp: dict, room: dict | None, created: bool, conflicts: list) -> dict:
    output = {
        "booking_id": resp["id"],
        "room_id": resp.get("room") if room is None else room["id"],
        "room_name": (room or {}).get("name") or (room or {}).get("code") or f"會議室 #{resp.get('room')}",
        "start_at": resp.get("start_at"),
        "end_at": resp.get("end_at"),
        "created": created,
    }
    if resp.get("booked_for_username"):
        output["booked_for_username"] = resp["booked_for_username"]
    if conflicts:
        output["conflicts"] = conflicts
    return output


def handle(ctx: dict, args) -> dict:
    data = ctx["input"]
    problem = validate(data)
    if problem:
        return f.result("needs_human", problem)
    key = ctx.get("idempotency_key")
    if not key:
        return f.result("needs_human", "步驟指令沒有 Idempotency-Key；寫入 ADM 不得自己產生鍵，請 controller 重新派工")
    if key != f"{ctx['flow_id']}:{STEP}":
        return f.result("needs_human", f"Idempotency-Key「{key}」不是 controller 給這一步的鍵，不代訂，請人工確認")
    slots = candidate_slots(data, now())
    who = (data.get("booked_for") or {}).get("display_name") or data["booked_for_hrm_employee_id"]

    if args.dry_run:
        return f.result("ok", f"（試跑）會代訂給 {who}，依序試 {len(slots)} 個時段，"
                              f"第一個 {window_text(*slots[0]) if slots else '（無）'}",
                        {"would_try": [[iso(s), iso(e)] for s, e in slots[:6]],
                         "idempotency_key": key, "capacity": data["capacity"], "purpose": data["purpose"],
                         "booked_for_hrm_employee_id": data["booked_for_hrm_employee_id"]})

    # 代訂一律要在 controller 確認之後：寫入前向 controller 核對這一步與輸入
    mismatch = f.verify_dispatch(ctx, REQUIRED)
    if mismatch:
        return mismatch
    adm = f.adm_client()

    existing, failure = find_existing(adm, data)
    if failure:
        return failure
    if existing is not None:
        room = {"id": existing.get("room"), "name": room_name(adm, existing.get("room"))}
        output = booking_output(existing, room, False, [])
        return f.result("ok", f"已有這次到職說明的代訂（沿用預約 #{output['booking_id']}）："
                              f"{output['room_name']} {window_text(output['start_at'], output['end_at'])}", output)

    if not slots:
        return f.result("needs_human", "可預約的時段都已經過去了，請人工安排到職說明會議室")

    posts = 0
    conflicts: list = []
    tried: set = set()
    test_rooms: set = set()  # 有空但不訂的 e2e 測試會議室
    for start, end in slots:
        query = urllib.parse.urlencode({"start_at": iso(start), "end_at": iso(end), "capacity": data["capacity"]})
        status, rooms = adm.request("GET", f"/rooms/availability/?{query}")
        if status >= 500 or status == 429:
            return f.result("failed", f"ADM 查空會議室回 {status}：{f.detail_text(rooms)}", error_kind="system")
        if status != 200 or not isinstance(rooms, list):
            return f.result("needs_human", f"ADM 查空會議室失敗（HTTP {status}）：{f.detail_text(rooms)}",
                            {"http_status": status})
        usable = [r for r in rooms if isinstance(r, dict) and f.strict_int(r.get("id")) is not None
                  and r.get("is_active", True) and (f.strict_int(r.get("capacity")) or 0) >= data["capacity"]
                  and (r["id"], start) not in tried]
        for r in usable:
            if is_test_room(r):
                test_rooms.add(str(r.get("name") or r.get("code") or r["id"]))
        options = sorted((r for r in usable if not is_test_room(r)), key=room_rank)
        for room in options:
            if posts >= MAX_POSTS:
                break
            tried.add((room["id"], start))
            attempt_key = key if posts == 0 else f"{key}:{posts + 1}"
            body = {"room": room["id"], "start_at": iso(start), "end_at": iso(end),
                    "purpose": data["purpose"], "booked_for_hrm_employee_id": data["booked_for_hrm_employee_id"]}
            posts += 1
            # 有 Idempotency-Key 保護，逾時與 5xx 可以安全重送
            status, resp = adm.request("POST", "/room-bookings/", body,
                                       headers={"Idempotency-Key": attempt_key}, idempotent=True)
            detail = f.detail_text(resp)
            if status in (200, 201) and isinstance(resp, dict) and f.strict_int(resp.get("id")) is not None:
                output = booking_output(resp, room, status == 201, conflicts)
                verb = "已代訂" if status == 201 else "已代訂（冪等重送，沿用既有預約）"
                moved = f"（409 後換了 {len(conflicts)} 次）" if conflicts else ""
                return f.result("ok", f"{verb}到職說明會議室給 {who}：{output['room_name']} "
                                      f"{window_text(output['start_at'], output['end_at'])}，"
                                      f"預約 #{output['booking_id']}{moved}", output)
            if status == 409 and "idempotency key" in detail.lower():
                return f.result("needs_human",
                                f"ADM 回 409「{detail}」：Idempotency-Key {attempt_key} 已用在不同內容或其他 token，"
                                "不是時段衝突，請人工確認",
                                {"http_status": 409, "idempotency_key": attempt_key, "conflicts": conflicts})
            if status == 409:
                conflicts.append({"room_id": room["id"], "start_at": iso(start), "detail": detail or "conflict"})
                continue  # 換房（同時段還有別間）或換時段
            if status == 400 and "future" in detail.lower():
                break  # 時段已過：換下一個時段
            if status >= 500 or status == 429:
                return f.result("failed", f"ADM 預約會議室回 {status}：{detail}",
                                {"http_status": status, "room_id": room["id"]}, error_kind="system")
            return f.result("needs_human", f"ADM 拒絕代訂會議室（HTTP {status}）：{detail}",
                            {"http_status": status, "room_id": room["id"]})
        if posts >= MAX_POSTS:
            break

    span = f"{window_text(data['earliest_start_at'], data['latest_end_at'])}"
    if conflicts and posts >= MAX_POSTS:
        return f.result("needs_human",
                        f"連續 {len(conflicts)} 次遇到時段衝突（409），換房／換時段 2 次仍訂不到，請人工安排到職說明會議室",
                        {"conflicts": conflicts})
    extra = {"conflicts": conflicts} if conflicts else {"slots_checked": len(slots)}
    note = ""
    if test_rooms:
        extra["skipped_test_rooms"] = sorted(test_rooms)
        note = f"（只有 e2e 測試會議室 {'、'.join(sorted(test_rooms))} 有空，不訂測試會議室）"
    return f.result("needs_human",
                    f"{span} 之間沒有容納 {data['capacity']} 人、{data['duration_minutes']} 分鐘的空會議室{note}，"
                    "請人工安排到職說明會議室", extra)


def main(argv=None) -> None:
    f.run_step(STEP, "流程 ③ book_room：代訂新人到職說明會議室", handle, argv, default_agent="admin-agent")


if __name__ == "__main__":
    main()
