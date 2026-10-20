"""agent-adm assign-laptop 的測試：假 ADM（session staff、A3／A4）＋假 controller（流程 ③ await_laptop）。"""

from __future__ import annotations

import unittest

from fakes import FakeAdm, FakeController
from harness import SkillTestCase

MONDAY = "2026-10-12"
NOW = "2026-10-12T10:07:00+08:00"
NEW_HIRE = "0b9c1d2e-3f40-4a5b-8c6d-7e8f9a0b1c2d"
SOUEI_HRM = "895ced65-5f19-4b5b-a8b3-46529c931c1d"
FLOW = "fl_onboard_0001"
AGENTS = ("main", "shion", "souei", "gobta", "mjolmire-businessman", "luminous", "hinata")
EVENTS = f"/api/flows/{FLOW}/events"


def laptops() -> list:
    names = ["14 吋文書筆電", "14 吋文書筆電", "14 吋文書筆電", "15 吋業務筆電", "15 吋業務筆電", "16 吋開發筆電"]
    return [{"id": n, "code": f"NB-{n:03d}", "name": name, "category": "laptop", "serial_number": f"DEMO-NB-{n:03d}",
             "location": "資訊部倉庫", "custodian": None, "is_active": True}
            for n, name in enumerate(names, start=1)]


class AssignLaptopBase(SkillTestCase):
    def setUp(self):
        super().setUp()
        users = {a: {"password": "apw", "id": i, "staff": a == "souei"} for i, a in enumerate(AGENTS, start=1)}
        users["wang"] = {"password": "x", "id": 50}  # 新人（HR 已在 ADM 建帳號並連結 HRM）
        users["other"] = {"password": "x", "id": 60}
        self.adm = FakeAdm(users, [], [], assets=laptops(),
                           links={NEW_HIRE: 50, SOUEI_HRM: 3, "aaaaaaaa-0000-4000-8000-000000000060": 60})
        self.ctl = FakeController({"tok-souei": "souei", "tok-main": "main"})
        self.env.update({
            "ADM_API_BASE": f"{self.adm.url}/api/v1",
            "ADM_AGENT_PASSWORD": "apw",
            "FLOW_CONTROLLER_URL": self.ctl.url,
            "FLOW_CONTROLLER_TOKEN_SOUEI": "tok-souei",
            "FLOW_CONTROLLER_TOKEN_MAIN": "tok-main",
            # 這把 IT token 不該被 assign-laptop 拿來用
            "ADM_API_TOKEN_IT": "adm_it_token_should_not_be_used",
        })

    def tearDown(self):
        self.adm.stop()
        self.ctl.stop()
        super().tearDown()

    def assign(self, agent="souei", *extra, env=None):
        return self.run_script("adm", ["assign-laptop", "--agent", agent, "--date", MONDAY, "--now", NOW, *extra],
                               env)

    def asset(self, code: str) -> dict:
        return next(a for a in self.adm.assets if a["code"] == code)

    def assign_posts(self) -> list:
        return [c for c in self.adm.calls if c[0] == "POST" and c[1].endswith("/assign/")]

    def task_read(self) -> bool:
        return all(n.get("read_at") for n in self.ctl.notifications["souei"])


