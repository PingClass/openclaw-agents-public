"""adm-vehicle-draft 的測試：假 ADM＋假 controller＋flow-report 替身。

執行：python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-draft/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "_testkit"))

from harness import FLOW_ID, SALES_HRM, CommonCopiesTest, SkillTestCase, visit_day  # noqa: E402,F401

AVAIL = "/api/v1/vehicles/availability/"


def draft_input(**overrides) -> dict:
    start = visit_day()
    data = {
        "visit_start_at": start.isoformat(),
        "visit_end_at": (start + timedelta(hours=2)).isoformat(),
        "booking_start_at": start.isoformat(),
        "booking_end_at": start.replace(hour=23, minute=59).isoformat(),
        "seats": 3,
        "destination": "台中 客戶 C 工廠",
        "purpose": "客戶拜訪：客戶 C",
        "customer": {"name": "客戶 C", "external_id": "C-003"},
        "sales": {"hrm_employee_id": SALES_HRM, "display_name": "Mjolmire"},
        "exclude_vehicle_ids": [],
        "sales_comment": None,
    }
    data.update(overrides)
    return data


class DraftTest(SkillTestCase):
    def test_picks_smallest_vehicle_that_fits_and_reports(self):
        data = draft_input()
        code, out = self.run_step("draft", "draft_vehicle", data)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"] and out["reported"])
        self.assertEqual(out["step_status"], "ok")
        output = out["output"]
        self.assertEqual((output["vehicle_id"], output["vehicle_code"]), (1, "CAR-01"))
        self.assertEqual(output["start_at"], data["booking_start_at"])
        self.assertEqual(output["end_at"], data["booking_end_at"])
        self.assertEqual(output["destination"], data["destination"])
        self.assertEqual(output["purpose"], data["purpose"])
        # 查空車用預約時段（拜訪開始 → 當天 23:59），不是拜訪時段
        query = self.adm.calls[0][2]
        self.assertEqual(query["start_at"], data["booking_start_at"])
        self.assertEqual(query["end_at"], data["booking_end_at"])
        self.assertEqual(query["seats"], "3")
        # 回報到 controller：只有一筆，status=ok、帶齊必填欄位（假 controller 會檢查）
        (report,) = self.reports()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["output"]["vehicle_id"], 1)
        argv = self.report_argvs()[0]
        self.assertEqual(argv[argv.index("--flow") + 1], FLOW_ID)
        self.assertEqual(argv[argv.index("--step") + 1], "draft_vehicle")
        self.assertEqual(argv[argv.index("--agent") + 1], "admin-agent")
        self.assertNotIn("--error-kind", argv)
        # 只查，不預約
        self.assertEqual(self.adm.count("POST", "/api/v1/vehicle-bookings/"), 0)

    def test_excludes_conflicting_vehicle(self):
        code, out = self.run_step("draft", "draft_vehicle", draft_input(exclude_vehicle_ids=[1]))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["output"]["vehicle_code"], "CAR-02")

    def test_skips_vehicle_already_booked(self):
        start = visit_day()
        self.adm.add_booking(vehicle=1, start_at=(start + timedelta(hours=3)).isoformat(),
                             end_at=(start + timedelta(hours=5)).isoformat())
        code, out = self.run_step("draft", "draft_vehicle", draft_input())
        self.assertEqual(out["output"]["vehicle_code"], "CAR-02")

    def test_prefer_and_min_seats_from_sales_comment(self):
        code, out = self.run_step("draft", "draft_vehicle", draft_input(sales_comment="要大一點的車"),
                                  "--min-seats", "6")
        self.assertEqual(out["output"]["vehicle_code"], "CAR-02")
        self.assertEqual(self.adm.calls[0][2]["seats"], "6")
        code, out = self.run_step("draft", "draft_vehicle", draft_input(seats=1), "--prefer", "CAR-02")
        self.assertEqual(out["output"]["vehicle_code"], "CAR-02")

    def test_no_vehicle_needs_human(self):
        code, out = self.run_step("draft", "draft_vehicle", draft_input(seats=8))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("沒有 8 人座以上的空車", out["summary"])
        (report,) = self.reports()
        self.assertEqual(report["status"], "needs_human")
        self.assertNotIn("error_kind", report)

    def test_adm_5xx_reports_system_failure_after_retries(self):
        self.adm.faults[("GET", AVAIL)] = [503, 503, 503, 503]
        code, out = self.run_step("draft", "draft_vehicle", draft_input())
        self.assertEqual(self.adm.count("GET", AVAIL), 4)
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))
        (report,) = self.reports()
        self.assertEqual(report["error_kind"], "system")

    def test_adm_retry_then_success(self):
        self.adm.faults[("GET", AVAIL)] = [502, "drop"]
        code, out = self.run_step("draft", "draft_vehicle", draft_input())
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(self.adm.count("GET", AVAIL), 3)

    def test_missing_token_needs_human(self):
        code, out = self.run_step("draft", "draft_vehicle", draft_input(), drop_env=("ADM_API_TOKEN",))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("ADM_API_TOKEN", out["summary"])
        self.assertEqual(self.adm.calls, [])

    def test_dry_run_no_network(self):
        code, out = self.run_step("draft", "draft_vehicle", draft_input(), "--dry-run",
                                  drop_env=("ADM_API_TOKEN",))
        self.assertEqual(code, 0, out)
        self.assertFalse(out["reported"])
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.controller.calls, [])

    def test_without_report_flag_only_prints(self):
        path = self.write("i.txt", __import__("harness").instruction("draft_vehicle", draft_input()))
        code, out = self.run_script("draft", ["--instruction-file", path])
        self.assertEqual(out["step_status"], "ok")
        self.assertFalse(out["reported"])
        self.assertEqual(self.controller.calls, [])

    def test_wrong_step_is_rejected(self):
        code, out = self.run_step("draft", "book_vehicle", draft_input())
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("draft_vehicle", out["error"])
        self.assertEqual(self.controller.calls, [])

    def test_message_without_flow_id_is_not_executed(self):
        path = self.write("chat.txt", "幫我查明天的空車\n")
        code, out = self.run_script("draft", ["--instruction-file", path, "--report"])
        self.assertEqual(code, 1)
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.controller.calls, [])

    def test_report_failure_exit_1(self):
        code, out = self.run_step("draft", "draft_vehicle", draft_input(),
                                  extra_env={"FLOW_CONTROLLER_TOKEN_ADMIN_AGENT": "wrong"})
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("flow-report", out["report_error"])

    def test_separate_args_instead_of_instruction(self):
        import json

        code, out = self.run_script("draft", ["--flow", FLOW_ID, "--attempt", "2",
                                              "--input-json", json.dumps(draft_input()), "--report"])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["attempt"], 2)
        self.assertEqual(self.controller.results[0][1], "draft_vehicle")


if __name__ == "__main__":
    unittest.main()
