"""agent-inbox（inbox.py）的測試：假 controller。"""

from __future__ import annotations

import os
import unittest

from fakes import FakeController
from harness import SkillTestCase, load

NOW = "2026-10-12T10:00:00+08:00"


class InboxTestBase(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.ctl = FakeController({"tok-gobta": "gobta", "tok-main": "main", "tok-hinata": "hinata"})
        self.env.update({
            "FLOW_CONTROLLER_URL": self.ctl.url,
            "FLOW_CONTROLLER_TOKEN_GOBTA": "tok-gobta",
            "FLOW_CONTROLLER_TOKEN_MAIN": "tok-main",
            "FLOW_CONTROLLER_TOKEN_HINATA": "tok-hinata",
        })

    def tearDown(self):
        self.ctl.stop()
        super().tearDown()

    def check(self, agent, extra_env=None):
        return self.run_script("inbox", ["check", "--agent", agent, "--now", NOW], extra_env)

    def dispatch(self, agent="gobta", approval_id="ap-1"):
        return self.ctl.add_approval(
            agent, approval_id, question="請確認拜訪客戶1 的派車草稿", step_title="業務確認派車",
            flow_title="gobta 10/13 拜訪客戶1",
            context={"車輛": "ABC-1234", "時段": "2026-10-13 10:00～11:30", "目的地": "台北市內湖區"})

    def expense(self, amount, approval_id="ap-2", agent="main"):
        return self.ctl.add_approval(
            agent, approval_id, question=f"請核准交通費報帳 NT${amount}", step_title="主管核准報帳",
            flow_title="gobta 10/13 拜訪客戶1", context={"amount": amount, "distance_km": 30})


class AmountTextTests(InboxTestBase):
    def test_amount_from_question_uses_total_not_rate(self):
        """context 沒有金額欄位時，從問題文字取總額（不是 NT$5/km 的費率）。"""
        self.ctl.add_approval(
            "main", "ap-9", step_title="主管核准報帳", flow_title="10/13 拜訪 客戶1",
            question="請核准 gobta 10/13 拜訪「客戶1」的交通費：120 km × NT$5/km（demo 假費率） = NT$600",
            context={"distance_km": 120})
        self.set_rng(0.10)  # < 15%：金額 > 500 時退回
        code, out = self.check("main")
        self.assertEqual(code, 0, out)
        record = self.state("agent-inbox", "main", "handled.json")["approvals"]["ap-9"]
        self.assertEqual(record["amount"], 600)
        self.assertEqual(self.ctl.approvals["ap-9"]["decision"], "reject")


class SalesDispatchTests(InboxTestBase):
    def test_approves_dispatch_90_percent(self):
        self.dispatch()
        self.set_rng(0.89)
        code, out = self.check("gobta")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "decided")
        approval = self.ctl.approvals["ap-1"]
        self.assertEqual(approval["decision"], "approve")
        self.assertTrue(approval["comment"])
        self.assertFalse(out["announce"])
        # 記下派車確認頁內容，給 report-trip 用
        record = self.state("agent-inbox", "gobta", "handled.json")["approvals"]["ap-1"]
        self.assertEqual(record["category"], "dispatch")
        self.assertEqual(record["flow_id"], "flow-1")
        self.assertIn("時段", record["context"])

    def test_rejects_dispatch_10_percent(self):
        self.dispatch()
        self.set_rng(0.91)
        self.check("gobta")
        approval = self.ctl.approvals["ap-1"]
        self.assertEqual(approval["decision"], "reject")
        self.assertTrue(approval["comment"].endswith("。"))

    def test_rerun_does_not_decide_twice(self):
        self.dispatch()
        self.check("gobta")
        code, out = self.check("gobta")
        self.assertEqual(out["outcome"], "nothing_to_do")
        self.assertEqual(self.ctl.count("POST", "/api/approvals/ap-1/decision"), 1)

    def test_failed_post_keeps_the_same_decision(self):
        self.dispatch()
        self.ctl.faults[("POST", "/api/approvals/ap-1/decision")] = [503]
        self.set_rng(0.95)  # 退回
        code, out = self.check("gobta")
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "error")
        self.assertFalse(out["announce"])
        first = self.state("agent-inbox", "gobta", "handled.json")["approvals"]["ap-1"]
        self.set_rng(0.01)  # 重跑不能重擲骰
        code, out = self.check("gobta")
        self.assertEqual(out["outcome"], "decided")
        approval = self.ctl.approvals["ap-1"]
        self.assertEqual(approval["decision"], "reject")
        self.assertEqual(approval["comment"], first["comment"])

    def test_already_decided_by_human_is_closed(self):
        self.dispatch()
        self.ctl.approvals["ap-1"]["status"] = "decided"
        code, out = self.check("gobta")
        self.assertEqual(out["outcome"], "nothing_to_do")
        self.assertEqual(out["skipped"][0]["reason"], "token_used")
        self.assertEqual(self.ctl.count("POST", "/api/approvals/ap-1/decision"), 0)

    def test_resent_link_uses_newest_notification(self):
        self.dispatch()
        old = self.ctl.notifications["gobta"][0]
        # 重送：換新 token，舊連結失效
        new_token = self.dispatch(approval_id="ap-1")
        self.ctl.notifications["gobta"][-1]["created_at"] = "2026-10-12T01:30:00+00:00"
        self.assertNotIn(new_token, old["link"])
        code, out = self.check("gobta")
        self.assertEqual(out["outcome"], "decided", out)

    def test_invalid_old_link_waits_for_new_notification(self):
        self.dispatch()
        self.ctl.approvals["ap-1"]["token"] = "rotated"
        code, out = self.check("gobta")
        self.assertEqual(out["skipped"][0]["reason"], "invalid_token")
        self.assertEqual(out["outcome"], "nothing_to_do")


