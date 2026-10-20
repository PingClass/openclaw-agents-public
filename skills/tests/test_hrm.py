"""agent-hrm（hrm.py）的測試：假 HRM（session 登入、員工、部門、Employment）。"""

from __future__ import annotations

import os
import random
import unittest
from collections import Counter
from datetime import date
from unittest import mock

from fakes import FakeHrm
from harness import SkillTestCase, load

MONDAY = "2026-10-12"
MONDAY_EARLY = "2026-10-12T07:50:00+08:00"   # 建議排程：hr-agent 09:00 掃描之前
MONDAY_LATE = "2026-10-12T10:05:00+08:00"    # 掃描之後
NEXT_WEEK = {f"2026-10-{d}" for d in range(19, 24)}
WEEK_AFTER = {f"2026-10-{d}" for d in range(26, 31)}

DEPARTMENTS = [
    {"id": "d-old-hr", "code": "HR", "name": "人資部", "is_active": False},
    {"id": "d-sales", "code": "ORG-SALES", "name": "業務部", "is_active": True},
    {"id": "d-cs", "code": "ORG-CS", "name": "客服部", "is_active": True},
    {"id": "d-it", "code": "ORG-IT", "name": "資訊部", "is_active": True},
    {"id": "d-admin", "code": "ORG-ADMIN", "name": "管理部", "is_active": True},
    {"id": "d-hr", "code": "ORG-HR", "name": "人資部", "is_active": True},
]


class HrmTestBase(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.hrm = FakeHrm({"shion": {"password": "pw", "role": "hr"},
                            "souei": {"password": "pw", "role": "employee"}}, DEPARTMENTS)
        self.env.update({"HRM_API_BASE": self.hrm.url, "HRM_AGENT_PASSWORD": "pw"})

    def tearDown(self):
        self.hrm.stop()
        super().tearDown()

    def decide(self, *extra, agent="shion", now=MONDAY_EARLY, day=MONDAY):
        return self.run_script("hrm", ["decide", "--agent", agent, "--date", day, "--now", now, *extra])

    def hire(self, *extra, now=MONDAY_LATE):
        return self.run_script("hrm", ["hire", "--agent", "shion", "--now", now, *extra])

    def posts(self, path):
        return self.hrm.count("POST", path)


class DecideTests(HrmTestBase):
    def test_hit_creates_new_hire_for_next_week(self):
        self.set_rng(0.10)  # < 20%
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "hired")
        self.assertFalse(out["announce"])
        self.assertEqual(len(self.hrm.employees), 1)
        emp = self.hrm.employees[0]
        self.assertEqual(emp["company"], FakeHrm.COMPANY)
        self.assertEqual(emp["department"], "d-sales")   # 0.10 落在業務部的權重
        self.assertEqual((emp["email"], emp["phone"]), ("", ""))   # 去識別化：不填聯絡資料
        self.assertTrue(emp["employee_number"].startswith("NH-202610"))
        self.assertEqual(emp["status"], "active")
        self.assertEqual(len(self.hrm.employments), 1)
        employment = self.hrm.employments[0]
        self.assertEqual(employment["employee"], emp["id"])
        self.assertTrue(employment["is_primary"])
        self.assertIn(employment["start_date"], NEXT_WEEK)
        self.assertIn(date.fromisoformat(employment["start_date"]).weekday(), range(5))
        self.assertEqual(out["start_date"], employment["start_date"])
        self.assertNotIn("note", out)
        # 寫入一律帶 X-CSRFToken（假 HRM 沒帶會 403）
        state = self.state("agent-hrm", "shion", f"{MONDAY}.json")
        self.assertTrue(state["done"])
        self.assertEqual(state["hire"]["status"], "done")

    def test_after_scan_picks_week_after_next(self):
        """09:00 掃描之後才建立的「下週」新人會被漏掉，所以改成下下週，並提醒。"""
        self.set_rng(0.10)
        code, out = self.decide(now=MONDAY_LATE)
        self.assertEqual(code, 0, out)
        self.assertIn(self.hrm.employments[0]["start_date"], WEEK_AFTER)

    def test_miss_does_nothing_and_rerun_does_not_reroll(self):
        self.set_rng(0.50, 0.01)
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "no_hire")
        self.assertEqual(self.hrm.calls, [])
        code, out = self.decide()
        self.assertEqual(out["outcome"], "already_decided")
        self.assertEqual(self.hrm.employees, [])

    def test_rerun_after_hire_does_not_create_twice(self):
        self.set_rng(0.10)
        self.decide()
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "already_decided")
        self.assertEqual(len(self.hrm.employees), 1)
        self.assertEqual(len(self.hrm.employments), 1)

    def test_not_monday(self):
        code, out = self.decide(day="2026-10-13", now="2026-10-13T07:50:00+08:00")
        self.assertEqual((code, out["outcome"]), (0, "not_monday"))
        self.assertEqual(self.hrm.calls, [])

    def test_not_hr_agent(self):
        code, out = self.decide(agent="souei")
        self.assertEqual((code, out["outcome"]), (0, "not_hr"))

    def test_dry_run_no_network_no_state(self):
        self.set_rng(0.10)
        code, out = self.decide("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_hire")
        self.assertIn(out["plan"]["start_date"], NEXT_WEEK)
        self.assertEqual(self.hrm.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "agent-hrm")))

    def test_resume_after_failure_keeps_same_person(self):
        self.set_rng(0.10)
        self.hrm.faults[("POST", "/api/employments/")] = [500]
        code, out = self.decide()
        self.assertEqual(code, 1, out)
        self.assertEqual(out["outcome"], "create_employment_failed")
        number = self.hrm.employees[0]["employee_number"]
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "hired")
        self.assertEqual(out["employee_number"], number)
        self.assertEqual(len(self.hrm.employees), 1)
        self.assertEqual(len(self.hrm.employments), 1)