class AssignLaptopTests(AssignLaptopBase):
    def test_assigns_suggested_laptop_and_reports_event(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "assigned")
        self.assertFalse(out["announce"])
        self.assertEqual(out["assigned"], [{"flow_id": FLOW, "asset_code": "NB-004", "already_assigned": False,
                                            "matches_suggestion": True}])
        self.assertEqual(self.asset("NB-004")["custodian"], 50)
        (post,) = self.assign_posts()
        _m, path, _q, body, headers = post
        self.assertEqual(path, "/api/v1/assets/NB-004/assign/")
        # 一律帶新人姓名與員工編號（state.input）：已有帳號時 ADM 忽略，沒有時建立佔位保管人
        self.assertEqual(body, {"custodian_hrm_employee_id": NEW_HIRE, "custodian_display_name": "王小明",
                                "custodian_employee_number": "E2026101"})
        lower = {k.lower(): v for k, v in headers.items()}  # urllib 會改寫 header 大小寫
        self.assertTrue(lower.get("x-csrftoken"))
        self.assertEqual(lower.get("idempotency-key"), f"agent-adm:souei:assign:{FLOW}")
        self.assertEqual(self.adm.audit[0]["actor"], "souei")
        # controller 收到 laptop_assigned，任務標成已讀
        (event,) = self.ctl.events
        self.assertEqual(event, (FLOW, "souei", {"type": "laptop_assigned", "asset_code": "NB-004"}))
        self.assertEqual(self.ctl.flows[FLOW]["current_step"], "book_room")
        self.assertTrue(self.task_read())
        state = self.state("agent-adm", "souei", "assign-laptop.json")
        self.assertEqual(state["flows"][FLOW]["status"], "reported")

    def test_session_only_no_token_used_for_assignment(self):
        """指派一律用 souei 的 ADM session：ADM 收到的請求都沒有 Authorization（token）。"""
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        self.assign()
        self.assertTrue(self.adm.calls)
        for _m, _p, _q, _b, headers in self.adm.calls:
            self.assertNotIn("Authorization", headers)
        self.assertNotIn("adm_it_token_should_not_be_used", repr(self.adm.calls))
        # 對照：假 ADM 對帶 token 的指派一律 403（A4 規則）
        status, _body, _ = self.adm.route("POST", "/api/v1/assets/NB-001/assign/", {},
                                          {"custodian_hrm_employee_id": NEW_HIRE},
                                          {"Authorization": "Bearer adm_it_token_should_not_be_used"}, {})
        self.assertEqual(status, 403)

    def test_already_assigned_to_same_person_counts_as_success(self):
        # IT 在網頁上先手動指派了（或上次指派後回報失敗）：不再指派第二台，直接回報
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        self.asset("NB-004")["custodian"] = 50
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "assigned")
        self.assertTrue(out["assigned"][0]["already_assigned"])
        self.assertEqual(self.assign_posts(), [])
        self.assertEqual(self.adm.audit, [])
        self.assertEqual(self.ctl.events[0][2]["asset_code"], "NB-004")

    def test_already_assigned_alternative_is_reused(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        self.asset("NB-001")["custodian"] = 50  # 新人名下已有備選那台
        code, out = self.assign()
        self.assertEqual(out["assigned"][0]["asset_code"], "NB-001")
        self.assertEqual(self.assign_posts(), [])
        self.assertIsNone(self.asset("NB-004")["custodian"], "不會再指派第二台")

    def test_suggested_taken_uses_alternative(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        self.asset("NB-004")["custodian"] = 60
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["assigned"][0]["asset_code"], "NB-005")
        self.assertFalse(out["assigned"][0]["matches_suggestion"])
        self.assertEqual(self.asset("NB-004")["custodian"], 60, "別人的筆電不會被改掉")
        event = self.ctl.events[0][2]
        self.assertEqual(event["asset_code"], "NB-005")
        self.assertIn("NB-004", event["note"])

    def test_no_candidate_available_needs_human_once(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        for code in ("NB-004", "NB-005", "NB-001"):
            self.asset(code)["custodian"] = 60
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "needs_human")
        self.assertTrue(out["announce"])
        self.assertEqual(self.assign_posts(), [])
        self.assertEqual(self.ctl.events, [])
        self.assertFalse(self.task_read(), "任務留著，修好後下一輪再處理")
        # 下一輪：還是不行，但不再重複喊
        code, out = self.assign()
        self.assertEqual(out["outcome"], "needs_human")
        self.assertFalse(out["announce"])
        # 人處理好（IT 釋出 NB-005）後，下一輪自動完成
        self.asset("NB-005")["custodian"] = None
        code, out = self.assign()
        self.assertEqual(out["outcome"], "assigned")
        self.assertEqual(out["assigned"][0]["asset_code"], "NB-005")

    def test_new_hire_without_adm_account_gets_placeholder_custodian(self):
        """新人還沒有 ADM 帳號：帶姓名指派，ADM 建立未啟用的佔位保管人，照常回報並在 note 註明。"""
        unlinked = "99999999-0000-4000-8000-000000000000"
        self.ctl.add_onboarding(FLOW, employee_id=unlinked)
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "assigned")
        self.assertFalse(out["announce"])
        self.assertEqual(out["assigned"], [{"flow_id": FLOW, "asset_code": "NB-004", "already_assigned": False,
                                            "matches_suggestion": True,
                                            "placeholder_custodian": "hrm-E2026101"}])
        (post,) = self.assign_posts()
        self.assertEqual(post[3]["custodian_display_name"], "王小明")
        self.assertEqual(self.adm.audit[0]["action"], "person.placeholder_created")
        self.assertEqual(self.adm.audit[0]["actor"], "souei")
        (event,) = self.ctl.events
        self.assertEqual(event[2]["asset_code"], "NB-004")
        self.assertIn("保管人帳號 hrm-E2026101 是自動建立、尚未啟用的佔位帳號", event[2]["note"])
        state = self.state("agent-adm", "souei", "assign-laptop.json")
        self.assertEqual(state["flows"][FLOW]["placeholder_username"], "hrm-E2026101")
        self.assertEqual(state["flows"][FLOW]["status"], "reported")

    def test_placeholder_note_kept_when_rerun_after_event_failure(self):
        """指派成功但回報 controller 失敗：重跑走「已指派」路徑，note 仍要註明佔位帳號。"""
        self.ctl.add_onboarding(FLOW, employee_id="99999999-0000-4000-8000-000000000000")
        self.ctl.faults[("POST", EVENTS)] = [503]
        code, out = self.assign()
        self.assertEqual(code, 1, out)
        self.assertEqual(self.ctl.events, [])
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["assigned"][0]["already_assigned"])
        self.assertEqual(out["assigned"][0]["placeholder_custodian"], "hrm-E2026101")
        self.assertIn("hrm-E2026101", self.ctl.events[0][2]["note"])
        self.assertEqual(len(self.assign_posts()), 1, "重跑不再指派")
        self.assertEqual([a["action"] for a in self.adm.audit].count("person.placeholder_created"), 1)

    def test_placeholder_note_combines_with_alternative_note(self):
        self.asset("NB-004")["custodian"] = 60
        self.ctl.add_onboarding(FLOW, employee_id="99999999-0000-4000-8000-000000000000")
        _code, out = self.assign()
        self.assertEqual(out["assigned"][0]["asset_code"], "NB-005")
        note = self.ctl.events[0][2]["note"]
        self.assertIn("改指派 NB-005", note)
        self.assertIn("hrm-E2026101", note)

    def test_new_hire_not_linked_and_no_name_needs_human(self):
        flow = self.ctl.add_onboarding(FLOW, employee_id="99999999-0000-4000-8000-000000000000")
        flow["state"]["input"]["name"] = "  "
        code, out = self.assign()
        self.assertEqual(out["outcome"], "needs_human")
        self.assertTrue(out["announce"])
        self.assertIn("流程沒有新人姓名", out["needs_human"][0]["reason"])
        (post,) = self.assign_posts()
        self.assertEqual(post[3], {"custodian_hrm_employee_id": "99999999-0000-4000-8000-000000000000"})
        self.assertEqual(self.ctl.events, [])

    def test_adm_still_rejects_named_new_hire_needs_human(self):
        """帶了姓名 ADM 仍回 400（例如 HRM ID 連到停用帳號）：交給人，不換別台。"""
        self.ctl.add_onboarding(FLOW, employee_id="99999999-0000-4000-8000-000000000000")
        self.adm.faults[("POST", "/api/v1/assets/NB-004/assign/")] = [400]
        _code, out = self.assign()
        self.assertEqual(out["outcome"], "needs_human")
        self.assertIn("連到停用的帳號", out["needs_human"][0]["reason"])
        self.assertEqual(len(self.assign_posts()), 1, "400 之後不再換別台")

    def test_lost_event_response_then_rerun_is_idempotent(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        self.ctl.faults[("POST", EVENTS)] = ["drop_after"]
        code, out = self.assign()
        self.assertEqual(code, 1)
        self.assertFalse(out["announce"])
        self.assertEqual(self.ctl.flows[FLOW]["current_step"], "book_room", "controller 其實收下了")
        state = self.state("agent-adm", "souei", "assign-laptop.json")
        self.assertEqual(state["flows"][FLOW]["status"], "assigned")
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["skipped"][0]["reason"], "already_reported")
        self.assertEqual(len(self.assign_posts()), 1)
        self.assertEqual(len(self.ctl.events), 1)
        self.assertTrue(self.task_read())

    def test_rerun_after_success_does_nothing(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        self.assign()
        code, out = self.assign()
        self.assertEqual(out["outcome"], "nothing_to_do")
        code, out = self.assign("souei", "--flow", FLOW)
        self.assertEqual(out["skipped"][0]["reason"], "already_reported")
        self.assertEqual(len(self.assign_posts()), 1)
        self.assertEqual(len(self.ctl.events), 1)

    def test_flow_option_without_inbox_task(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE, notify=False)
        code, out = self.assign("souei", "--flow", FLOW)
        self.assertEqual(out["outcome"], "assigned", out)
        self.assertEqual(self.ctl.events[0][2]["asset_code"], "NB-004")

    def test_not_waiting_yet_is_skipped(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE, current_step="draft_laptop")
        code, out = self.assign()
        self.assertEqual(out["skipped"][0]["reason"], "not_waiting")
        self.assertEqual(self.adm.calls, [])
        self.assertFalse(self.task_read())

    def test_not_staff_is_announced(self):
        self.adm.users["souei"]["staff"] = False
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        code, out = self.assign()
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "forbidden")
        self.assertTrue(out["announce"])
        self.assertEqual(self.ctl.events, [])

    def test_controller_rejects_reporter(self):
        # 流程的 IT 人員不是 souei（例如 Identity 改過）：controller 回 403 not_reporter
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        self.ctl.flows[FLOW]["_it_agent"] = "someone-else"
        code, out = self.assign()
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "error")
        self.assertTrue(out["announce"])
        self.assertIn("not_reporter", out["errors"][0]["error"])

    def test_non_it_agent_does_nothing(self):
        code, out = self.assign("gobta")
        self.assertEqual(out["outcome"], "not_it")
        self.assertEqual(self.ctl.calls, [])

    def test_non_it_agent_with_flow_option_does_nothing(self):
        # --flow 不能繞過 IT 角色檢查：非 IT 的員工 agent 就算是 ADM staff 也不指派
        self.adm.users["gobta"]["staff"] = True
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        code, out = self.assign("gobta", "--flow", FLOW)
        self.assertEqual(out["outcome"], "not_it")
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.ctl.calls, [])
        self.assertIsNone(self.asset("NB-004")["custodian"])

    def test_cancelled_flow_still_at_await_laptop_is_not_assigned(self):
        # 流程被取消時 current_step 仍停在 await_laptop：不得指派，只把任務標成已讀
        flow = self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        flow["status"] = "cancelled"
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["skipped"][0]["reason"], "flow_cancelled")
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.ctl.events, [])
        self.assertTrue(self.task_read())

    def test_inactive_laptop_held_by_new_hire_is_not_counted(self):
        # 新人名下只有一台停用的舊筆電：不算已配發，照常指派建議的那台
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        old = self.asset("NB-002")
        old["custodian"], old["is_active"] = 50, False
        code, out = self.assign()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["assigned"][0]["asset_code"], "NB-004")
        self.assertFalse(out["assigned"][0]["already_assigned"])
        self.assertEqual(self.asset("NB-004")["custodian"], 50)

    def test_empty_inbox(self):
        code, out = self.assign()
        self.assertEqual(out["outcome"], "nothing_to_do")
        self.assertEqual(self.adm.calls, [])

    def test_dry_run_no_network(self):
        self.ctl.add_onboarding(FLOW, employee_id=NEW_HIRE)
        code, out = self.assign("souei", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out["outcome"], "would_assign")
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.ctl.calls, [])


if __name__ == "__main__":
    unittest.main()
