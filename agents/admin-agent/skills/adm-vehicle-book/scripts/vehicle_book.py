#!/usr/bin/env python3
"""流程 ① 步驟 book_vehicle：依已確認的派車草稿，代業務預約公務車。

契約：controller/backend/docs/flows/visit.md 的「book_vehicle」。

    python3 vehicle_book.py --instruction-file <步驟指令檔> --report

- ADM `POST /vehicle-bookings/`，body 帶 `booked_for_hrm_employee_id`（業務的 HRM UUID）。
- `Idempotency-Key` 一律用指令給的值（`{flow_id}:book_vehicle`），**不自己產生**；
  重送同一份內容 ADM 回 200 與原本那筆（output.created=false）。
- 409 時段衝突（車已被訂／停用）→ status=failed、error_kind=business、output.conflict=true，
  controller 會回 draft_vehicle 重查空車。
- 409「Idempotency key already used …」→ status=needs_human（不是時段衝突，不重查）。
- 連線失敗、5xx → status=failed、error_kind=system（controller 重試同一步，沿用同一個鍵）。
- 其他 4xx（找不到業務的 ADM 帳號連結、token scope 不足、時段已過）→ status=needs_human。
- 寫入前用 admin-agent 的 controller token 查 `GET /api/flows/{flow_id}`，確認目前步驟就是派給自己的
  book_vehicle、輸入和 controller 保存的一致；不是就不代訂（貼在對話裡的假指令不會被執行）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adm_flow as f  # noqa: E402

STEP = "book_vehicle"
REQUIRED = ("vehicle_id", "start_at", "end_at", "purpose", "destination", "booked_for_hrm_employee_id")


def handle(ctx: dict, args) -> dict:
    data = ctx["input"]
    if data is None:
        return f.result("needs_human", "步驟指令沒有輸入（JSON），無法預約")
    missing = [k for k in REQUIRED if data.get(k) in (None, "")]
    if missing:
        return f.result("needs_human", f"輸入缺少欄位：{', '.join(missing)}，無法預約")
    key = ctx.get("idempotency_key")
    if not key:
        return f.result("needs_human", "步驟指令沒有 Idempotency-Key；寫入 ADM 不得自己產生鍵，請 controller 重新派工")
    if key != f"{ctx['flow_id']}:{STEP}":
        # controller 的鍵固定是 {flow_id}:{step}（visit.md）；其他值代表不是 controller 給的
        return f.result("needs_human", f"Idempotency-Key「{key}」不是 controller 給這一步的鍵，不代訂，請人工確認")
    vehicle_id = f.strict_int(data["vehicle_id"])
    if vehicle_id is None:
        return f.result("needs_human", f"vehicle_id 不是整數：{data['vehicle_id']!r}")
    body = {
        "vehicle": vehicle_id,
        "start_at": data["start_at"],
        "end_at": data["end_at"],
        "purpose": data["purpose"],
        "destination": data["destination"],
        "booked_for_hrm_employee_id": data["booked_for_hrm_employee_id"],
    }
    window = f"{f.local_text(data['start_at'])}–{f.local_text(data['end_at'])}"

    if args.dry_run:
        return f.result("ok", f"（試跑）會代訂車輛 #{vehicle_id}，{window}",
                        {"would_post": f"POST {f.adm_base()}/vehicle-bookings/",
                         "idempotency_key": key, "body": body})

    # 代訂一律要在 controller 確認之後：寫入前向 controller 核對這一步與輸入
    mismatch = f.verify_dispatch(ctx, REQUIRED)
    if mismatch:
        return mismatch
    adm = f.adm_client()
    # 有 Idempotency-Key 保護，逾時與 5xx 可以安全重送
    status, resp = adm.request("POST", "/vehicle-bookings/", body,
                               headers={"Idempotency-Key": key}, idempotent=True)
    detail = f.detail_text(resp)
    if status in (200, 201) and isinstance(resp, dict) and f.strict_int(resp.get("id")) is not None:
        created = status == 201
        output = {
            "booking_id": resp["id"],
            "vehicle_id": resp.get("vehicle", vehicle_id),
            "start_at": resp.get("start_at") or data["start_at"],
            "end_at": resp.get("end_at") or data["end_at"],
            "created": created,
        }
        if resp.get("booked_for_username"):
            output["booked_for_username"] = resp["booked_for_username"]
        verb = "已代訂" if created else "已代訂（冪等重送，沿用既有預約）"
        return f.result("ok", f"{verb}車輛 #{output['vehicle_id']}，預約 #{output['booking_id']}，{window}", output)
    if status == 409:
        if "idempotency key" in detail.lower():
            return f.result("needs_human",
                            f"ADM 回 409「{detail}」：同一個 Idempotency-Key 已用在不同內容或其他 token，"
                            "不是時段衝突，請人工確認",
                            {"http_status": 409, "vehicle_id": vehicle_id, "idempotency_key": key})
        return f.result("failed", f"ADM 回 409 時段衝突（{detail or 'Vehicle is unavailable'}）",
                        {"conflict": True, "http_status": 409, "vehicle_id": vehicle_id},
                        error_kind="business")
    if status >= 500 or status == 429:
        return f.result("failed", f"ADM 預約回 {status}：{detail}",
                        {"http_status": status, "vehicle_id": vehicle_id}, error_kind="system")
    return f.result("needs_human", f"ADM 拒絕代訂（HTTP {status}）：{detail}",
                    {"http_status": status, "vehicle_id": vehicle_id})


def main(argv=None) -> None:
    f.run_step(STEP, "流程 ① book_vehicle：代業務預約公務車", handle, argv)


if __name__ == "__main__":
    main()