class HireTests(HrmTestBase):
    def test_hire_with_department_and_date(self):
        code, out = self.hire("--department", "資訊部", "--start-date", "2026-10-21")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "hired")
        self.assertEqual(self.hrm.employees[0]["department"], "d-it")
        self.assertEqual(self.hrm.employments[0]["start_date"], "2026-10-21")
        # 10:05 之後建立、下週到職：趕不上這週一的掃描，提醒 demo 手動跑 onboarding-list
        self.assertIn("--week-of 2026-10-21", out["note"])

    def test_hire_default_is_next_week_weekday(self):
        code, out = self.hire()
        self.assertEqual(code, 0, out)
        self.assertIn(out["start_date"], NEXT_WEEK)

    def test_same_key_is_idempotent(self):
        code, first = self.hire("--key", "demo-1")
        self.assertEqual(code, 0, first)
        code, again = self.hire("--key", "demo-1")
        self.assertEqual(code, 0, again)
        self.assertEqual(again["outcome"], "already_hired")
        self.assertEqual(again["employee_id"], first["employee_id"])
        self.assertEqual(len(self.hrm.employees), 1)
        # 不同的鍵（或不帶鍵）才會再建一位
        self.hire("--key", "demo-2")
        self.assertEqual(len(self.hrm.employees), 2)

    def test_lost_response_on_create_employee_is_not_duplicated(self):
        self.hrm.faults[("POST", "/api/employees/")] = ["drop_after"]
        code, out = self.hire()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(self.hrm.employees), 1)        # 伺服器其實建好了
        self.assertEqual(self.hrm.employments, [])
        code, out = self.hire()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["resumed"])
        self.assertEqual(len(self.hrm.employees), 1)        # 用員工編號找回來，沒有建第二位
        self.assertEqual(self.posts("/api/employees/"), 1)
        self.assertEqual(len(self.hrm.employments), 1)

    def test_lost_response_found_across_pages_without_search(self):
        """真的 HRM 沒有 search：要翻完同部門的分頁才找得到（假 HRM 每頁 2 筆）。"""
        for i in range(5):
            self.hrm.employees.append({
                "id": f"e-old-{i}", "company": FakeHrm.COMPANY, "department": "d-it",
                "employee_number": f"E-{i:03d}", "first_name": "舊", "last_name": "員",
                "email": "", "phone": "", "status": "active"})
        self.hrm.faults[("POST", "/api/employees/")] = ["drop_after"]
        code, out = self.hire("--department", "資訊部")
        self.assertEqual(code, 1, out)
        code, out = self.hire()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["resumed"])
        self.assertEqual(len(self.hrm.employees), 6)
        self.assertEqual(self.posts("/api/employees/"), 1)
        gets = [call[2] for call in self.hrm.calls if call[0] == "GET" and call[1] == "/api/employees/"]
        self.assertTrue(all("search" not in q for q in gets))
        self.assertTrue(all(q.get("department") == "d-it" for q in gets))

    def test_lost_response_on_employment_is_not_duplicated(self):
        self.hrm.faults[("POST", "/api/employments/")] = ["drop_after"]
        code, out = self.hire()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(self.hrm.employments), 1)
        code, out = self.hire()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.hrm.employments), 1)      # 員工已經有到職日，不再建
        self.assertEqual(self.posts("/api/employments/"), 1)

    def test_employment_api_missing_creates_nothing(self):
        self.hrm.has_employment_api = False
        code, out = self.hire()
        self.assertEqual(code, 1, out)
        self.assertEqual(out["outcome"], "employment_api_missing")
        self.assertTrue(out["announce"])
        self.assertEqual(self.hrm.employees, [])

    def test_failed_precheck_leaves_nothing_to_resume(self):
        self.hrm.has_employment_api = False
        self.hire()
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "agent-hrm", "shion", "hires.json")))
        self.hrm.has_employment_api = True
        code, out = self.hire()
        self.assertEqual(code, 0, out)
        self.assertNotIn("resumed", out)

    def test_keyed_hire_does_not_take_over_unkeyed_pending(self):
        self.hrm.faults[("POST", "/api/employments/")] = [500]
        self.hire()                                        # 沒帶鍵、做到一半
        code, out = self.hire("--key", "demo-1")
        self.assertEqual(code, 0, out)
        self.assertNotIn("resumed", out)
        code, again = self.hire("--key", "demo-1")
        self.assertEqual(again["outcome"], "already_hired")
        self.assertEqual(again["employee_id"], out["employee_id"])
        code, rest = self.hire()                           # 不帶鍵：接著做原本那位
        self.assertTrue(rest["resumed"])
        self.assertEqual(len(self.hrm.employees), 2)
        self.assertEqual(len(self.hrm.employments), 2)

    def test_role_not_hr(self):
        self.hrm.users["shion"]["role"] = "employee"
        code, out = self.hire()
        self.assertEqual(code, 1, out)
        self.assertEqual(out["outcome"], "not_hr_role")
        self.assertTrue(out["announce"])

    def test_missing_password_is_config_error(self):
        code, out = self.run_script("hrm", ["hire", "--agent", "shion"], {"HRM_AGENT_PASSWORD": ""})
        self.assertEqual(code, 1, out)
        self.assertEqual(out["outcome"], "config_error")
        self.assertTrue(out["announce"])

    def test_weekend_start_date_rejected(self):
        code, out = self.hire("--start-date", "2026-10-24")
        self.assertEqual((code, out["outcome"]), (1, "config_error"))

    def test_dry_run(self):
        code, out = self.hire("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_hire")
        self.assertEqual(self.hrm.calls, [])

    def test_status(self):
        self.hire()
        code, out = self.run_script("hrm", ["status", "--agent", "shion", "--now", MONDAY_LATE])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out["hires"]), 1)
        self.assertTrue(out["hrm"]["employment_api"])
        self.assertEqual(out["next_scan"], "2026-10-19")


class PlanTests(HrmTestBase):
    def test_sales_and_cs_are_more_likely(self):
        mod = load("hrm")
        with mock.patch.object(self.c, "RNG", random.Random(7)):
            counts = Counter(mod.plan_hire(self.c.parse_dt(MONDAY_EARLY), catch_scan=True)["department_name"]
                             for _ in range(2000))
        self.assertEqual(set(counts), {"業務部", "客服部", "資訊部", "管理部", "人資部"})
        for big in ("業務部", "客服部"):
            for small in ("資訊部", "管理部", "人資部"):
                self.assertGreater(counts[big], counts[small])

    def test_next_scan_monday(self):
        mod = load("hrm")
        p = self.c.parse_dt
        self.assertEqual(mod.next_scan_monday(p("2026-10-12T08:59:00+08:00")), date(2026, 10, 12))
        self.assertEqual(mod.next_scan_monday(p("2026-10-12T09:00:00+08:00")), date(2026, 10, 19))
        self.assertEqual(mod.next_scan_monday(p("2026-10-18T23:00:00+08:00")), date(2026, 10, 19))


if __name__ == "__main__":
    unittest.main()
