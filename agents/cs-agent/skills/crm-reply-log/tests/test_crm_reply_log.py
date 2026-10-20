"""crm-reply-log 的測試：假 CRM＋假 controller＋假 flow-report。

執行：python3 -m unittest discover -s agents/cs-agent/skills/crm-reply-log/tests -v
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_services import SkillTestCase  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "crm_reply_log.py")
FLOW = "fl_00000000000000b2"
KEY_I = f"{FLOW}:log_reply:interaction"
KEY_F = f"{FLOW}:log_reply:follow_up"
REPLY = "陳小姐您好，關於您先前反映的問題，我們已完成確認，將為您辦理退款 NT$1,200。"


def step_message(inp: dict, *, step: str = "log_reply", attempt: int = 1, flow: str | None = FLOW,
                 keys: dict | None = None) -> str:
    keys = {"interaction": KEY_I, "follow_up": KEY_F} if keys is None else keys
    lines = ["[workflow-controller 步驟指令]"]
    if flow:
        lines.append(f"flow_id: {flow}")
    lines += ["flow_type: refund", f"step: {step}", f"attempt: {attempt}", f"Idempotency-Key: {flow}:{step}"]
    lines += [f"Idempotency-Key.{k}: {v}" for k, v in keys.items()]
    lines += ["skill: crm-reply-log", "---",
              "controller 已把客服主管核准的回覆送給客戶「陳小姐」，在 CRM 留下紀錄。",
              "輸入（JSON）：", json.dumps(inp, ensure_ascii=False, indent=2, sort_keys=True), "---",
              f"做完用 flow-report 回報：flow_id={flow}、step={step}、attempt={attempt}。",
              "status=ok 時 output 的欄位：",
              "- interaction_id（str，必填）：CRM 互動紀錄 id（UUID 字串）",
              "- follow_up_id（str，必填）：CRM 後續待辦 id（UUID 字串）"]
    return "\n".join(lines) + "\n"


class ReplyLogTest(SkillTestCase):
    SCRIPT = SCRIPT
    MODULE = "crm_reply_log"

    def setUp(self):
        super().setUp()
        self.cust = self.crm.add_customer("陳小姐", "C-007")
        self.case = self.crm.add_case(self.cust, "退款：粉圓破損", category="refund", priority="high")
        self.crm.add_note(self.case, "assessment", "推估建議退款 NT$1,500（推估）")
        self.inp = {
            "case_id": self.case, "customer_id": self.cust, "external_customer_id": "C-007",
            "customer_name": "陳小姐", "category": "refund", "summary": "粉圓到貨破損，要求退款",
            "reply_text": REPLY, "sent_at": "2026-10-12T03:00:00+00:00", "channel": "sim", "message_id": 12,
            "decision": "approve", "amount_twd": 1200,
            "follow_up": {"kind": "general", "due_at": "2026-10-15T10:00:00+08:00",
                          "title": "追蹤退款案件回覆：陳小姐"},
        }
        self.msg_path = os.path.join(self.tmp, "step.txt")
        self.write_msg(self.inp)

    def write_msg(self, inp: dict, **kw):
        with open(self.msg_path, "w", encoding="utf-8") as fh:
            fh.write(step_message(inp, **kw))

    def posts(self, path):
        return [c for c in self.crm.calls if c[0] == "POST" and c[1] == path]

    def test_logs_interaction_and_follow_up(self):
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["reported"])
        # 互動：回覆內容、案件 id，沒有內部評估
        self.assertEqual(len(self.crm.interactions), 1)
        row = self.crm.interactions[0]
        self.assertEqual(row["customer"], self.cust)
        self.assertEqual(row["external_message_id"], f"{FLOW}:reply")
        lines = row["summary"].splitlines()
        self.assertEqual(lines[0], "[回覆客戶] 2026-10-12 11:00 通路：sim")
        self.assertEqual(lines[1], f"案件：{self.case}（退款）")
        self.assertIn(REPLY, row["summary"])
        self.assertNotIn("1,500", row["summary"])
        self.assertNotIn("推估", row["summary"])
        self.assertEqual(self.posts("/api/v1/interactions/")[0][4]["Idempotency-Key"], KEY_I)
        # 後續待辦：關聯案件，kind／due_at／title 照輸入
        fu_posts = self.posts("/api/v1/follow-ups/")
        self.assertEqual(fu_posts[0][4]["Idempotency-Key"], KEY_F)
        self.assertEqual(fu_posts[0][3], {"customer": self.cust, "case": self.case,
                                          "title": "追蹤退款案件回覆：陳小姐",
                                          "due_at": "2026-10-15T10:00:00+08:00", "kind": "general"})
        fu = [r for r in self.crm.followups.values() if r["case"] == self.case]
        self.assertEqual(len(fu), 1)
        # 不結案、不動案件
        self.assertEqual(self.crm.cases[self.case]["status"], "open")
        # flow-report：UUID 字串
        report = self.ctl.reports[0]
        self.assertEqual((report["step"], report["status"]), ("log_reply", "ok"))
        self.assertEqual(report["output"], {"interaction_id": row["id"], "follow_up_id": fu[0]["id"]})
        self.assertIsInstance(report["output"]["follow_up_id"], str)
        self.assertEqual(report["attempt"], 1)

    def test_rerun_replays_without_duplicates(self):
        code, first = self.run_script("--message-file", self.msg_path, "--report")
        self.write_msg(self.inp, attempt=2)
        code, second = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, second)
        self.assertEqual(len(self.crm.interactions), 1)
        self.assertEqual(len([r for r in self.crm.followups.values() if r["case"] == self.case]), 1)
        self.assertEqual(second["output"], first["output"])

    def test_lost_responses_are_retried(self):
        self.crm.faults[("POST", "/api/v1/interactions/")] = ["drop_after"]
        self.crm.faults[("POST", "/api/v1/follow-ups/")] = ["drop_after", 502]
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.crm.interactions), 1)
        self.assertEqual(len(self.crm.followups), 1)

    def test_interaction_already_logged_with_other_key(self):
        self.run_script("--message-file", self.msg_path)
        self.write_msg(self.inp, keys={"interaction": "other:i", "follow_up": KEY_F})
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.crm.interactions), 1)
        self.assertEqual(len(self.crm.followups), 1)

    def test_missing_write_key_is_refused(self):
        self.write_msg(self.inp, keys={"interaction": KEY_I})
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertIn("Idempotency-Key.follow_up", out["error"])
        self.assertEqual(self.crm.calls, [])

    def test_idempotency_conflict_needs_human(self):
        self.crm.idem[KEY_F] = ("POST", "/api/v1/follow-ups/", "different", 201, {"id": "x"})
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")
        self.assertIn("409", self.ctl.reports[0]["summary"])

    def test_case_of_other_customer_needs_human(self):
        other = self.crm.add_customer("別人", "C-999")
        self.inp["case_id"] = self.crm.add_case(other)
        self.write_msg(self.inp)
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")

    def test_missing_reply_text_needs_human(self):
        self.inp["reply_text"] = ""
        self.write_msg(self.inp)
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")
        self.assertEqual(self.crm.calls, [])

    def test_bad_follow_up_kind_needs_human(self):
        self.inp["follow_up"]["kind"] = "refund"
        self.write_msg(self.inp)
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")

    def test_crm_5xx_reports_system_failure(self):
        self.crm.faults[("POST", "/api/v1/interactions/")] = [500]
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        report = self.ctl.reports[0]
        self.assertEqual((report["status"], report["error_kind"]), ("failed", "system"))

    def test_crm_down_reports_system_failure(self):
        os.environ["CRM_API_BASE"] = "http://127.0.0.1:9/api"
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["error_kind"], "system")

    def test_flow_report_failure_announces(self):
        os.environ["FAKE_FLOW_REPORT_FAIL"] = "1"
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 1)
        self.assertTrue(out["announce"])
        self.assertEqual(len(self.crm.interactions), 1)

    def test_wrong_step_is_refused(self):
        self.write_msg(self.inp, step="draft_reply")
        code, out = self.run_script("--message-file", self.msg_path)
        self.assertEqual(code, 1)
        self.assertEqual(self.crm.calls, [])

    def test_explicit_args_mode(self):
        path = os.path.join(self.tmp, "input.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.inp, fh, ensure_ascii=False)
        code, out = self.run_script("--flow", FLOW, "--input-file", path, "--key-interaction", KEY_I,
                                    "--key-follow-up", KEY_F, "--report")
        self.assertEqual(code, 0, out)
        self.assertNotIn("attempt", self.ctl.reports[0])

    def test_dry_run_does_not_touch_network(self):
        os.environ.pop("CRM_API_TOKEN")
        code, out = self.run_script("--message-file", self.msg_path, "--dry-run", "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_log")
        self.assertTrue(out["interaction_summary"].startswith("[回覆客戶]"))
        self.assertEqual(self.crm.calls, [])
        self.assertEqual(self.ctl.calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
