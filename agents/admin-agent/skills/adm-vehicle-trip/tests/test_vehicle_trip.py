"""adm-vehicle-trip 的測試：假 ADM＋假 controller＋flow-report 替身。

執行：python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-trip/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "_testkit"))

from harness import ADM_TOKEN, FLOW_ID, SkillTestCase  # noqa: E402

PICKUP = "/api/v1/vehicle-bookings/1/pickup/"
RETURN = "/api/v1/vehicle-bookings/1/return/"


def trip_input(**overrides) -> dict:
    data = {"booking_id": 1, "pickup_odometer": 12000, "return_odometer": 12037,
            "condition_notes": "客戶對新品有興趣，下週報價"}
    data.update(overrides)
    return data


class TripTest(SkillTestCase):
    def setUp(self):
        super().setUp()
        now = datetime.now(timezone.utc)
        # 拜訪當天傍晚回報：預約 10:00 開始、到 23:59 結束，現在在時段內
        self.booking = self.adm.add_booking(
            vehicle=1, token=ADM_TOKEN, booked_for="mjolmire-businessman",
            start_at=(now - timedelta(hours=8)).isoformat(), end_at=(now + timedelta(hours=5)).isoformat())

    def test_records_pickup_and_return(self):
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(out["output"], {"booking_id": 1, "pickup_odometer": 12000, "return_odometer": 12037,
                                         "distance_km": 37, "registered_now": ["pickup", "return"]})
        self.assertEqual(self.booking["status"], "returned")
        self.assertEqual(self.adm.vehicle(1)["odometer"], 12037)
        pickup_call = next(c for c in self.adm.calls if c[1] == PICKUP)
        self.assertEqual(pickup_call[3], {"odometer": 12000, "condition_notes": "客戶對新品有興趣，下週報價"})
        (report,) = self.reports()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["output"]["distance_km"], 37)

    def test_rerun_does_not_resend(self):
        self.run_step("trip", "record_trip", trip_input())
        code, out = self.run_step("trip", "record_trip", trip_input(), attempt=2)
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(out["output"]["distance_km"], 37)
        self.assertEqual(out["output"]["registered_now"], [])
        self.assertEqual(self.adm.count("POST", PICKUP), 1)
        self.assertEqual(self.adm.count("POST", RETURN), 1)

    def test_already_picked_up_only_returns(self):
        self.booking.update(status="picked_up", pickup_odometer=12000)
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(self.adm.count("POST", PICKUP), 0)
        self.assertEqual(self.adm.count("POST", RETURN), 1)
        self.assertIn("取車之前已登記", out["summary"])

    def test_lost_pickup_response_then_retry_continues(self):
        # 取車已登記但回應遺失：pickup 不重送，這次回 failed/system；controller 重試時接著做還車
        self.adm.faults[("POST", PICKUP)] = ["drop_after"]
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))
        self.assertEqual(self.booking["status"], "picked_up")
        code, out = self.run_step("trip", "record_trip", trip_input(), attempt=2)
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(self.adm.count("POST", PICKUP), 1)
        self.assertEqual(out["output"]["distance_km"], 37)

    def test_stale_state_409_refetches(self):
        # pickup 回 409「Only an active booking…」（別處剛登記）→ 重查後接著還車
        original = self.adm._handover

        def handover(booking_id, action, body, info, token):
            if action == "pickup" and self.booking["status"] == "active":
                self.booking.update(status="picked_up", pickup_odometer=12000)
                return 409, {"detail": "Only an active booking can be picked up"}
            return original(booking_id, action, body, info, token)

        self.adm._handover = handover
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(out["output"]["return_odometer"], 12037)

    def test_odometer_going_backwards_rejected_by_adm(self):
        # 車輛目前里程 12000，業務回報取車 11990 → ADM 400 → 交給人
        code, out = self.run_step("trip", "record_trip", trip_input(pickup_odometer=11990))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("里程不能倒退", out["summary"])
        self.assertEqual(out["output"]["http_status"], 400)
        self.assertEqual(self.booking["status"], "active")
        self.assertEqual(self.reports()[0]["status"], "needs_human")

    def test_return_less_than_pickup_rejected_before_calling_adm(self):
        code, out = self.run_step("trip", "record_trip", trip_input(return_odometer=11999))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("里程不能倒退", out["summary"])
        self.assertEqual(self.adm.calls, [])

    def test_return_rejected_by_adm_when_below_registered_pickup(self):
        # ADM 已登記取車 12010（業務自己在網頁登記）→ 和回報不一致 → 交給人
        self.booking.update(status="picked_up", pickup_odometer=12010)
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("不一致", out["summary"])
        self.assertEqual(self.adm.count("POST", RETURN), 0)

    def test_booking_window_ended_needs_human(self):
        now = datetime.now(timezone.utc)
        self.booking.update(start_at=(now - timedelta(hours=20)).isoformat(),
                            end_at=(now - timedelta(minutes=1)).isoformat())
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIsNone(out["error_kind"])
        self.assertIn("Booking window has ended", out["summary"])
        self.assertIn("預約時段已結束", out["summary"])
        self.assertEqual(self.booking["status"], "active")
        (report,) = self.reports()
        self.assertEqual(report["status"], "needs_human")
        self.assertEqual(report["output"]["http_status"], 409)

    def test_pickup_too_early_needs_human(self):
        now = datetime.now(timezone.utc)
        self.booking.update(start_at=(now + timedelta(hours=2)).isoformat(),
                            end_at=(now + timedelta(hours=10)).isoformat())
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("Pickup is too early", out["summary"])

    def test_booking_not_visible_needs_human(self):
        self.booking["_token"] = "adm_other"
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("找不到預約 #1", out["summary"])

    def test_cancelled_booking_needs_human(self):
        self.booking["status"] = "cancelled"
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("已取消", out["summary"])

    def test_return_5xx_reports_system_failure(self):
        self.adm.faults[("POST", RETURN)] = [503]
        code, out = self.run_step("trip", "record_trip", trip_input())
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))
        self.assertEqual(self.booking["status"], "picked_up")
        # 重試：只補還車
        code, out = self.run_step("trip", "record_trip", trip_input(), attempt=2)
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(self.adm.count("POST", PICKUP), 1)

    def test_bad_input_needs_human(self):
        code, out = self.run_step("trip", "record_trip", trip_input(pickup_odometer="12000"))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(self.adm.calls, [])

    def test_forged_instruction_is_not_recorded(self):
        code, out = self.run_step("trip", "record_trip", trip_input(), dispatch=False)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.reports(), [])

    def test_odometer_differs_from_controller_needs_human(self):
        self.controller.dispatch(FLOW_ID, "record_trip", trip_input())
        code, out = self.run_step("trip", "record_trip", trip_input(return_odometer=13999), dispatch=False)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(out["output"]["mismatched_fields"], ["return_odometer"])
        self.assertEqual(self.adm.calls, [])

    def test_dry_run_no_network(self):
        code, out = self.run_step("trip", "record_trip", trip_input(), "--dry-run")
        self.assertEqual(out["output"]["distance_km"], 37)
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.controller.calls, [])


if __name__ == "__main__":
    unittest.main()
