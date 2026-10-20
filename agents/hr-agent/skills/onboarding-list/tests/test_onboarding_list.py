"""onboarding-list 的測試：假 HRM＋假 controller＋repo 裡的 flow-start。

執行：python3 -m unittest discover -s agents/hr-agent/skills/onboarding-list/tests -v
"""

from __future__ import annotations

import filecmp
import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_services import REPO, SkillTestCase  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
SKILLS_DIR = os.path.dirname(SKILL_DIR)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "onboarding_list.py")

MONDAY_9AM = "2026-10-12T09:00:00+08:00"
SUNDAY = "2026-10-18T20:00:00+08:00"
SALES = "d-sales"
IT = "d-it"


class OnboardingListTest(SkillTestCase):
    SCRIPT = SCRIPT
    MODULE = "onboarding_list"

    def setUp(self):
        super().setUp()
        self.hrm.add_department(SALES, "業務部")
        self.hrm.add_department(IT, "資訊部")

    def flows_posted(self):
        return self.ctl.count("POST", "/api/flows")

    # --- 期間 --------------------------------------------------------------

    def test_next_week_range(self):
        nw = self.mod.next_week
        self.assertEqual(nw(date(2026, 10, 12)), (date(2026, 10, 19), date(2026, 10, 25)))  # 週一
        self.assertEqual(nw(date(2026, 10, 14)), (date(2026, 10, 19), date(2026, 10, 25)))  # 週三
        self.assertEqual(nw(date(2026, 10, 18)), (date(2026, 10, 19), date(2026, 10, 25)))  # 週日
        self.assertEqual(self.mod.week_of(date(2026, 10, 21)), (date(2026, 10, 19), date(2026, 10, 25)))

    def test_monday_scan_starts_flows_for_next_week(self):
        self.hrm.add_employee("e-mon", "王小明", "NH-1", SALES, "2026-10-19")   # 下週一：含邊界
        self.hrm.add_employee("e-sun", "李小華", "NH-2", IT, "2026-10-25")      # 下週日：含邊界
        self.hrm.add_employee("e-this", "陳一", "NH-3", SALES, "2026-10-16")    # 這週：不算
        self.hrm.add_employee("e-after", "陳二", "NH-4", SALES, "2026-10-26")   # 下下週：不算
        self.hrm.add_employee("e-gone", "陳三", "NH-5", SALES, "2026-10-20", status="terminated")
        self.hrm.add_employee("e-mid", "林三", "NH-6", SALES, "2026-10-21")     # 第 3 筆：分頁
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "flows_started")
        self.assertFalse(out["announce"])
        self.assertEqual(out["window"], {"start_date_after": "2026-10-19", "start_date_before": "2026-10-25"})
        self.assertEqual(sorted(s["employee_id"] for s in out["started"]), ["e-mid", "e-mon", "e-sun"])
        # 查 HRM 用的參數
        query = [c for c in self.hrm.calls if c[1] == "/api/employees/"][0][2]
        self.assertEqual(query, {"start_date_after": "2026-10-19", "start_date_before": "2026-10-25",
                                 "status": "active"})
        # flow-start 的 input 照契約，只有流程需要的欄位（沒有電話）
        flow = next(f for f in self.ctl.flows.values() if f["input"]["employee_id"] == "e-mon")
        self.assertEqual(flow["type"], "onboarding")
        self.assertEqual(flow["input"], {
            "employee_id": "e-mon", "name": "王小明", "employee_number": "NH-1",
            "department_id": SALES, "department_name": "業務部", "start_date": "2026-10-19",
        })
        self.assertEqual(flow["client_dedupe_key"], "hrm:employee:e-mon")

    def test_sunday_run_still_scans_coming_week(self):
        self.hrm.add_employee("e1", "王小明", "NH-1", SALES, "2026-10-19")
        code, out = self.run_script("--now", SUNDAY)
        self.assertEqual(code, 0, out)
        self.assertEqual([s["employee_id"] for s in out["started"]], ["e1"])

    def test_week_of_override(self):
        self.hrm.add_employee("e1", "王小明", "NH-1", SALES, "2026-10-16")
        code, out = self.run_script("--now", MONDAY_9AM, "--week-of", "2026-10-14")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["window"]["start_date_after"], "2026-10-12")
        self.assertEqual([s["employee_id"] for s in out["started"]], ["e1"])

    # --- 安靜結束、重跑 ----------------------------------------------------

    def test_no_new_hires_is_silent(self):
        self.hrm.add_employee("e1", "王小明", "NH-1", SALES, "2026-10-30")
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "no_new_hires")
        self.assertFalse(out["announce"])
        self.assertEqual(self.flows_posted(), 0)

    def test_rerun_does_not_start_twice(self):
        self.hrm.add_employee("e1", "王小明", "NH-1", SALES, "2026-10-20")
        self.run_script("--now", MONDAY_9AM)
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "already_started")
        self.assertFalse(out["announce"])
        self.assertEqual(len(out["existing"]), 1)
        self.assertEqual(len(self.ctl.flows), 1)

    # --- 要人處理 ----------------------------------------------------------

    def test_employee_without_department_is_skipped_and_announced(self):
        self.hrm.add_employee("e1", "王小明", "NH-1", SALES, "2026-10-20")
        self.hrm.add_employee("e2", "李小華", "NH-2", None, "2026-10-21")
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "needs_attention")
        self.assertTrue(out["announce"])
        self.assertEqual([s["employee_id"] for s in out["skipped"]], ["e2"])
        self.assertEqual([s["employee_id"] for s in out["started"]], ["e1"])

    def test_flow_start_rejected(self):
        self.hrm.add_employee("e1", "王小明", "NH-1", SALES, "2026-10-20")
        os.environ["FLOW_CONTROLLER_TOKEN_HR_AGENT"] = "wrong"
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual(code, 1, out)
        self.assertEqual(out["outcome"], "needs_attention")
        self.assertTrue(out["announce"])
        self.assertEqual(len(out["failed"]), 1)

    def test_hrm_retry_then_success(self):
        self.hrm.add_employee("e1", "王小明", "NH-1", SALES, "2026-10-20")
        self.hrm.faults[("GET", "/api/employees/")] = [503, "drop"]
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out["started"]), 1)

    def test_hrm_token_rejected_announces(self):
        os.environ["HRM_API_TOKEN"] = "bad"
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual(code, 1, out)
        self.assertEqual(out["outcome"], "hrm_401")
        self.assertTrue(out["announce"])

    def test_missing_token_is_config_error(self):
        os.environ["HRM_API_TOKEN"] = ""
        code, out = self.run_script("--now", MONDAY_9AM)
        self.assertEqual((code, out["outcome"]), (1, "config_error"))
        self.assertTrue(out["announce"])

    # --- dry-run -----------------------------------------------------------

    def test_dry_run_no_network(self):
        os.environ["HRM_API_TOKEN"] = ""
        code, out = self.run_script("--now", MONDAY_9AM, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_scan")
        self.assertIn("start_date_after=2026-10-19", out["query"])
        self.assertEqual(self.hrm.calls, [])
        self.assertEqual(self.ctl.calls, [])

    # --- 副本一致 ----------------------------------------------------------

    def test_shared_copies_are_identical(self):
        notice = os.path.join(SKILLS_DIR, "onboarding-notice")
        for rel in ("scripts/hr_common.py", "scripts/ffj_common.py", "tests/fake_services.py"):
            self.assertTrue(filecmp.cmp(os.path.join(SKILL_DIR, rel), os.path.join(notice, rel), shallow=False),
                            f"{rel} 在兩個 skill 不一致")
        upstream = os.path.join(REPO, "skills", "agent-crm", "scripts", "ffj_common.py")
        self.assertTrue(filecmp.cmp(os.path.join(SKILL_DIR, "scripts", "ffj_common.py"), upstream,
                                    shallow=False), "ffj_common.py 和 skills/agent-crm 的版本不一致")


if __name__ == "__main__":
    unittest.main()
