"""refund-decision-note 的測試：假 CRM＋假 controller＋真的 flow-report skill。

執行：python3 -m unittest discover -s agents/finance-agent/skills/refund-decision-note/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "_testkit"))

from harness import CRM_TOKEN, FLOW_ID, CommonCopiesTest, SkillTestCase  # noqa: E402,F401

KEY = f"{FLOW_ID}:record_decision"
NOTE_BODY = ("【客服主管決定】同意退款 NT$1,200（主管調整金額；財會推估 NT$1,500）\n"
             "決定人：Hinata，10/12 10:00\n意見：部分退款即可\n評估備註：n-1")


class DecisionTest(SkillTestCase):
    STEP = "record_decision"
    SKILL = "refund-decision-note"
    SCRIPT = "decision"

    def setUp(self):
        super().setUp()
        self.customer = self.crm.add_customer("陳小姐")
        self.case = self.crm.add_case(self.customer, "訂單 FFJ-2610123 退冰", "要求退款")

    def data(self, **overrides) -> dict:
        data = {
            "case_id": self.case["id"],
            "decision": "approve",
            "decision_label": "同意退款",
            "amount_twd": 1200,
            "comment": "部分退款即可",
            "decided_by": "Hinata",
            "decided_at": "2026-10-12T02:00:00+00:00",
            "assessment_note_id": "n-1",
            "note_kind": "decision",
            "note_body": NOTE_BODY,
        }
        data.update(overrides)
        return data

    def notes_path(self) -> str:
        return f"/api/v1/cases/{self.case['id']}/notes/"

    def test_writes_decision_note_verbatim(self):
        code, out = self.run_step(self.data())
        self.assertEqual(code, 0, out)
        self.assertTrue(out["reported"], out)
        self.assertEqual(out["step_status"], "ok")
        (note,) = self.crm.case_notes(self.case["id"])
        self.assertEqual(note["kind"], "decision")
        self.assertEqual(note["body"], NOTE_BODY)  # 一字不改
        self.assertEqual(out["output"], {"note_id": note["id"]})
        post = [c for c in self.crm.calls if c[0] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0][4]["Idempotency-Key"], KEY)
        self.assertEqual(post[0][4]["Authorization"], f"Bearer {CRM_TOKEN}")
        self.assertIn("同意退款 NT$1,200", out["summary"])
        (report,) = self.reports()
        self.assertEqual(report["output"]["note_id"], note["id"])
        self.assertEqual(report["attempt"], 1)

    def test_reject_and_escalate(self):
        body = "【客服主管決定】不退款\n決定人：Hinata，10/12 10:00"
        code, out = self.run_step(self.data(decision="reject", decision_label="不退款", amount_twd=None,
                                            note_body=body))
        self.assertEqual(out["step_status"], "ok")
        self.assertIn("不退款", out["summary"])
        self.assertNotIn("NT$", out["summary"])
        self.assertEqual(self.crm.case_notes(self.case["id"])[0]["body"], body)

    def test_retry_writes_only_one_note(self):
        code, first = self.run_step(self.data())
        code, second = self.run_step(self.data(), attempt=2)
        self.assertEqual(code, 0, second)
        self.assertEqual(len(self.crm.case_notes(self.case["id"])), 1)
        self.assertEqual(second["output"]["note_id"], first["output"]["note_id"])
        self.assertIn("沿用", second["summary"])
        self.assertEqual(self.crm.count("POST", self.notes_path()), 1)

    def test_lost_response_retried_with_same_key(self):
        self.crm.faults[("POST", self.notes_path())] = ["drop_after"]
        code, out = self.run_step(self.data())
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(len(self.crm.case_notes(self.case["id"])), 1)

    def test_human_note_with_same_body_is_not_reused(self):
        # 人在 CRM 介面手寫的同內容備註不算（只沿用 service credential 寫的）
        self.crm.add_note(self.case, "decision", NOTE_BODY, author="hinata")
        code, out = self.run_step(self.data())
        self.assertEqual(out["step_status"], "ok")
        self.assertEqual(len(self.crm.case_notes(self.case["id"])), 2)

    def test_tampered_note_body_needs_human(self):
        code, out = self.run_step(self.data(note_body="【客服主管決定】同意退款 NT$99,999"),
                                  controller_input=self.data())
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(out["output"]["mismatched_fields"], ["note_body"])
        self.assertEqual(self.crm.calls, [])

    def test_wrong_note_kind_needs_human(self):
        code, out = self.run_step(self.data(note_kind="assessment"))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(self.crm.calls, [])

    def test_empty_body_needs_human(self):
        code, out = self.run_step(self.data(note_body="  "))
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("note_body", out["summary"])

    def test_missing_scope_needs_human(self):
        code, out = self.run_step(self.data(), extra_env={"CRM_API_TOKEN_FINANCE": "crm_no_note"})
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("403", out["summary"])

    def test_not_dispatched_does_nothing(self):
        code, out = self.run_step(self.data(), dispatch=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.crm.calls, [])
        self.assertEqual(self.reports(), [])

    def test_dry_run(self):
        code, out = self.run_step(self.data(), "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.crm.calls, [])
        self.assertEqual(self.controller.calls, [])
        self.assertTrue(any(KEY in line for line in out["plan"]))


if __name__ == "__main__":
    unittest.main()