class ManagerExpenseTests(InboxTestBase):
    def test_over_500_rejected_15_percent(self):
        self.expense(820)
        self.set_rng(0.10)
        self.check("main")
        approval = self.ctl.approvals["ap-2"]
        self.assertEqual(approval["decision"], "reject")
        self.assertIn("820", approval["comment"])

    def test_over_500_approved_otherwise(self):
        self.expense(820)
        self.set_rng(0.20)
        self.check("main")
        self.assertEqual(self.ctl.approvals["ap-2"]["decision"], "approve")

    def test_small_amount_always_approved(self):
        self.expense(300)
        self.set_rng(0.0)
        self.check("main")
        self.assertEqual(self.ctl.approvals["ap-2"]["decision"], "approve")

    def test_amount_from_question_text(self):
        self.ctl.add_approval("main", "ap-3", question="請核准這筆交通費報帳，金額 NT$1,250",
                              step_title="主管核准報帳", context={"里程": "125 km"})
        self.set_rng(0.05)
        self.check("main")
        self.assertEqual(self.ctl.approvals["ap-3"]["decision"], "reject")
        record = self.state("agent-inbox", "main", "handled.json")["approvals"]["ap-3"]
        self.assertEqual(record["amount"], 1250)


REFUND_OPTIONS = [{"value": "approve", "label": "同意退款"},
                  {"value": "reject", "label": "不退款"},
                  {"value": "escalate", "label": "轉人工處理"}]


