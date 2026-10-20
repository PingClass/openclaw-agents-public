"""onboarding-notice 的測試：假 HRM＋假 controller＋repo 裡的 flow-report。

執行：python3 -m unittest discover -s agents/hr-agent/skills/onboarding-notice/tests -v
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_services import SkillTestCase  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "onboarding_notice.py")

FLOW = "fl_00000000000000a1"
SALES = "6a3f0b7c-2d4e-4c1a-8b9f-1e2d3c4b5a69"
IT = "7b4f0b7c-2d4e-4c1a-8b9f-1e2d3c4b5a70"
MAIN = {"employee_id": "85f9387a-6a8f-4283-92dc-71ec63de4447", "employee_number": "AGT-MAIN",
        "full_name": "MainBot", "email": "main@agents.invalid"}
OTHER = {"employee_id": "11111111-6a8f-4283-92dc-71ec63de4447", "employee_number": "AGT-X",
         "full_name": "副主管", "email": "x@agents.invalid"}
PHONE_RE = re.compile(r"09\d{2}[-\s]?\d{3}[-\s]?\d{3}")


def step_input(dept=SALES, dept_name="業務部", **over):
    data = {
        "new_hire": {"hrm_employee_id": "e-new", "name": "王小明", "employee_number": "NH-20261019-AB12",
                     "department_id": dept, "department_name": dept_name, "job_title": None,
                     "start_date": "2026-10-19"},
        "department_id": dept,
        "managers_query": f"GET /api/departments/{dept}/managers/",
        "expected_manager": {"hrm_employee_id": MAIN["employee_id"], "display_name": "Main"},
        "hr": {"hrm_employee_id": "3296d2ef-5716-419a-8064-458e54ac7b17", "display_name": "Shion"},
        "laptop": {"asset_code": "LT-003", "asset_name": "ThinkPad T14"},
        "orientation": {"room_name": "會議室 A", "start_at": "2026-10-19T09:30:00+08:00",
                        "end_at": "2026-10-19T10:30:00+08:00", "booking_id": 42},
        "manager_comment": None,
    }
    data.update(over)
    return data


def message(data, step="onboarding_notice", attempt=1, flow=FLOW, flow_type="onboarding"):
    """照 controller/backend/app/dispatch.py build_step_message 的格式。"""
    lines = [
        "[workflow-controller 步驟指令]",
        f"flow_id: {flow}",
        f"flow_type: {flow_type}",
        f"step: {step}",
        f"attempt: {attempt}",
        f"Idempotency-Key: {flow}:{step}",
        "skill: onboarding-notice",
        "---",
        "為新人 王小明（業務部，10/19 到職）產生到職清單，給部門主管確認收到。",
        "輸入（JSON）：",
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        "---",
        f"做完用 flow-report 回報：flow_id={flow}、step={step}、attempt={attempt}、status、summary、output。",
        "status=ok 時 output 的欄位：",
        "- manager_hrm_employee_ids（list，必填）：HRM 回傳的部門主管員工 id",
        "- checklist（str，必填）：到職清單全文",
    ]
    return "\n".join(lines) + "\n"


class OnboardingNoticeTest(SkillTestCase):
    SCRIPT = SCRIPT
    MODULE = "onboarding_notice"

    def setUp(self):
        super().setUp()
        self.hrm.add_department(SALES, "業務部", [MAIN])
        self.hrm.add_department(IT, "資訊部", [])

    def run_step(self, data=None, *extra, **kw):
        return self.run_script("--message-file", "-", "--report", *extra,
                               stdin=message(data or step_input(), **kw))

    # --- 正常 --------------------------------------------------------------

    def test_one_manager_reports_ok(self):
        code, out = self.run_step()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["reported"])
        self.assertEqual(out["outcome"], "notice_ready")
        self.assertTrue(out["matches_expected"])
        self.assertEqual(self.hrm.count("GET", f"/api/departments/{SALES}/managers/"), 1)
        report = self.ctl.reports[0]
        self.assertEqual((report["flow_id"], report["step"], report["status"], report["attempt"]),
                         (FLOW, "onboarding_notice", "ok", 1))
        output = report["output"]
        self.assertEqual(output["manager_hrm_employee_ids"], [MAIN["employee_id"]])
        checklist = output["checklist"]
        for text in ("王小明", "NH-20261019-AB12", "業務部", "2026-10-19（週一）", "LT-003",
                     "會議室 A", "09:30–10:30", "MainBot", "Shion", "主管第一週要做的事"):
            self.assertIn(text, checklist)
        self.assertIn("職稱：未提供", checklist)
        # 不含 email、電話、地址
        self.assertNotIn("@", checklist)
        self.assertNotIn("main@agents.invalid", json.dumps(output, ensure_ascii=False))
        self.assertIsNone(PHONE_RE.search(checklist))
        self.assertTrue(all(isinstance(i, str) for i in output["items"]))

    # --- 沒有主管、不只一位 -------------------------------------------------

    def test_no_manager_reports_empty_list_ok(self):
        data = step_input(dept=IT, dept_name="資訊部", expected_manager={"hrm_employee_id": None})
        code, out = self.run_step(data)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "no_manager")
        self.assertFalse(out["announce"])
        report = self.ctl.reports[0]
        self.assertEqual(report["status"], "ok")                      # 契約：查不到也是 ok
        self.assertEqual(report["output"]["manager_hrm_employee_ids"], [])
        self.assertIn("HRM 查不到資訊部的部門主管", report["output"]["checklist"])
        self.assertIn("查不到", report["summary"])

    def test_multiple_managers_all_reported(self):
        self.hrm.managers[SALES] = [MAIN, OTHER]
        code, out = self.run_step()
        self.assertEqual(code, 0, out)
        ids = self.ctl.reports[0]["output"]["manager_hrm_employee_ids"]
        self.assertEqual(ids, [MAIN["employee_id"], OTHER["employee_id"]])   # 不自己挑一位
        self.assertFalse(out["matches_expected"])

    # --- 個資防線 ----------------------------------------------------------

    def test_phone_and_address_in_free_text_are_removed(self):
        data = step_input()
        data["orientation"]["room_name"] = "會議室 A（分機 0912-345-678）"
        data["new_hire"]["job_title"] = "業務專員 台北市信義區松仁路100號"
        code, out = self.run_step(data)
        self.assertEqual(code, 0, out)
        checklist = self.ctl.reports[0]["output"]["checklist"]
        self.assertIsNone(PHONE_RE.search(checklist))
        self.assertNotIn("松仁路100號", checklist)
        self.assertIn("［已移除］", checklist)

    def test_clean_keeps_dates_and_codes(self):
        for text in ("2026-10-19", "NH-20261019-AB12", "LT-003", "09:30–10:30", "E2026101"):
            self.assertEqual(self.mod.clean(text), text)

    # --- 失敗分類 ----------------------------------------------------------

    def test_unknown_department_needs_human(self):
        code, out = self.run_step(step_input(dept="d-missing"))
        self.assertEqual(code, 0, out)               # 已回報 controller（needs_human），這一步結束
        self.assertTrue(out["reported"])
        self.assertEqual(out["outcome"], "step_needs_human")
        report = self.ctl.reports[0]
        self.assertEqual(report["status"], "needs_human")
        self.assertIn("找不到部門", report["summary"])

    def test_hrm_down_is_system_failure(self):
        self.hrm.faults[("GET", f"/api/departments/{SALES}/managers/")] = [503, 503, 503, 503]
        code, out = self.run_step()
        report = self.ctl.reports[0]
        self.assertEqual((report["status"], report["error_kind"]), ("failed", "system"))
        self.assertEqual(out["report_status"], "failed")

    def test_hrm_retry_then_ok(self):
        self.hrm.faults[("GET", f"/api/departments/{SALES}/managers/")] = [502, "drop"]
        code, out = self.run_step()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "ok")

    def test_bad_token_needs_human(self):
        os.environ["HRM_API_TOKEN"] = "bad"
        self.run_step()
        report = self.ctl.reports[0]
        self.assertEqual(report["status"], "needs_human")
        self.assertIn("401", report["summary"])

    def test_missing_token_needs_human(self):
        os.environ["HRM_API_TOKEN"] = ""
        self.run_step()
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")

    def test_stale_attempt_announces(self):
        self.ctl.current_attempts[(FLOW, "onboarding_notice")] = 2
        code, out = self.run_step(attempt=1)
        self.assertEqual(code, 1, out)
        self.assertFalse(out["reported"])
        self.assertTrue(out["announce"])
        self.assertIn("stale_attempt", json.dumps(out["report_error"], ensure_ascii=False))

    # --- 指令檢查 ----------------------------------------------------------

    def test_wrong_step_is_config_error(self):
        code, out = self.run_step(step="book_room")
        self.assertEqual((code, out["outcome"]), (1, "config_error"))
        self.assertEqual(self.ctl.reports, [])
        self.assertEqual(self.hrm.calls, [])

    def test_missing_flow_id_is_rejected(self):
        code, out = self.run_step(flow="")
        self.assertEqual((code, out["outcome"]), (1, "config_error"))

    def test_without_report_prints_next(self):
        code, out = self.run_script("--message-file", "-", stdin=message(step_input()))
        self.assertEqual(code, 0, out)
        self.assertIn("--attempt 1", out["next"])
        self.assertEqual(self.ctl.reports, [])
        self.assertIn("checklist", out["output"])

    def test_input_file_mode(self):
        path = os.path.join(self.tmp, "in.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(step_input(), fh, ensure_ascii=False)
        code, out = self.run_script("--flow", FLOW, "--attempt", "1", "--input-file", path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["output"]["manager_hrm_employee_ids"], [MAIN["employee_id"]])

    def test_dry_run_no_network(self):
        os.environ["HRM_API_TOKEN"] = ""
        code, out = self.run_step(None, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_notice")
        self.assertEqual(out["requests"], [f"GET /api/departments/{SALES}/managers/"])
        self.assertEqual(self.hrm.calls, [])
        self.assertEqual(self.ctl.calls, [])


if __name__ == "__main__":
    unittest.main()
