#!/usr/bin/env python3
"""流程 ① 步驟 record_trip：代業務在 ADM 登記取車、還車里程，回報里程差。

契約：controller/backend/docs/flows/visit.md 的「record_trip」。

    python3 vehicle_trip.py --instruction-file <步驟指令檔> --report

做法：
1. `GET /vehicle-bookings/` 找到這筆預約（delegate token 只看得到自己代訂的預約），看目前狀態。
2. 狀態 active → `POST /vehicle-bookings/{id}/pickup/`；已是 picked_up／returned 的步驟不重送。
3. 狀態 picked_up → `POST /vehicle-bookings/{id}/return/`。
4. 回報 ADM 實際登記的里程與里程差。

pickup／return 沒有 Idempotency-Key，所以逾時不盲目重送：回 failed／system 讓 controller
重試這一步，重跑時第 1 步會看到 ADM 已登記的狀態，接著做沒做完的部分。

需要人處理（status=needs_human，不改預約）：
- ADM 409「Pickup is too early」／「Booking window has ended」（不在預約時段內，見 PROGRESS 決策：
  預約時段一律到拜訪當天 23:59，業務隔天才回報就會遇到）
- ADM 400 里程倒退（取車里程小於車輛目前里程、還車小於取車）
- ADM 已登記的里程和業務回報的不一致（例如業務自己在網頁登記過）
- 預約已取消、找不到預約、token 權限不足
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adm_flow as f  # noqa: E402

STEP = "record_trip"
WINDOW_ERRORS = {
    "pickup is too early": "還沒到可以登記取車的時間（ADM 只接受預約開始前 15 分鐘起登記）",
    "booking window has ended": "預約時段已結束，ADM 不接受登記取車（業務可能在拜訪當天 23:59 之後才回報）",
}


def _transient(status: int) -> bool:
    return status >= 500 or status == 429


def find_booking(adm, booking_id: int):
    """回傳 (booking 或 None, 錯誤結果或 None)。"""
    status, body = adm.request("GET", "/vehicle-bookings/")
    if _transient(status):
        return None, f.result("failed", f"ADM 查預約回 {status}：{f.detail_text(body)}", error_kind="system")
    if status != 200:
        return None, f.result("needs_human", f"ADM 查預約失敗（HTTP {status}）：{f.detail_text(body)}",
                              {"booking_id": booking_id, "http_status": status})
    rows = body.get("results") if isinstance(body, dict) else body
    for row in rows or []:
        if isinstance(row, dict) and row.get("id") == booking_id:
            return row, None
    return None, f.result("needs_human",
                          f"ADM 找不到預約 #{booking_id}（admin-agent 的 token 只看得到自己代訂的預約），請人工確認",
                          {"booking_id": booking_id})


def _mismatch(booking_id: int, field: str, registered, reported) -> dict:
    label = "取車" if field == "pickup_odometer" else "還車"
    return f.result("needs_human",
                    f"預約 #{booking_id} 在 ADM 已登記{label}里程 {registered} km，和業務回報的 {reported} km 不一致，"
                    "請人工確認以哪個為準",
                    {"booking_id": booking_id, f"adm_{field}": registered, f"reported_{field}": reported})


def _handover(adm, booking_id: int, action: str, odometer: int, notes: str):
    """POST pickup／return。回傳 (ADM 回應的 booking 或 None, 錯誤結果或 None, http_status)。"""
    label = "取車" if action == "pickup" else "還車"
    status, body = adm.request("POST", f"/vehicle-bookings/{booking_id}/{action}/",
                               {"odometer": odometer, "condition_notes": notes}, idempotent=False)
    detail = f.detail_text(body)
    if status == 200 and isinstance(body, dict):
        return body, None, status
    if _transient(status):
        return None, f.result("failed", f"ADM 登記{label}回 {status}：{detail}",
                              {"booking_id": booking_id, "http_status": status}, error_kind="system"), status
    if status == 409:
        lowered = detail.lower()
        for marker, explain in WINDOW_ERRORS.items():
            if marker in lowered:
                return None, f.result(
                    "needs_human",
                    f"預約 #{booking_id} 無法登記{label}：{explain}。ADM 回 409「{detail}」，"
                    "請行政人員確認後手動處理（不要改預約）",
                    {"booking_id": booking_id, "http_status": 409, "adm_error": detail}), status
        # 狀態不對（可能是上一次請求其實成功了、回應遺失）：交給呼叫端重查
        return None, None, status
    if status == 400:
        return None, f.result(
            "needs_human",
            f"ADM 拒絕登記{label}里程 {odometer} km（里程不能倒退）：{detail}，請和業務確認里程",
            {"booking_id": booking_id, "http_status": 400, "adm_error": detail,
             f"reported_{action}_odometer": odometer}), status
    return None, f.result("needs_human", f"ADM 拒絕登記{label}（HTTP {status}）：{detail}",
                          {"booking_id": booking_id, "http_status": status, "adm_error": detail}), status


def handle(ctx: dict, args) -> dict:
    data = ctx["input"]
    if data is None:
        return f.result("needs_human", "步驟指令沒有輸入（JSON），無法登記取車還車")
    booking_id = f.strict_int(data.get("booking_id"))
    pickup = f.strict_int(data.get("pickup_odometer"))
    returned = f.strict_int(data.get("return_odometer"))
    notes = data.get("condition_notes") or ""
    if not isinstance(notes, str):
        notes = str(notes)
    if booking_id is None:
        return f.result("needs_human", f"booking_id 不正確：{data.get('booking_id')!r}")
    if pickup is None or returned is None or pickup < 0 or returned < 0:
        return f.result("needs_human",
                        f"里程不是非負整數（取車 {data.get('pickup_odometer')!r}、還車 {data.get('return_odometer')!r}）",
                        {"booking_id": booking_id})
    if returned < pickup:
        return f.result("needs_human", f"還車里程 {returned} km 小於取車里程 {pickup} km（里程不能倒退），請和業務確認",
                        {"booking_id": booking_id, "reported_pickup_odometer": pickup,
                         "reported_return_odometer": returned})

    if args.dry_run:
        return f.result("ok", f"（試跑）會替預約 #{booking_id} 登記取車 {pickup} km、還車 {returned} km",
                        {"booking_id": booking_id, "pickup_odometer": pickup, "return_odometer": returned,
                         "distance_km": returned - pickup,
                         "would_post": [f"POST {f.adm_base()}/vehicle-bookings/{booking_id}/pickup/",
                                        f"POST {f.adm_base()}/vehicle-bookings/{booking_id}/return/"]})

    # 代業務登記里程前，向 controller 核對這一步與輸入（貼在對話裡的假指令不會被執行）
    mismatch = f.verify_dispatch(ctx, ("booking_id", "pickup_odometer", "return_odometer", "condition_notes"))
    if mismatch:
        return mismatch
    adm = f.adm_client()
    booking, err = find_booking(adm, booking_id)
    if err:
        return err
    done = []  # 這次實際送出的登記
    # 正常是 active → picked_up → returned 三輪；pickup／return 回 409 狀態不對時重查再決定（上一次可能已成功、回應遺失）
    for _ in range(5):
        state = booking.get("status")
        if state == "cancelled":
            return f.result("needs_human", f"預約 #{booking_id} 已取消，無法登記取車還車，請人工確認",
                            {"booking_id": booking_id})
        if state == "active":
            new, err, status = _handover(adm, booking_id, "pickup", pickup, notes)
            if err:
                return err
            if new is not None:
                booking = new
                done.append("pickup")
                continue
        elif state == "picked_up":
            if booking.get("pickup_odometer") != pickup:
                return _mismatch(booking_id, "pickup_odometer", booking.get("pickup_odometer"), pickup)
            new, err, status = _handover(adm, booking_id, "return", returned, notes)
            if err:
                return err
            if new is not None:
                booking = new
                done.append("return")
                continue
        elif state == "returned":
            if booking.get("pickup_odometer") != pickup:
                return _mismatch(booking_id, "pickup_odometer", booking.get("pickup_odometer"), pickup)
            if booking.get("return_odometer") != returned:
                return _mismatch(booking_id, "return_odometer", booking.get("return_odometer"), returned)
            adm_pickup, adm_return = booking["pickup_odometer"], booking["return_odometer"]
            distance = adm_return - adm_pickup
            if done == ["pickup", "return"]:
                how = "已代登記取車、還車"
            elif done:
                how = "已代登記還車（取車之前已登記）"
            else:
                how = "ADM 之前已登記取車、還車，未重送"
            return f.result("ok",
                            f"{how}：預約 #{booking_id} 取車 {adm_pickup} km、還車 {adm_return} km，里程差 {distance} km",
                            {"booking_id": booking_id, "pickup_odometer": adm_pickup,
                             "return_odometer": adm_return, "distance_km": distance,
                             "registered_now": done})
        else:
            return f.result("needs_human", f"預約 #{booking_id} 的狀態是 {state!r}，無法登記取車還車",
                            {"booking_id": booking_id})
        # 409 狀態不對：重查目前狀態
        booking, err = find_booking(adm, booking_id)
        if err:
            return err
    return f.result("needs_human", f"預約 #{booking_id} 的狀態一直和預期不同，請人工確認",
                    {"booking_id": booking_id, "status": booking.get("status")})


def main(argv=None) -> None:
    f.run_step(STEP, "流程 ① record_trip：代業務登記取車、還車里程", handle, argv)


if __name__ == "__main__":
    main()