class RefundTests(InboxTestBase):
    """流程 ② 的兩種確認（controller docs/flows/refund.md）：客服主管 hinata。"""

    def refund_decision(self, approval_id="ap-r1"):
        return self.ctl.add_approval(
            "hinata", approval_id, step_title="客服主管決定退款", flow_title="客訴退款評估：陳小姐",
            question="請決定「陳小姐」的退款案件：財會推估建議 NT$1,500（推估，非核定）。",
            options=REFUND_OPTIONS, context={"suggested_amount": "NT$1,500（推估）"})

    def reply_review(self, approval_id="ap-r2"):
        return self.ctl.add_approval(
            "hinata", approval_id, step_title="客服主管確認回覆內容", flow_title="客訴退款評估：陳小姐",
            question="請確認回覆客戶「陳小姐」的內容（決定：同意退款 NT$1,200）。核准後就會送出給客戶。",
            options=[{"value": "approve", "label": "核准送出"}, {"value": "revise", "label": "退回重擬"}],
            context={"reply_text": "我們將退款 NT$1,200"})

    def test_refund_decision_80_10_10(self):
        for roll, expected in ((0.79, "approve"), (0.85, "reject"), (0.95, "escalate")):
            with self.subTest(roll=roll):
                approval_id = f"ap-r-{expected}"
                self.refund_decision(approval_id)
                self.set_rng(roll)
                code, out = self.check("hinata")
                self.assertEqual(code, 0, out)
                approval = self.ctl.approvals[approval_id]
                self.assertEqual(approval["decision"], expected)
                self.assertTrue(approval["comment"])
                record = self.state("agent-inbox", "hinata", "handled.json")["approvals"][approval_id]
                self.assertEqual(record["category"], "refund_decision")
                self.assertEqual(record["rule"], "cs_manager_refund_80_10_10")

    def test_refund_approve_does_not_send_amount(self):
        """同意退款時不帶金額，controller 用推估金額；金額不從問題文字裡猜。"""
        self.refund_decision()
        self.set_rng(0.10)
        self.check("hinata")
        approval = self.ctl.approvals["ap-r1"]
        self.assertEqual(approval["decision"], "approve")
        self.assertIsNone(self.state("agent-inbox", "hinata", "handled.json")["approvals"]["ap-r1"]["amount"])

    def test_refund_approve_without_suggested_amount_becomes_reject(self):
        """推估金額是 0 時，不帶金額同意會被 controller 422 擋下並每小時重送卡住，改成不退款。"""
        self.ctl.add_approval(
            "hinata", "ap-r0", step_title="客服主管決定退款", question="請決定「陳小姐」的退款案件：財會推估建議 NT$0。",
            options=REFUND_OPTIONS,
            fields=[{"name": "amount_twd", "label": "退款金額（NT$）", "type": "int",
                     "applies_to": ["approve"], "default": 0, "min": 1, "max": 1000000}])
        self.set_rng(0.10)
        code, out = self.check("hinata")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.approvals["ap-r0"]["decision"], "reject")
        record = self.state("agent-inbox", "hinata", "handled.json")["approvals"]["ap-r0"]
        self.assertEqual(record["rule"], "cs_manager_refund_80_10_10_no_suggested_amount")
        self.assertFalse(record["approve"])

    def test_refund_approve_with_suggested_amount_field(self):
        self.ctl.add_approval(
            "hinata", "ap-r5", step_title="客服主管決定退款", question="請決定退款",
            options=REFUND_OPTIONS,
            fields=[{"name": "amount_twd", "applies_to": ["approve"], "default": 1500}])
        self.set_rng(0.10)
        self.check("hinata")
        self.assertEqual(self.ctl.approvals["ap-r5"]["decision"], "approve")

    def test_reply_review_90_percent(self):
        self.reply_review()
        self.set_rng(0.91)
        self.check("hinata")
        self.assertEqual(self.ctl.approvals["ap-r2"]["decision"], "revise")
        self.reply_review("ap-r3")
        self.set_rng(0.89)
        self.check("hinata")
        self.assertEqual(self.ctl.approvals["ap-r3"]["decision"], "approve")
        record = self.state("agent-inbox", "hinata", "handled.json")["approvals"]["ap-r3"]
        self.assertEqual(record["category"], "reply_review")

    def test_other_role_uses_95_rule_for_refund(self):
        self.ctl.add_approval("main", "ap-r4", step_title="客服主管決定退款", question="請決定退款",
                              options=REFUND_OPTIONS)
        self.set_rng(0.96)
        self.check("main")
        self.assertEqual(self.ctl.approvals["ap-r4"]["decision"], "reject")


