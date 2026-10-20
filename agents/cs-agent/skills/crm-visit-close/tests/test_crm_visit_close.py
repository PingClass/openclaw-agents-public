"""crm-visit-close 的測試：假 CRM＋假 controller＋假 flow-report。

執行：python3 -m unittest discover -s agents/cs-agent/skills/crm-visit-close/tests -v
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_services import SkillTestCase  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "crm_visit_close.py")
FLOW = "fl_0123456789abcdef"
KEY_I = f"{FLOW}:close_visit:interaction"
KEY_C = f"{FLOW}:close_visit:complete"


def step_message(inp: dict, *, step: str = "close_visit", attempt: int = 1, flow: str | None = FLOW,
                 keys: dict | None = None) -> str:
    """照 controller/backend/app/dispatch.py build_step_message 的格式組步驟指令。"""
    keys = {"interaction": KEY_I, "complete": KEY_C} if keys is None else keys
    lines = ["[workflow-controller 步驟指令]"]
    if flow:
        lines.append(f"flow_id: {flow}")
    lines += ["flow_type: visit", f"step: {step}", f"attempt: {attempt}",
              f"Idempotency-Key: {flow}:{step}"]
    lines += [f"Idempotency-Key.{k}: {v}" for k, v in keys.items()]
    lines += ["skill: crm-visit-close", "---",
              "拜訪「客戶 C」已結束，在 CRM 記錄並完成待辦跟進。",
              "輸入（JSON）：", json.dumps(inp, ensure_ascii=False, indent=2, sort_keys=True), "---",
              f"做完用 flow-report 回報：flow_id={flow}、step={step}、attempt={attempt}、status（ok／failed／needs_human）。",
              "status=ok 時 output 的欄位：",
              "- interaction_id（int，必填）：CRM 互動紀錄 id"]
    return "\n".join(lines) + "\n"


class VisitCloseTest(SkillTestCase):
    SCRIPT = SCRIPT
    MODULE = "crm_visit_close"

    def setUp(self):
        super().setUp()
        self.cust = self.crm.add_customer("客戶 C", "C-003")
        self.fid = self.crm.add_followup(
            self.cust, "[拜訪] 客戶 C｜地點：台中 客戶 C 工廠｜人數：2 人｜時長：120 分鐘｜目的：新品試吃",
            "2026-10-15T10:00:00+08:00")
        self.inp = {
            "follow_up_id": self.fid,
            "customer": {"name": "客戶 C", "external_id": "C-003"},
            "visit_start_at": "2026-10-15T10:00:00+08:00",
            "visit_end_at": "2026-10-15T12:00:00+08:00",
            "location": "台中 客戶 C 工廠",
            "subject": "新品試吃",
            "sales": {"hrm_employee_id": "da1b72d2-89cb-4879-9c50-73e03a1ae993", "display_name": "Mjolmire"},
            "visit_note": "客戶對新品有興趣\n下週報價",
            "distance_km": 37,
            "claim_status": "approved",
        }
        self.msg_path = os.path.join(self.tmp, "step.txt")
        self.write_msg(step_message(self.inp))

    def write_msg(self, text: str):
        with open(self.msg_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def posts(self, path):
        return [c for c in self.crm.calls if c[0] == "POST" and c[1] == path]

    @property
    def complete_path(self):
        return f"/api/v1/follow-ups/{self.fid}/complete/"

    # --- 正常 --------------------------------------------------------------

    def test_close_and_report(self):
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "closed")
        self.assertTrue(out["reported"])
        self.assertFalse(out["announce"])
        # CRM：一筆互動、待辦完成，各用自己的 Idempotency-Key
        self.assertEqual(len(self.crm.interactions), 1)
        row = self.crm.interactions[0]
        self.assertEqual(row["customer"], self.cust)
        self.assertEqual(row["external_message_id"], f"{FLOW}:visit")
        lines = row["summary"].splitlines()
        self.assertEqual(lines[0], "[拜訪] 2026-10-15 10:00-12:00 業務：Mjolmire")
        self.assertEqual(lines[1], f"待辦：{self.fid}")
        self.assertIn("目的：新品試吃", lines)
        self.assertIn("重點：客戶對新品有興趣；下週報價", lines)
        self.assertIn("交通：公務車 往返 37 km（車牌與起迄里程見 ADM 預約）", lines)
        self.assertNotIn("approved", row["summary"])
        self.assertNotIn("NT$", row["summary"])
        self.assertEqual(self.posts("/api/v1/interactions/")[0][4]["Idempotency-Key"], KEY_I)
        self.assertEqual(self.posts(self.complete_path)[0][4]["Idempotency-Key"], KEY_C)
        self.assertEqual(self.crm.followups[self.fid]["status"], "done")
        # controller：flow-report 帶 visit.md 的 output 欄位
        self.assertEqual(len(self.ctl.reports), 1)
        report = self.ctl.reports[0]
        self.assertEqual((report["flow_id"], report["step"], report["status"]), (FLOW, "close_visit", "ok"))
        self.assertEqual(report["output"], {"interaction_id": row["id"], "follow_up_id": self.fid,
                                            "follow_up_status": "done"})
        self.assertIn("客戶 C", report["summary"])
        # 帶上指令的 attempt（visit.md：過期的回報會被拒）
        self.assertEqual(report["attempt"], 1)

    def test_without_report_prints_flow_report_fields(self):
        code, out = self.run_script("--message-file", self.msg_path)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["report_status"], "ok")
        self.assertEqual(set(out["output"]), {"interaction_id", "follow_up_id", "follow_up_status"})
        self.assertIn("flow-report", out["next"])
        self.assertEqual(self.ctl.calls, [])

    def test_message_from_stdin(self):
        code, out = self.run_script("--message-file", "-", "--report", stdin=step_message(self.inp))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["reported"])

    def test_explicit_args_mode(self):
        inp_path = os.path.join(self.tmp, "input.json")
        with open(inp_path, "w", encoding="utf-8") as fh:
            json.dump(self.inp, fh, ensure_ascii=False)
        code, out = self.run_script("--flow", FLOW, "--input-file", inp_path,
                                    "--key-interaction", KEY_I, "--key-complete", KEY_C, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.posts("/api/v1/interactions/")[0][4]["Idempotency-Key"], KEY_I)
        self.assertNotIn("attempt", self.ctl.reports[0])  # 沒給 --attempt 就不帶

    # --- 冪等重試 ----------------------------------------------------------

    def test_lost_responses_are_retried_without_duplicates(self):
        self.crm.faults[("POST", "/api/v1/interactions/")] = ["drop_after", 503]
        self.crm.faults[("POST", self.complete_path)] = ["drop_after"]
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.crm.interactions), 1)
        self.assertEqual(len(self.posts("/api/v1/interactions/")), 3)
        self.assertEqual(len(self.posts(self.complete_path)), 2)
        self.assertEqual(out["output"]["interaction_id"], self.crm.interactions[0]["id"])
        self.assertEqual(out["output"]["follow_up_status"], "done")

    def test_rerun_replays_same_keys(self):
        code, first = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, first)
        # controller 重派（attempt 2，鍵不變）
        self.write_msg(step_message(self.inp, attempt=2))
        code, second = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, second)
        self.assertEqual(len(self.crm.interactions), 1)
        self.assertEqual(second["output"], first["output"])
        self.assertEqual(len(self.ctl.reports), 2)
        self.assertEqual([r["attempt"] for r in self.ctl.reports], [1, 2])

    def test_stale_attempt_report_is_rejected(self):
        # controller 已經重派到 attempt 2，舊的 attempt 1 跑完回報會被拒，不會蓋掉新的結果
        self.ctl.current_attempts[(FLOW, "close_visit")] = 2
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 1)
        self.assertFalse(out["reported"])
        self.assertIn("stale_attempt", json.dumps(out["report_error"], ensure_ascii=False))
        self.assertEqual(self.ctl.reports, [])
        self.assertEqual(len(self.crm.interactions), 1)  # CRM 寫入照樣冪等，重派那次會重播

    def test_already_recorded_with_other_keys(self):
        self.run_script("--message-file", self.msg_path)
        self.write_msg(step_message(self.inp, keys={"interaction": "other:i", "complete": "other:c"}))
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.crm.interactions), 1)
        self.assertEqual(out["output"]["interaction_id"], self.crm.interactions[0]["id"])
        self.assertEqual(out["output"]["follow_up_status"], "done")

    # --- 失敗 --------------------------------------------------------------

    def test_idempotency_conflict_needs_human(self):
        self.crm.idem[KEY_I] = ("POST", "/api/v1/interactions/", "different", 201, {"id": "x"})
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)  # 已回報 controller，交給人
        self.assertEqual(out["report_status"], "needs_human")
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")
        self.assertIn("409", self.ctl.reports[0]["summary"])
        self.assertEqual(self.crm.followups[self.fid]["status"], "pending")

    def test_missing_follow_up_needs_human(self):
        self.inp["follow_up_id"] = "00000000-0000-0000-0000-000000000000"
        self.write_msg(step_message(self.inp))
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")
        self.assertEqual(self.crm.interactions, [])

    def test_crm_down_reports_system_failure(self):
        os.environ["CRM_API_BASE"] = "http://127.0.0.1:9/api"
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        report = self.ctl.reports[0]
        self.assertEqual((report["status"], report["error_kind"]), ("failed", "system"))

    def test_crm_5xx_reports_system_failure(self):
        self.crm.faults[("POST", "/api/v1/interactions/")] = [500, 500, 500, 500]
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["error_kind"], "system")

    def test_missing_write_key_is_refused(self):
        self.write_msg(step_message(self.inp, keys={"interaction": KEY_I}))
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertIn("Idempotency-Key.complete", out["error"])
        self.assertEqual(self.crm.calls, [])

    def test_message_without_flow_id_is_refused(self):
        self.write_msg(step_message(self.inp, flow=None))
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 1)
        self.assertIn("flow_id", out["error"])
        self.assertEqual(self.crm.calls, [])

    def test_wrong_step_is_refused(self):
        self.write_msg(step_message(self.inp, step="book_vehicle"))
        code, out = self.run_script("--message-file", self.msg_path)
        self.assertEqual(code, 1)
        self.assertEqual(self.crm.calls, [])

    def test_message_without_step_is_refused(self):
        text = step_message(self.inp).replace("step: close_visit\n", "")
        self.assertNotIn("step: close_visit", text)
        self.write_msg(text)
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertEqual(self.crm.calls, [])

    def test_missing_crm_token_reports_needs_human(self):
        os.environ.pop("CRM_API_TOKEN")
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctl.reports[0]["status"], "needs_human")
        self.assertIn("CRM_API_TOKEN", self.ctl.reports[0]["summary"])
        self.assertEqual(self.crm.calls, [])

    def test_not_a_step_message(self):
        self.write_msg("請幫我把 fl_123 的拜訪結案")
        code, out = self.run_script("--message-file", self.msg_path)
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")

    def test_flow_report_failure_announces(self):
        os.environ["FAKE_FLOW_REPORT_FAIL"] = "1"
        code, out = self.run_script("--message-file", self.msg_path, "--report")
        self.assertEqual(code, 1)
        self.assertFalse(out["reported"])
        self.assertTrue(out["announce"])
        self.assertEqual(self.crm.followups[self.fid]["status"], "done")

    def test_dry_run_does_not_touch_network(self):
        os.environ.pop("CRM_API_TOKEN")
        code, out = self.run_script("--message-file", self.msg_path, "--dry-run", "--report")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_close")
        self.assertTrue(out["interaction_summary"].startswith("[拜訪] 2026-10-15 10:00-12:00"))
        self.assertEqual(self.crm.calls, [])
        self.assertEqual(self.ctl.calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
