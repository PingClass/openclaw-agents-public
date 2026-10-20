"""laptop-allocation-draft 的測試：沿用 admin-agent 的 _testkit（假 ADM＋假 controller＋flow-report 替身）。

執行：python3 -m unittest discover -s agents/it-agent/skills/laptop-allocation-draft/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
sys.path.insert(0, os.path.join(AGENTS_DIR, "admin-agent", "skills", "_testkit"))

from harness import ADM_TOKEN_IT, SkillTestCase  # noqa: E402

ASSETS = "/api/v1/assets/"
NEW_HIRE = "0b9c1d2e-3f40-4a5b-8c6d-7e8f9a0b1c2d"


def laptop_input(department="業務部", job_title="業務專員") -> dict:
    """照 controller onboarding.py 的 _laptop_inputs 組出 draft_laptop 的輸入。"""
    return {
        "new_hire": {"hrm_employee_id": NEW_HIRE, "name": "王小明", "employee_number": "E2026101",
                     "department_id": 1, "department_name": department, "job_title": job_title,
                     "start_date": "2026-10-19"},
        "category": "laptop",
        "assets_query": "GET /api/v1/assets/?category=laptop&unassigned=true&is_active=true",
    }


class LaptopDraftTest(SkillTestCase):
    def draft(self, data=None, *extra, **kwargs):
        kwargs.setdefault("drop_env", ("OPENCLAW_AGENT_ID",))  # 不設 agent id 時預設 it-agent
        return self.run_step("laptop", "draft_laptop", data or laptop_input(), *extra,
                             agent="it-agent", flow_type="onboarding", **kwargs)

    def test_sales_hire_gets_sales_laptop(self):
        code, out = self.draft()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "ok", out)
        output = out["output"]
        self.assertEqual(output["asset_code"], "NB-004")
        self.assertEqual(output["asset_name"], "15 吋業務筆電")
        self.assertEqual(output["alternatives"][0], "NB-005")
        self.assertEqual(output["available_count"], 6)
        self.assertIn("業務", output["reason"])
        self.assertIn("NB-004", out["summary"])
        # 查詢條件照契約，用 it-agent 的 assets:read token
        (call,) = self.adm.calls
        method, path, query, _body, headers = call
        self.assertEqual((method, path), ("GET", ASSETS))
        self.assertEqual(query, {"category": "laptop", "unassigned": "true", "is_active": "true"})
        self.assertEqual(headers["Authorization"], f"Bearer {ADM_TOKEN_IT}")
        # 用 it-agent 的身分回報
        (_flow, step, agent, report) = self.controller.results[0]
        self.assertEqual((step, agent, report["status"]), ("draft_laptop", "it-agent", "ok"))

    def test_it_hire_gets_dev_laptop(self):
        _, out = self.draft(laptop_input("資訊部", "系統工程師"))
        self.assertEqual(out["output"]["asset_code"], "NB-006")

    def test_other_hire_gets_office_laptop(self):
        _, out = self.draft(laptop_input("管理部", "行政助理"))
        self.assertEqual(out["output"]["asset_code"], "NB-001")
        self.assertEqual(out["output"]["alternatives"][:2], ["NB-002", "NB-003"])

    def test_english_keywords_match_whole_words_only(self):
        # Recruiter 含 "it"、Standard 含 "rd"：不能被當成資訊／研發職務
        _, out = self.draft(laptop_input("人資部", "Recruiter"))
        self.assertEqual(out["output"]["asset_code"], "NB-001")
        _, out = self.draft(laptop_input("管理部", "Standard 行政"))
        self.assertEqual(out["output"]["asset_code"], "NB-001")
        _, out = self.draft(laptop_input("資訊室", "IT 支援"))
        self.assertEqual(out["output"]["asset_code"], "NB-006")

    def test_assigned_and_inactive_laptops_are_skipped(self):
        for asset in self.adm.assets:
            if asset["code"] in ("NB-004", "NB-005"):
                asset["custodian"] = 9
                asset["custodian_hrm_employee_id"] = "someone-else"
        self.adm.assets[5]["is_active"] = False
        _, out = self.draft()
        # 沒有業務筆電可用，退而建議文書筆電，並說明原因
        self.assertEqual(out["output"]["asset_code"], "NB-001")
        self.assertEqual(out["output"]["available_count"], 3)
        self.assertIn("目前沒有這類筆電", out["output"]["reason"])
        self.assertNotIn("NB-004", out["output"]["alternatives"])
        self.assertNotIn("NB-006", out["output"]["alternatives"])

    def test_no_laptop_needs_human(self):
        for asset in self.adm.assets:
            asset["custodian"] = 9
        code, out = self.draft()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(out["output"], {"available_count": 0})
        self.assertIn("沒有未指派", out["summary"])
        (report,) = self.reports()
        self.assertEqual(report["status"], "needs_human")

    def test_never_calls_assign_api(self):
        """agent 不得指派：腳本只送 GET，沒有任何 POST 到 ADM。"""
        self.draft()
        self.draft(laptop_input("資訊部", "工程師"), attempt=2)
        self.assertTrue(self.adm.calls)
        self.assertEqual({c[0] for c in self.adm.calls}, {"GET"})
        self.assertFalse(any("assign" in c[1] for c in self.adm.calls))
        self.assertTrue(all(a["custodian"] is None for a in self.adm.assets))
        with open(os.path.join(os.path.dirname(HERE), "scripts", "laptop_draft.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('"POST"', source)
        self.assertNotIn("/assign/", source.replace("（A4）", ""))

    def test_fake_adm_rejects_token_on_assign(self):
        """對照：A4 對 agent token 一律 403（假 ADM 照真實規則）。"""
        status, _ = self.adm.route("POST", "/api/v1/assets/NB-001/assign/", {}, {"custodian_hrm_employee_id": NEW_HIRE},
                                   {"Authorization": f"Bearer {ADM_TOKEN_IT}"})
        self.assertEqual(status, 403)

    def test_missing_token_needs_human(self):
        code, out = self.draft(drop_env=("OPENCLAW_AGENT_ID", "ADM_API_TOKEN_IT"))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("ADM_API_TOKEN_IT", out["summary"])

    def test_scope_missing_needs_human(self):
        self.adm.tokens[ADM_TOKEN_IT]["scopes"] = set()
        _, out = self.draft()
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("403", out["summary"])

    def test_5xx_reports_system_failure(self):
        self.adm.faults[("GET", ASSETS)] = [503, 503, 503, 503]
        _, out = self.draft()
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))
        self.assertEqual(self.adm.count("GET", ASSETS), 4)

    def test_missing_new_hire_needs_human(self):
        _, out = self.draft({"category": "laptop"})
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(self.adm.calls, [])

    def test_dry_run_no_network(self):
        code, out = self.draft(None, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("category=laptop", out["output"]["would_query"])
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.controller.calls, [])


if __name__ == "__main__":
    unittest.main()
