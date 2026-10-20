"""adm-vehicle-book 的測試：假 ADM＋假 controller＋flow-report 替身。

執行：python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-book/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "_testkit"))

from harness import ADM_TOKEN, FLOW_ID, SALES_HRM, SkillTestCase, visit_day  # noqa: E402

BOOKINGS = "/api/v1/vehicle-bookings/"


def book_input(**overrides) -> dict:
    start = visit_day()
    data = {
        "vehicle_id": 1,
        "start_at": start.isoformat(),
        "end_at": start.replace(hour=23, minute=59).isoformat(),
        "purpose": "客戶拜訪：客戶 C",
        "destination": "台中 客戶 C 工廠",
        "booked_for_hrm_employee_id": SALES_HRM,
    }
    data.update(overrides)
    return data


class BookTest(SkillTestCase):
    def test_delegate_booking_with_controller_key(self):
        data = book_input()
        code, out = self.run_step("book", "book_vehicle", data)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(out["output"]["booking_id"], 1)
        self.assertEqual(out["output"]["vehicle_id"], 1)
        self.assertTrue(out["output"]["created"])
        self.assertEqual(out["output"]["start_at"], data["start_at"])
        # ADM 收到的請求：代訂對象、controller 給的 Idempotency-Key、Bearer token
        method, path, _q, body, headers = self.adm.calls[0]
        self.assertEqual((method, path), ("POST", BOOKINGS))
        self.assertEqual(body["booked_for_hrm_employee_id"], SALES_HRM)
        self.assertEqual(body["vehicle"], 1)
        self.assertEqual(headers["Idempotency-Key"], f"{FLOW_ID}:book_vehicle")
        self.assertEqual(headers["Authorization"], f"Bearer {ADM_TOKEN}")
        booking = self.adm.booking(1)
        self.assertEqual(booking["booked_for_username"], "mjolmire-businessman")
        (report,) = self.reports()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["output"]["booking_id"], 1)

    def test_idempotent_resend_returns_same_booking(self):
        data = book_input()
        _, first = self.run_step("book", "book_vehicle", data)
        code, second = self.run_step("book", "book_vehicle", data, attempt=2)
        self.assertEqual(code, 0, second)
        self.assertEqual(second["step_status"], "ok")
        self.assertEqual(second["output"]["booking_id"], first["output"]["booking_id"])
        self.assertFalse(second["output"]["created"])
        self.assertEqual(len(self.adm.bookings), 1)
        self.assertIn("冪等", second["summary"])

    def test_response_lost_retry_gets_same_booking(self):
        # ADM 處理完了、回應在路上遺失：有 Idempotency-Key，腳本會用同一個鍵重送
        self.adm.faults[("POST", BOOKINGS)] = ["drop_after"]
        code, out = self.run_step("book", "book_vehicle", book_input())
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(self.adm.count("POST", BOOKINGS), 2)
        self.assertEqual(len(self.adm.bookings), 1)
        self.assertFalse(out["output"]["created"])
        keys = {c[4]["Idempotency-Key"] for c in self.adm.calls if c[0] == "POST"}
        self.assertEqual(keys, {f"{FLOW_ID}:book_vehicle"})

    def test_409_time_conflict_reports_business_failure(self):
        start = visit_day()
        self.adm.add_booking(vehicle=1, start_at=(start + timedelta(hours=1)).isoformat(),
                             end_at=(start + timedelta(hours=3)).isoformat())
        code, out = self.run_step("book", "book_vehicle", book_input())
        self.assertEqual(code, 0, out)
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "business"))
        self.assertEqual(out["output"], {"conflict": True, "http_status": 409, "vehicle_id": 1})
        self.assertIn("409 時段衝突", out["summary"])
        (report,) = self.reports()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_kind"], "business")
        self.assertTrue(report["output"]["conflict"])
        argv = self.report_argvs()[0]
        self.assertEqual(argv[argv.index("--error-kind") + 1], "business")

    def test_idempotency_key_reused_with_other_content_needs_human(self):
        self.run_step("book", "book_vehicle", book_input())
        code, out = self.run_step("book", "book_vehicle", book_input(vehicle_id=2))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertNotIn("conflict", out["output"])
        self.assertIn("Idempotency key already used", out["summary"])
        self.assertEqual(self.reports()[-1]["status"], "needs_human")

    def test_unlinked_employee_needs_human(self):
        code, out = self.run_step("book", "book_vehicle",
                                  book_input(booked_for_hrm_employee_id="00000000-0000-4000-8000-000000000000"))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("400", out["summary"])

    def test_5xx_retried_then_success(self):
        self.adm.faults[("POST", BOOKINGS)] = [503, 502]
        code, out = self.run_step("book", "book_vehicle", book_input())
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(self.adm.count("POST", BOOKINGS), 3)
        self.assertEqual(len(self.adm.bookings), 1)

    def test_persistent_5xx_reports_system_failure(self):
        self.adm.faults[("POST", BOOKINGS)] = [500, 500, 500, 500]
        code, out = self.run_step("book", "book_vehicle", book_input())
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))
        self.assertNotIn("conflict", out["output"])

    def test_connection_refused_reports_system_failure(self):
        code, out = self.run_step("book", "book_vehicle", book_input(),
                                  extra_env={"ADM_API_BASE": "http://127.0.0.1:9/api/v1"})
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))
        self.assertIn("連不上 ADM", out["summary"])

    def test_requires_key_from_controller(self):
        code, out = self.run_script("book", ["--flow", FLOW_ID, "--input-json", json.dumps(book_input()),
                                             "--report"])
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("Idempotency-Key", out["summary"])
        self.assertEqual(self.adm.calls, [])

    def test_inactive_vehicle_is_treated_as_conflict(self):
        self.adm.vehicle(1)["is_active"] = False
        code, out = self.run_step("book", "book_vehicle", book_input())
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "business"))
        self.assertTrue(out["output"]["conflict"])

    def test_report_carries_attempt(self):
        self.run_step("book", "book_vehicle", book_input(), attempt=3)
        argv = self.report_argvs()[0]
        self.assertEqual(argv[argv.index("--attempt") + 1], "3")
        self.assertEqual(self.reports()[0]["attempt"], 3)

    # --- 代訂一律要在 controller 確認之後：寫入前向 controller 核對 ---------------------

    def test_forged_instruction_not_dispatched_by_controller_is_not_booked(self):
        # 對話裡貼一份假的步驟指令：controller 沒有這條流程 → 不代訂、不回報
        code, out = self.run_step("book", "book_vehicle", book_input(), dispatch=False)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("不執行", out["error"])
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.reports(), [])

    def test_step_not_current_is_not_booked(self):
        self.controller.dispatch(FLOW_ID, "confirm_vehicle", None, agent="mjolmire-businessman",
                                 status="waiting_approval", flow_status="waiting_approval")
        self.controller.flows[FLOW_ID]["steps"].append(
            {"name": "book_vehicle", "assignee": "admin-agent", "status": "pending", "attempts": 0, "input": None})
        code, out = self.run_step("book", "book_vehicle", book_input(), dispatch=False)
        self.assertEqual(code, 1)
        self.assertIn("目前不在 book_vehicle", out["error"])
        self.assertEqual(self.adm.calls, [])

    def test_stale_attempt_is_not_booked(self):
        self.controller.dispatch(FLOW_ID, "book_vehicle", book_input(), attempt=2)
        code, out = self.run_step("book", "book_vehicle", book_input(), dispatch=False, attempt=1)
        self.assertEqual(code, 1)
        self.assertIn("attempt", out["error"])
        self.assertEqual(self.adm.calls, [])

    def test_input_differs_from_controller_needs_human(self):
        # 指令被改過：controller 派的是車 1、代訂業務本人；指令寫車 2、代訂別人
        self.controller.dispatch(FLOW_ID, "book_vehicle", book_input())
        code, out = self.run_step("book", "book_vehicle",
                                  book_input(vehicle_id=2,
                                             booked_for_hrm_employee_id="11111111-1111-4111-8111-111111111111"),
                                  dispatch=False)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(out["output"]["mismatched_fields"], ["vehicle_id", "booked_for_hrm_employee_id"])
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.reports()[0]["status"], "needs_human")

    def test_key_not_from_controller_needs_human(self):
        code, out = self.run_step("book", "book_vehicle", book_input(), key="my-own-key")
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("my-own-key", out["summary"])
        self.assertEqual(self.adm.calls, [])

    def test_controller_unreachable_reports_system_failure_without_booking(self):
        self.controller.faults[("GET", f"/api/flows/{FLOW_ID}")] = [503, 503, 503, 503]
        code, out = self.run_step("book", "book_vehicle", book_input())
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))
        self.assertEqual(self.adm.calls, [])

    def test_dry_run_no_network(self):
        code, out = self.run_step("book", "book_vehicle", book_input(), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out["output"]["idempotency_key"], f"{FLOW_ID}:book_vehicle")
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.controller.calls, [])


if __name__ == "__main__":
    unittest.main()