class OtherApprovalTests(InboxTestBase):
    def test_other_95_percent(self):
        self.ctl.add_approval("main", "ap-4", question="請確認會議紀錄", step_title="主管確認",
                              options=[{"value": "approve", "label": "核准"},
                                       {"value": "reject", "label": "退回"}])
        self.set_rng(0.96)
        self.check("main")
        self.assertEqual(self.ctl.approvals["ap-4"]["decision"], "reject")

    def test_ack_only_options(self):
        self.ctl.add_approval("main", "ap-5", question="新人到職清單請確認收到", step_title="主管確認",
                              options=[{"value": "ack", "label": "確認收到"}])
        self.set_rng(0.99)
        self.check("main")
        self.assertEqual(self.ctl.approvals["ap-5"]["decision"], "ack")

    def test_revise_counts_as_reject(self):
        self.ctl.add_approval("main", "ap-6", question="請確認草稿", step_title="確認草稿",
                              options=[{"value": "approve", "label": "核准"},
                                       {"value": "revise", "label": "退回重擬"},
                                       {"value": "reject", "label": "不核准"}])
        self.set_rng(0.99)
        self.check("main")
        self.assertEqual(self.ctl.approvals["ap-6"]["decision"], "revise")


class OnboardingTests(InboxTestBase):
    """流程 ③：主管確認收到到職清單一律確認；IT 任務只列出不處理。"""

    def onboarding(self, agent="main", approval_id="ap-7"):
        return self.ctl.add_approval(
            agent, approval_id, step_title="主管確認收到到職清單",
            question="請確認收到新人 王小明（業務部，10/19 到職）的到職清單；有缺漏請選「有缺漏」並說明，會轉給人資處理",
            flow_title="10/19 到職 王小明（業務部）",
            options=[{"value": "ack", "label": "確認收到"},
                     {"value": "missing", "label": "有缺漏（請在意見說明）"}],
            context={"new_hire": "王小明", "laptop": "LT-003", "checklist": "1. 到職說明"})

    def test_onboarding_ack_always(self):
        for agent, roll in (("main", 0.99), ("hinata", 0.0)):
            self.onboarding(agent=agent, approval_id=f"ap-{agent}")
            self.set_rng(roll)
            code, out = self.check(agent)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.ctl.approvals[f"ap-{agent}"]["decision"], "ack")
            record = self.state("agent-inbox", agent, "handled.json")["approvals"][f"ap-{agent}"]
            self.assertEqual(record["category"], "onboarding")
            self.assertEqual(record["rule"], "onboarding_ack")
            self.assertTrue(self.ctl.approvals[f"ap-{agent}"]["comment"])

    def test_task_notifications_are_listed_not_handled(self):
        self.ctl.tokens["tok-souei"] = "souei"
        self.ctl.notifications["souei"] = [{
            "id": 900, "kind": "task", "title": "IT 任務：配發筆電給新人 王小明",
            "body": "建議資產 LT-003", "link": None, "flow_id": "flow-9", "approval_id": None,
            "created_at": "2026-10-12T01:00:00+00:00", "read_at": None,
        }]
        code, out = self.check("souei", {"FLOW_CONTROLLER_TOKEN_SOUEI": "tok-souei"})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "nothing_to_do")
        self.assertFalse(out["announce"])
        self.assertEqual(out["tasks"], [{"notification_id": 900, "flow_id": "flow-9",
                                         "title": "IT 任務：配發筆電給新人 王小明"}])


