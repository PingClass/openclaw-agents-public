#!/usr/bin/env python3
"""流程 ① 步驟 draft_vehicle：查空車，產生派車草稿（不預約）。

契約：controller/backend/docs/flows/visit.md 的「draft_vehicle」。

    python3 vehicle_draft.py --instruction-file <步驟指令檔> --report

挑車規則：ADM availability（start_at／end_at＝輸入的 booking_start_at／booking_end_at、seats）
回來的車，排除 exclude_vehicle_ids（與 --exclude），座位數夠用的車裡選座位最少的（不浪費大車），
同座位數依車輛代碼排序。業務退回重排時，可用 --prefer／--min-seats 依意見調整。
"""

from __future__ import annotations

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adm_flow as f  # noqa: E402

STEP = "draft_vehicle"
REQUIRED = ("booking_start_at", "booking_end_at", "destination", "purpose")


def add_args(parser) -> None:
    parser.add_argument("--exclude", action="append", default=[],
                        help="另外要排除的車輛 id 或代碼（可重複；例如業務意見指定不要的車）")
    parser.add_argument("--prefer", help="優先選這台（車輛 id 或代碼；例如業務意見指定的車），沒空就照一般規則")
    parser.add_argument("--min-seats", type=int, help="至少要幾個座位（業務意見要大車時用；不會小於輸入的 seats）")


def _matches(vehicle: dict, ref: str) -> bool:
    ref = str(ref).strip()
    return ref != "" and ref in (str(vehicle.get("id")), str(vehicle.get("code") or ""),
                                 str(vehicle.get("plate_number") or ""))


def choose(vehicles: list, seats: int, exclude: list, prefer: str | None) -> dict | None:
    candidates = [v for v in vehicles
                  if isinstance(v, dict) and f.strict_int(v.get("id")) is not None
                  and v.get("is_active", True)
                  and (f.strict_int(v.get("seats")) or 0) >= seats
                  and not any(_matches(v, ref) for ref in exclude)]
    if not candidates:
        return None
    if prefer:
        for v in candidates:
            if _matches(v, prefer):
                return v
    candidates.sort(key=lambda v: (v.get("seats") or 0, str(v.get("code") or ""), v["id"]))
    return candidates[0]


def handle(ctx: dict, args) -> dict:
    data = ctx["input"]
    if data is None:
        return f.result("needs_human", "步驟指令沒有輸入（JSON），無法查空車")
    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        return f.result("needs_human", f"輸入缺少欄位：{', '.join(missing)}，無法查空車")
    start, end = data["booking_start_at"], data["booking_end_at"]
    start_dt, end_dt = f.parse_dt(start), f.parse_dt(end)
    if start_dt is None or end_dt is None or end_dt <= start_dt:
        return f.result("needs_human", f"預約時段不正確：{start} → {end}")
    seats = data.get("seats") if data.get("seats") is not None else 1
    if f.strict_int(seats) is None or seats < 1:
        return f.result("needs_human", f"人數不正確：{seats!r}")
    need = max(seats, args.min_seats or 0)
    exclude = [str(v) for v in (data.get("exclude_vehicle_ids") or [])] + list(args.exclude)
    query = urllib.parse.urlencode({"start_at": start, "end_at": end, "seats": need})
    path = f"/vehicles/availability/?{query}"
    window = f"{f.local_text(start)}–{f.local_text(end)}"

    if args.dry_run:
        return f.result("ok", f"（試跑）會查 {window}、{need} 人座以上的空車，排除 {exclude or '無'}",
                        {"would_query": f"GET {f.adm_base()}{path}", "start_at": start, "end_at": end,
                         "destination": data["destination"], "purpose": data["purpose"]})

    adm = f.adm_client()
    status, body = adm.request("GET", path)
    if status >= 500 or status == 429:
        return f.result("failed", f"ADM 查空車回 {status}：{f.detail_text(body)}", error_kind="system")
    if status != 200 or not isinstance(body, list):
        return f.result("needs_human", f"ADM 查空車失敗（HTTP {status}）：{f.detail_text(body)}",
                        {"http_status": status})
    vehicle = choose(body, need, exclude, args.prefer)
    if vehicle is None:
        excluded = f"（已排除 {', '.join(exclude)}）" if exclude else ""
        return f.result("needs_human",
                        f"{window} 沒有 {need} 人座以上的空車{excluded}，請人工安排或請業務改時段",
                        {"available_count": len(body), "seats": need, "exclude_vehicle_ids": exclude})
    output = {
        "vehicle_id": vehicle["id"],
        "vehicle_code": str(vehicle.get("code") or vehicle.get("plate_number") or f"#{vehicle['id']}"),
        "vehicle_name": vehicle.get("name") or None,
        "seats": vehicle.get("seats"),
        "plate_number": vehicle.get("plate_number") or None,
        "start_at": start,
        "end_at": end,
        "destination": data["destination"],
        "purpose": data["purpose"],
        "available_count": len(body),
    }
    output = {k: v for k, v in output.items() if v is not None}
    label = output["vehicle_code"] + (f"（{output['vehicle_name']}）" if output.get("vehicle_name") else "")
    return f.result("ok", f"派車草稿：{label}，{window}，目的地 {data['destination']}", output)


def main(argv=None) -> None:
    f.run_step(STEP, "流程 ① draft_vehicle：查空車，產生派車草稿", handle, argv, add_args)


if __name__ == "__main__":
    main()
