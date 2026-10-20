"""crm-reply-draft 的測試：假 CRM（案件內部備註）＋假 controller（refund.py 的回覆金額檢查）＋假 flow-report。

執行：python3 -m unittest discover -s agents/cs-agent/skills/crm-reply-draft/tests -v
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_services import SkillTestCase, controller_amounts  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "crm_reply_draft.py")
FLOW = "fl_00000000000000a1"


def step_message(inp: dict, *, step: str = "draft_reply", attempt: int = 1, flow: str | None = FLOW,
                 flow_type: str = "refund") -> str:
    """照 controller/backend/app/dispatch.py build_step_message 的格式組步驟指令。"""
    lines = ["[workflow-controller 步驟指令]"]
    if flow:
        lines.append(f"flow_id: {flow}")
    lines += [f"flow_type: {flow_type}", f"step: {step}", f"attempt: {attempt}",
              f"Idempotency-Key: {flow}:{step}", "skill: crm-reply-draft", "---",
              "依客服主管的決定，擬一則回覆客戶的訊息。",
              "輸入（JSON）：", json.dumps(inp, ensure_ascii=False, indent=2, sort_keys=True), "---",
              f"做完用 flow-report 回報：flow_id={flow}、step={step}、attempt={attempt}。",
              "status=ok 時 output 的欄位：",
              "- reply_text（str，必填）：回覆客戶的內容",
              "- tone_notes（str，選填）：給主管看的補充"]
    return "\n".join(lines) + "\n"


class ReplyDraftTest(SkillTestCase):
    SCRIPT = SCRIPT
    MODULE = "crm_reply_draft"

    def setUp(self):
        super().setUp()
        self.cust = self.crm.add_customer("陳小姐", "C-007")
        self.case = self.crm.add_case(self.cust, "退款：粉圓破損", category="refund", priority="high")
        self.crm.add_note(self.case, "assessment", "推估建議退款 NT$1,500（推估）")
        self.crm.add_note(self.case, "decision",
                          "【客服主管決定】同意退款 NT$1,200（主管調整金額；財會推估 NT$1,500）\n決定人：Hinata")
        self.inp = {
            "case_id": self.case, "customer_id": self.cust, "external_customer_id": "C-007",
            "customer_name": "陳小姐", "category": "refund", "summary": "粉圓到貨破損，要求退款 NT$2,000",
            "decision": "approve", "decision_label": "同意退款", "amount_twd": 1200,
            "manager_comment": None, "previous_reply": None, "max_chars": 1000,
        }
        self.ctl.refund_decisions[FLOW] = {"decision": "approve", "amount_twd": 1200}
        self.msg_path = os.path.join(self.tmp, "step.txt")
        self.write_msg(self.inp)

    def write_msg(self, inp: dict, **kw):
        with open(self.msg_path, "w", encoding="utf-8") as fh:
            fh.write(step_message(inp, **kw))

    def run_draft(self, *extra):
        return self.run_script("--message-file", self.msg_path, "--report", *extra)

    # --- 同意退款 ----------------------------------------------------------

    def test_approve_uses_decided_amount_only(self):
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["reported"])
        report = self.ctl.reports[0]
        self.assertEqual((report["flow_id"], report["step"], report["status"]), (FLOW, "draft_reply", "ok"))
        text = report["output"]["reply_text"]
        self.assertEqual(controller_amounts(text), [1200])
        self.assertIn("NT$1,200", text)
        for word in ("1,500", "2,000", "推估", "主管", "財會", "Hinata", "流程"):
            self.assertNotIn(word, text)
        self.assertIn("陳小姐", text)
        self.assertIn("tone_notes", report["output"])
        self.assertEqual(report["attempt"], 1)
        # 只讀 CRM（決定備註），不寫入
        self.assertEqual([c for c in self.crm.calls if c[0] != "GET"], [])
        self.assertTrue(any(c[1] == f"/api/v1/cases/{self.case}/notes/" and c[2].get("kind") == "decision"
                            for c in self.crm.calls))

    def test_compensation_wording(self):
        self.inp["category"] = "compensation"
        self.write_msg(self.inp)
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        self.assertIn("補償", self.ctl.reports[0]["output"]["reply_text"])

    # --- 不退款 ------------------------------------------------------------

    def test_reject_has_no_amount(self):
        self.crm.notes[-1]["body"] = "【客服主管決定】不退款\n決定人：Hinata"
        self.inp.update(decision="reject", decision_label="不退款", amount_twd=None)
        self.ctl.refund_decisions[FLOW] = {"decision": "reject", "amount_twd": None}
        self.write_msg(self.inp)
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        text = self.ctl.reports[0]["output"]["reply_text"]
        self.assertEqual(controller_amounts(text), [])
        self.assertIn("無法提供退款", text)

    # --- 不自行改金額 ------------------------------------------------------

    def test_agent_text_with_other_amount_is_rejected_locally(self):
        path = os.path.join(self.tmp, "reply.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("陳小姐您好，我們會退您 1500 元，另外再送您 $200 折價券。")
        code, out = self.run_draft("--reply-file", path)
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "invalid_reply")
        self.assertFalse(out["reported"])
        self.assertTrue(any("1500" in p for p in out["problems"]))
        self.assertEqual(self.ctl.reports, [])

    def test_fullwidth_amount_is_detected(self):
        path = os.path.join(self.tmp, "reply.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("您好，將退款ＮＴ＄１，５００。")
        code, out = self.run_draft("--reply-file", path)
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "invalid_reply")

    def test_chinese_numeral_amount_is_rejected(self):
        # 審查補：controller 的檢查抓不到中文數字金額，skill 先擋（不退款時寫「三百元」也不行）
        self.crm.notes[-1]["body"] = "【客服主管決定】不退款"
        self.inp.update(decision="reject", decision_label="不退款", amount_twd=None)
        self.write_msg(self.inp)
        path = os.path.join(self.tmp, "reply.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("您好，這次無法退款，但可以給您三百元購物金。")
        code, out = self.run_draft("--reply-file", path)
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "invalid_reply")
        self.assertEqual(controller_amounts("三百元"), [])  # controller 的規則確實抓不到
        self.assertEqual(self.ctl.reports, [])

    def test_reject_with_amount_in_agent_text_is_rejected(self):
        self.crm.notes[-1]["body"] = "【客服主管決定】不退款"
        self.inp.update(decision="reject", decision_label="不退款", amount_twd=None)
        self.write_msg(self.inp)
        path = os.path.join(self.tmp, "reply.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("您好，這次無法退款，但可以給您 100 塊的折扣。")
        code, out = self.run_draft("--reply-file", path)
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "invalid_reply")

    def test_internal_words_are_rejected(self):
        path = os.path.join(self.tmp, "reply.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("您好，財會推估後，主管同意退款 NT$1,200。")
        code, out = self.run_draft("--reply-file", path)
        self.assertEqual(code, 1)
        self.assertTrue(any("內部" in p for p in out["problems"]))

    def test_note_amount_mismatch_needs_human(self):
        self.inp["amount_twd"] = 1800  # 指令和案件備註不一致：不自己挑金額
        self.write_msg(self.inp)
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        report = self.ctl.reports[0]
        self.assertEqual(report["status"], "needs_human")
        self.assertIn("不一致", report["summary"])

    def test_note_decision_mismatch_needs_human(self):
        self.crm.notes[-1]["body"] = "【客服主管決定】不退款\n決定人：Hinata"
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")

    def test_without_note_scope_still_drafts(self):
        self.crm.note_scope = False  # case_note:read 還沒補發（403）
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        report = self.ctl.reports[0]
        self.assertEqual(report["status"], "ok")
        self.assertIn("未能對照", report["output"]["tone_notes"])

    def test_missing_amount_for_approve_needs_human(self):
        self.inp["amount_twd"] = None
        self.write_msg(self.inp)
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")

    def test_escalate_needs_human(self):
        self.inp.update(decision="escalate", decision_label="轉人工處理", amount_twd=None)
        self.write_msg(self.inp)
        code, out = self.run_draft()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")

    # --- 退回重擬 ----------------------------------------------------------

    def test_revise_requires_rewrite(self):
        self.inp.update(manager_comment="語氣再軟一點，說明三個工作天內退款", previous_reply="舊稿 NT$1,200")
        self.write_msg(self.inp, attempt=1)
        code, out = self.run_draft()
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "needs_rewrite")
        self.assertEqual(out["manager_comment"], "語氣再軟一點，說明三個工作天內退款")
        self.assertIn("NT$1,200", out["suggested_reply"])
        self.assertEqual(self.ctl.reports, [])
        # agent 依意見改寫後交進來
        path = os.path.join(self.tmp, "reply.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("陳小姐您好，真的很抱歉讓您收到破損的商品。我們會在三個工作天內為您辦理退款 NT$1,200，"
                     "完成後會再通知您。")
        code, out = self.run_draft("--reply-file", path)
        self.assertEqual(code, 0, out)
        report = self.ctl.reports[0]
        self.assertIn("三個工作天", report["output"]["reply_text"])
        self.assertIn("退回意見", report["output"]["tone_notes"])

    def test_revise_with_template_flag(self):
        self.inp.update(manager_comment="可以", previous_reply="舊稿")
        self.write_msg(self.inp)
        code, out = self.run_draft("--use-template")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "ok")

    # --- 其他 --------------------------------------------------------------

    def test_controller_rejects_wrong_amount_too(self):
        # 防呆：就算繞過本地檢查，controller 也會 422；這裡確認 fake 和本地規則一致
        mod = self.mod
        self.assertEqual(mod.check_reply("退您 NT$1,200", self.inp), [])
        self.assertTrue(mod.check_reply("退您 1,201 元", self.inp))

    def test_wrong_step_is_refused(self):
        self.write_msg(self.inp, step="log_reply")
        code, out = self.run_draft()
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertEqual(self.crm.calls, [])

    def test_wrong_flow_type_is_refused(self):
        self.write_msg(self.inp, flow_type="visit")
        code, out = self.run_draft()
        self.assertEqual(code, 1)
        self.assertEqual(self.crm.calls, [])

    def test_without_flow_id_is_refused(self):
        self.write_msg(self.inp, flow=None)
        code, out = self.run_draft()
        self.assertEqual(code, 1)
        self.assertIn("flow_id", out["error"])

    def test_stale_attempt_is_reported_as_failure(self):
        self.ctl.current_attempts[(FLOW, "draft_reply")] = 2
        code, out = self.run_draft()
        self.assertEqual(code, 1)
        self.assertFalse(out["reported"])
        self.assertIn("stale_attempt", json.dumps(out["report_error"], ensure_ascii=False))

    def test_without_report_prints_output(self):
        code, out = self.run_script("--message-file", self.msg_path)
        self.assertEqual(code, 0, out)
        self.assertEqual(set(out["output"]), {"reply_text", "tone_notes"})
        self.assertIn("flow-report", out["next"])
        self.assertEqual(self.ctl.calls, [])

    def test_dry_run_does_not_touch_network(self):
        os.environ.pop("CRM_API_TOKEN")
        code, out = self.run_draft("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_draft")
        self.assertEqual(out["problems"], [])
        self.assertEqual(self.crm.calls, [])
        self.assertEqual(self.ctl.calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