class ControlTests(InboxTestBase):
    def test_paused_by_env_and_file(self):
        self.dispatch()
        _, out = self.check("gobta", {"AGENT_INBOX_PAUSED": "gobta,main"})
        self.assertEqual(out["outcome"], "paused")
        os.makedirs(os.path.join(self.state_dir, "agent-inbox", "gobta"))
        open(os.path.join(self.state_dir, "agent-inbox", "gobta", "PAUSED"), "w").close()
        _, out = self.check("gobta")
        self.assertEqual(out["outcome"], "paused")
        self.assertEqual(self.ctl.count("POST", "/api/approvals/ap-1/decision"), 0)

    def test_human_confirm_mode_on_controller_pauses(self):
        """admin 在前端把 gobta 切成真人確認：agent-inbox 不處理，交給講師。"""
        self.dispatch()
        self.ctl.confirm_modes["gobta"] = "human"
        code, out = self.check("gobta")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "paused")
        self.assertEqual(out["confirm_mode"], "human")
        self.assertFalse(out["announce"])
        self.assertEqual(self.ctl.count("GET", "/api/inbox"), 0)
        self.assertEqual(self.ctl.count("POST", "/api/approvals/ap-1/decision"), 0)
        # 只影響被切換的人：main 照常處理
        self.expense(300)
        _, out = self.check("main")
        self.assertEqual(out["outcome"], "decided")
        self.assertEqual(out["confirm_mode"], "agent")

    def test_switch_back_to_agent_resumes(self):
        self.dispatch()
        self.ctl.confirm_modes["gobta"] = "human"
        _, out = self.check("gobta")
        self.assertEqual(out["outcome"], "paused")
        self.ctl.confirm_modes["gobta"] = "agent"
        _, out = self.check("gobta")
        self.assertEqual(out["outcome"], "decided")
        self.assertEqual(self.ctl.count("POST", "/api/approvals/ap-1/decision"), 1)

    def test_old_controller_without_confirm_mode_endpoint(self):
        """舊版 controller 沒有 /api/inbox/confirm-mode（404）：照舊處理。"""
        self.ctl.confirm_mode_supported = False
        self.dispatch()
        code, out = self.check("gobta")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "decided")
        self.assertIsNone(out["confirm_mode"])

    def test_hidden_links_are_never_decided(self):
        """就算查確認方式失敗，controller 在 human 時也不給連結，agent 無法代為確認。"""
        self.ctl.confirm_mode_supported = False
        self.ctl.confirm_modes["gobta"] = "human"
        self.dispatch()
        code, out = self.check("gobta")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "nothing_to_do")
        self.assertEqual(self.ctl.count("POST", "/api/approvals/ap-1/decision"), 0)

    def test_bad_token_is_announced(self):
        code, out = self.check("gobta", {"FLOW_CONTROLLER_TOKEN_GOBTA": "wrong"})
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "inbox_denied")
        self.assertTrue(out["announce"])

    def test_controller_down_is_quiet(self):
        code, out = self.check("gobta", {"FLOW_CONTROLLER_URL": "http://127.0.0.1:9"})
        self.assertEqual(code, 1)
        self.assertFalse(out["announce"])


class StatusTests(InboxTestBase):
    def status(self, agent, *extra, extra_env=None):
        return self.run_script("inbox", ["status", "--agent", agent, "--now", NOW, *extra], extra_env)

    def write_handled(self, agent, approvals):
        self.c.save_json(os.path.join(self.state_dir, "agent-inbox", agent, "handled.json"),
                         {"approvals": approvals, "skipped_notifications": {}})

    def test_status_after_check_counts_decision(self):
        self.dispatch()
        self.set_rng(0.95)  # 退回
        self.check("gobta")
        record = self.state("agent-inbox", "gobta", "handled.json")["approvals"]["ap-1"]
        self.assertEqual(record["action"], "reject")
        code, out = self.status("gobta")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "status")
        self.assertFalse(out["announce"])
        row = out["stats"]["by_category"]["dispatch"]
        self.assertEqual((row["decided"], row["approve"], row["reject"]), (1, 0, 1))
        self.assertEqual(row["reject_rate"], 1.0)
        self.assertEqual(row["expected"], {"approve": 0.9, "reject": 0.1})
        self.assertEqual(row["rolls"], [0.95])

    def test_status_rates_window_and_old_records(self):
        def rec(category, approve, decision, day, roll, status="decided", **more):
            r = {"status": status, "category": category, "approve": approve, "decision": decision,
                 "roll": roll, "first_seen": day, **more}
            if status == "decided":
                r["decided_at"] = f"{day}T11:00:00+08:00"
            return r

        self.write_handled("hinata", {
            # 決策紀錄提到的情況：回覆確認連續三次退回
            "a1": rec("reply_review", False, "reject", "2026-10-10", 0.93),
            "a2": rec("reply_review", False, "reject", "2026-10-11", 0.95),
            "a3": rec("reply_review", False, "reject", "2026-10-12", 0.96),
            "a4": rec("reply_review", True, "approve", "2026-09-25", 0.2),  # 7 天外
            # 舊紀錄沒有 action：轉人工由送出的選項值推回來
            "a5": rec("refund_decision", False, "escalate", "2026-10-11", 0.97),
            "a6": rec("refund_decision", True, "approve", "2026-10-12", 0.1, action="approve"),
            "a7": rec("reply_review", None, None, "2026-10-12", 0.5, status="pending"),
            "a8": {"status": "closed", "reason": "token_used", "first_seen": "2026-10-12"},
        })
        code, out = self.status("hinata")
        self.assertEqual(code, 0, out)
        stats = out["stats"]
        self.assertEqual((stats["days"], stats["since"], stats["until"]), (7, "2026-10-06", "2026-10-12"))
        reply = stats["by_category"]["reply_review"]
        self.assertEqual((reply["decided"], reply["approve"], reply["reject"]), (3, 0, 3))
        self.assertEqual((reply["approve_rate"], reply["reject_rate"]), (0.0, 1.0))
        self.assertEqual(reply["rolls"], [0.93, 0.95, 0.96])
        self.assertEqual(reply["expected"]["approve"], 0.9)
        refund = stats["by_category"]["refund_decision"]
        self.assertEqual((refund["approve"], refund["reject"], refund["escalate"]), (1, 0, 1))
        self.assertEqual(refund["escalate_rate"], 0.5)
        self.assertEqual(refund["expected"], {"approve": 0.8, "reject": 0.1, "escalate": 0.1})
        self.assertEqual((stats["pending"], stats["closed"]), (1, 1))
        self.assertIn("回覆確認 核准 0／退回 3（3 件，核准率 0%，預期 90%）", out["detail"])
        self.assertIn("轉人工 1", out["detail"])
        # 不連網
        self.assertEqual(self.ctl.count("GET", "/api/inbox"), 0)

        _, out = self.status("hinata", "--days", "30")
        self.assertEqual(out["stats"]["by_category"]["reply_review"]["decided"], 4)

    def test_status_without_state(self):
        code, out = self.status("main")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["stats"]["by_category"], {})
        self.assertIn("沒有送出過的確認", out["detail"])

    def test_status_days_out_of_range(self):
        code, out = self.status("main", "--days", "0")
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")


class RuleUnitTests(unittest.TestCase):
    def setUp(self):
        self.inbox = load("inbox")

    def test_decide_table(self):
        d = self.inbox.decide
        self.assertEqual(d("sales", "dispatch", None, 0.899)[0], True)
        self.assertEqual(d("sales", "dispatch", None, 0.90)[0], False)
        self.assertEqual(d("sales_manager", "expense", 501, 0.149)[0], False)
        self.assertEqual(d("sales_manager", "expense", 500, 0.0)[0], True)
        self.assertEqual(d("sales_manager", "expense", None, 0.0)[0], True)
        self.assertEqual(d("sales", "expense", 900, 0.5)[0], True)  # 業務碰到報帳走「其他」
        self.assertEqual(d("it", "other", None, 0.949)[0], True)
        self.assertEqual(d("it", "other", None, 0.95)[0], False)

    def test_classify(self):
        cls = self.inbox.classify
        self.assertEqual(cls({"step_title": "業務確認派車"}, {}), "dispatch")
        self.assertEqual(cls({"step_title": "主管核准報帳", "context": {"車輛": "X"}}, {}), "expense")
        self.assertEqual(cls({"step_title": "確認收到"}, {}), "other")
        self.assertEqual(cls({"step_title": "客服主管決定退款"}, {}), "refund_decision")
        self.assertEqual(cls({"step_title": "x", "options": REFUND_OPTIONS}, {}), "refund_decision")
        self.assertEqual(cls({"step_title": "客服主管確認回覆內容"}, {}), "reply_review")
        self.assertEqual(cls({"step_title": "主管確認收到到職清單", "context": {"車輛": "X"}}, {}), "onboarding")
        self.assertEqual(self.inbox.decide("sales_manager", "onboarding", None, 0.99), (True, "onboarding_ack"))

    def test_refund_action_table(self):
        act = self.inbox.refund_action
        self.assertEqual(act("cs_manager", 0.799)[0], "approve")
        self.assertEqual(act("cs_manager", 0.80)[0], "reject")
        self.assertEqual(act("cs_manager", 0.90)[0], "escalate")
        self.assertEqual(act("it", 0.5)[0], "approve")
        self.assertEqual(self.inbox.decide("cs_manager", "reply_review", None, 0.90)[0], False)


if __name__ == "__main__":
    unittest.main()
