"""agent-crm（crm.py）的測試：假 CRM、假 ADM、假 controller、假 openclaw。"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from fakes import FakeAdm, FakeController, FakeCrm
from harness import SCRIPTS, SkillTestCase, argv_value

GOBTA_HRM = "3c75cb71-5e9c-4cc7-8bce-dcc79817299f"
MJOLMIRE_HRM = "da1b72d2-89cb-4879-9c50-73e03a1ae993"
MONDAY = "2026-10-12"
MORNING = "2026-10-12T08:20:00+08:00"


def customers(n=5):
    return [{"id": f"c0000000-0000-4000-8000-00000000000{i}", "name": f"客戶{i}",
             "external_customer_id": f"EXT-{i}", "status": "active"} for i in range(1, n + 1)]


class CrmTestBase(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.crm = FakeCrm(
            {"gobta": {"password": "pw", "id": 7, "hrm": GOBTA_HRM},
             "mjolmire-businessman": {"password": "pw", "id": 8, "hrm": MJOLMIRE_HRM}},
            customers(),
        )
        # gobta 已經有一筆自己負責的一般待辦：用來推得自己的 CRM user id
        self.crm.followups.append({
            "id": "f0000000-0000-4000-8000-000000000999", "customer": customers()[0]["id"],
            "title": "舊待辦", "kind": "general", "due_at": "2026-10-01T10:00:00+08:00",
            "status": "done", "assignee": 7, "assignee_hrm_employee_id": GOBTA_HRM,
        })
        self.adm = FakeAdm({"gobta": {"password": "apw", "id": 3}}, [],
                           [{"id": 1, "code": "CAR-1", "plate_number": "ABC-1234", "seats": 5,
                             "odometer": 12000}])
        self.ctl = FakeController({"tok-gobta": "gobta", "tok-mj": "mjolmire-businessman"})
        self.env.update({
            "CRM_API_BASE": f"{self.crm.url}/api",
            "CRM_AGENT_PASSWORD": "pw",
            "ADM_API_BASE": f"{self.adm.url}/api/v1",
            "ADM_AGENT_PASSWORD": "apw",
            "FLOW_CONTROLLER_URL": self.ctl.url,
            "FLOW_CONTROLLER_TOKEN_GOBTA": "tok-gobta",
            "FLOW_CONTROLLER_TOKEN_MJOLMIRE_BUSINESSMAN": "tok-mj",
        })

    def tearDown(self):
        for server in (self.crm, self.adm, self.ctl):
            server.stop()
        super().tearDown()

    def visits(self):
        return [f for f in self.crm.followups if f["kind"] == "visit"]


class DecideTests(CrmTestBase):
    def decide(self, *extra, agent="gobta", now=MORNING, date=MONDAY):
        return self.run_script("crm", ["decide", "--agent", agent, "--date", date, "--now", now, *extra])

    def test_hit_creates_visit_and_schedules_report(self):
        self.set_rng(0.10)  # < 30%
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "visit_scheduled")
        visits = self.visits()
        self.assertEqual(len(visits), 1)
        visit = visits[0]
        self.assertEqual(visit["assignee"], 7)
        self.assertTrue(visit["title"].startswith("[拜訪] 客戶"))
        self.assertIn("地點：", visit["title"])
        self.assertIn("人數：", visit["title"])
        self.assertIn(visit["due_at"][:10], ("2026-10-13", "2026-10-14", "2026-10-15"))

        calls = self.cron_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertIn("--no-deliver", argv)
        self.assertEqual(argv_value(argv, "--agent"), "gobta")
        self.assertEqual(argv_value(argv, "--declaration-key"),
                         f"agent-crm:report-trip:gobta:{visit['due_at'][:10]}T"
                         f"{visit['due_at'][11:13]}{visit['due_at'][14:16]}")
        at = argv_value(argv, "--at")
        self.assertEqual(at[:10], visit["due_at"][:10])
        # 要在拜訪（＝公務車預約）結束前 10～20 分鐘回報，ADM 才收得下取車登記
        from datetime import datetime, timedelta
        due = datetime.fromisoformat(visit["due_at"])
        end = due + timedelta(minutes=out["duration_minutes"])
        report_at = datetime.fromisoformat(at)
        self.assertTrue(end - timedelta(minutes=20) <= report_at <= end - timedelta(minutes=10),
                        (at, visit["due_at"], out["duration_minutes"]))
        self.assertLess(at[11:16], "17:50")
        self.assertIn("report-trip --agent gobta", argv_value(argv, "--message"))
        self.assertFalse(out["announce"])

    def test_rerun_does_not_reroll_or_duplicate(self):
        self.set_rng(0.10)
        self.decide()
        code, out = self.decide()
        self.assertEqual(code, 0)
        self.assertEqual(out["outcome"], "already_decided")
        self.assertEqual(len(self.visits()), 1)
        self.assertEqual(len(self.cron_calls()), 1)

    def test_miss_does_nothing(self):
        self.set_rng(0.31)
        code, out = self.decide()
        self.assertEqual(out["outcome"], "no_visit")
        self.assertEqual(self.visits(), [])
        self.assertEqual(self.cron_calls(), [])
        # 重跑也維持不排
        self.set_rng(0.01)
        _, out = self.decide()
        self.assertEqual(out["outcome"], "already_decided")
        self.assertEqual(self.visits(), [])

    def test_non_sales_and_weekend(self):
        _, out = self.decide(agent="main")
        self.assertEqual(out["outcome"], "not_sales")
        _, out = self.decide(date="2026-10-10", now="2026-10-10T08:20:00+08:00")
        self.assertEqual(out["outcome"], "not_working_day")
        self.assertEqual(self.crm.calls, [])

    def test_lost_response_is_not_duplicated(self):
        """建立待辦的回應遺失（伺服器其實建好了），重跑要沿用，不能建第二筆。"""
        self.set_rng(0.10)
        self.crm.faults[("POST", "/api/v1/follow-ups/")] = ["drop_after"]
        code, out = self.decide()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.visits()), 1)
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "visit_scheduled")
        self.assertEqual(len(self.visits()), 1)
        self.assertEqual(len(self.cron_calls()), 1)

    def test_schedule_failure_is_announced_and_retried(self):
        self.set_rng(0.10)
        code, out = self.run_script("crm", ["decide", "--agent", "gobta", "--date", MONDAY,
                                            "--now", MORNING], {"FAKE_OPENCLAW_FAIL": "1"})
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "schedule_failed")
        self.assertTrue(out["announce"])
        # 重跑時補排，不會再建待辦
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "visit_scheduled")
        self.assertEqual(len(self.visits()), 1)

    def test_user_id_from_env_when_no_followups(self):
        self.crm.followups.clear()
        code, out = self.decide("--force", "visit")
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertIn("CRM_USER_ID_GOBTA", out["error"])
        code, out = self.run_script("crm", ["decide", "--agent", "gobta", "--date", MONDAY,
                                            "--now", MORNING], {"CRM_USER_ID_GOBTA": "7"})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.visits()[0]["assignee"], 7)

    def test_wrong_password_is_announced(self):
        code, out = self.run_script("crm", ["decide", "--agent", "gobta", "--force", "visit",
                                            "--date", MONDAY, "--now", MORNING],
                                    {"CRM_AGENT_PASSWORD": "wrong"})
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "login_failed")
        self.assertTrue(out["announce"])

    def test_crm_down_is_quiet_error(self):
        self.crm.stop()
        code, out = self.run_script("crm", ["decide", "--agent", "gobta", "--force", "visit",
                                            "--date", MONDAY, "--now", MORNING],
                                    {"CRM_API_BASE": "http://127.0.0.1:9/api"})
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "error")
        self.assertFalse(out["announce"])
        self.crm = FakeCrm({}, [])  # tearDown 用


class PlanVisitTests(CrmTestBase):
    """cs-agent 08:00 只掃「明天」的拜訪：08:00 以後排的拜訪至少要 2 個工作天後。"""

    class LowRng:
        def randint(self, a, b):
            return a

        def choice(self, seq):
            return seq[0]

    def plan(self, now):
        from unittest import mock
        from datetime import date
        crm = __import__("harness").load("crm")
        with mock.patch.object(self.c, "RNG", self.LowRng()):
            return crm.plan_visit(date(2026, 10, 12), self.c.parse_dt(now))

    def test_before_scan_can_be_tomorrow(self):
        self.assertEqual(self.plan("2026-10-12T07:40:00+08:00")["visit_date"], "2026-10-13")

    def test_after_scan_is_at_least_two_workdays(self):
        self.assertEqual(self.plan("2026-10-12T08:20:00+08:00")["visit_date"], "2026-10-14")

    def test_visit_tomorrow_after_scan_warns(self):
        code, out = self.run_script("crm", ["visit", "--agent", "gobta", "--days", "1", "--time", "14:00",
                                            "--date", MONDAY, "--now", MORNING])
        self.assertEqual(code, 0, out)
        self.assertIn("cs-agent", out.get("note", ""))


class VisitTests(CrmTestBase):
    def test_visit_with_customer_and_days_is_idempotent(self):
        argv = ["visit", "--agent", "gobta", "--customer", "客戶3", "--days", "2", "--time", "14:00",
                "--date", MONDAY, "--now", MORNING]
        code, out = self.run_script("crm", argv)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "visit_scheduled")
        self.assertEqual(out["due_at"], "2026-10-14T14:00:00+08:00")
        self.assertEqual(out["customer"], "客戶3")
        first = out
        self.set_rng(0.5)  # 重跑時就算重新抽到不同的人數、時長，也要以既有待辦為準
        code, out = self.run_script("crm", argv)
        self.assertEqual(out["outcome"], "visit_exists")
        self.assertEqual(len(self.visits()), 1)
        self.assertEqual((out["seats"], out["duration_minutes"]),
                         (first["seats"], first["duration_minutes"]))

    def test_visit_today_in_the_past_is_rejected(self):
        code, out = self.run_script("crm", ["visit", "--agent", "gobta", "--days", "0", "--time", "08:00",
                                            "--date", MONDAY, "--now", MORNING])
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "invalid_time")


class ReportTripTests(CrmTestBase):
    def make_flow(self, flow_id="flow-1", *, visit_at="2026-10-12T10:00:00+08:00", step_kind="wait",
                  sales=GOBTA_HRM, sales_agent="gobta", viewers=("gobta",), followup_id=None):
        self.ctl.flows[flow_id] = {
            "id": flow_id, "type": "visit", "status": "running", "title": "gobta 10/12 拜訪客戶1",
            "current_step": "trip" if step_kind == "wait" else "confirm_dispatch",
            "steps": [{"name": "confirm_dispatch", "kind": "approval"},
                      {"name": "trip", "kind": "wait"}],
            "state": {"input": {"follow_up_id": followup_id, "visit_at": visit_at,
                                "sales_hrm_employee_id": sales}},
            "_sales_agent": sales_agent,
        }
        self.ctl.flow_viewers[flow_id] = set(viewers)
        self.ctl.notifications.setdefault(sales_agent, []).append({
            "id": len(self.ctl.notifications.get(sales_agent, [])) + 1, "kind": "info",
            "title": "派車完成", "flow_id": flow_id, "approval_id": None, "link": None,
            "created_at": "2026-10-11T01:00:00+00:00",
        })
        self.adm.vehicle_bookings.append({
            "id": 50, "vehicle": 1, "start_at": visit_at, "end_at": visit_at, "status": "active",
            "booked_by_username": "admin-agent", "booked_for_username": "gobta", "pickup_odometer": None,
            "_key": "k", "_digest": "d",
        })

    def report(self, *extra, now="2026-10-12T17:05:00+08:00"):
        return self.run_script("crm", ["report-trip", "--agent", "gobta", "--date", MONDAY,
                                       "--now", now, *extra])

    def test_reports_mileage_once(self):
        self.make_flow()
        code, out = self.report()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "trip_reported")
        self.assertEqual(len(self.ctl.events), 1)
        flow_id, agent, body = self.ctl.events[0]
        self.assertEqual((flow_id, agent, body["type"]), ("flow-1", "gobta", "trip_report"))
        self.assertEqual(body["pickup_odometer"], 12000)
        self.assertTrue(10 <= body["return_odometer"] - body["pickup_odometer"] <= 80)
        self.assertIsInstance(body["note"], str)
        self.assertFalse(out["announce"])

        code, out = self.report()
        self.assertEqual(out["outcome"], "no_pending_trip")
        self.assertEqual(len(self.ctl.events), 1)

    def test_uses_own_visit_location_for_distance(self):
        self.set_rng(0.10)
        self.run_script("crm", ["decide", "--agent", "gobta", "--date", MONDAY, "--now", MORNING])
        visit = self.visits()[0]
        visit_day = visit["due_at"][:10]
        self.make_flow(visit_at=visit["due_at"], followup_id=visit["id"])
        code, out = self.run_script("crm", ["report-trip", "--agent", "gobta", "--date", visit_day,
                                            "--now", f"{visit_day}T17:20:00+08:00"])
        self.assertEqual(out["outcome"], "trip_reported", out)
        body = self.ctl.events[0][2]
        self.assertIn("客戶", body["note"])
        index = self.state("agent-crm", "gobta", "visits.json")
        km = index[visit["id"]]["one_way_km"] * 2
        self.assertTrue(abs((body["return_odometer"] - body["pickup_odometer"]) - km) <= 6)

    def test_skips_future_and_unfinished_visits(self):
        self.make_flow("flow-tomorrow", visit_at="2026-10-13T10:00:00+08:00")
        self.make_flow("flow-later", visit_at="2026-10-12T15:00:00+08:00")
        code, out = self.report(now="2026-10-12T15:30:00+08:00")
        self.assertEqual(out["outcome"], "no_pending_trip")
        reasons = {s["flow_id"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(reasons["flow-tomorrow"], "visit_not_today")
        self.assertEqual(reasons["flow-later"], "visit_not_finished")
        self.assertEqual(self.ctl.events, [])

    def test_skips_flow_not_waiting_and_other_sales(self):
        self.make_flow("flow-approval", step_kind="approval")
        self.make_flow("flow-other", sales=MJOLMIRE_HRM)
        code, out = self.report()
        reasons = {s["flow_id"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(reasons["flow-approval"], "not_waiting_trip_report")
        self.assertEqual(reasons["flow-other"], "not_my_visit")
        self.assertEqual(self.ctl.events, [])

    def test_falls_back_to_inbox_state_when_flow_not_viewable(self):
        """controller 不讓員工 token 讀流程（403）時，用 agent-inbox 記下的確認頁內容判斷日期。"""
        self.make_flow(viewers=())
        self.ctl.notifications["gobta"].clear()
        path = os.path.join(self.state_dir, "agent-inbox", "gobta")
        os.makedirs(path)
        import json
        with open(os.path.join(path, "handled.json"), "w", encoding="utf-8") as fh:
            json.dump({"approvals": {"ap-1": {"flow_id": "flow-1", "status": "decided",
                                              "context": {"時段": "2026-10-12 10:00～11:30",
                                                          "車輛": "ABC-1234"}}}}, fh)
        code, out = self.report()
        self.assertEqual(out["outcome"], "trip_reported", out)
        self.assertEqual(self.ctl.events[0][2]["pickup_odometer"], 12000)

    def controller_style_flow(self, flow_id="flow-c", *, viewers=("gobta",)):
        """流程 ① 工作包（PR #4）的 flow state 形狀：input.visit_start_at／visit_end_at、parties.sales。"""
        self.make_flow(flow_id, viewers=viewers)
        self.ctl.flows[flow_id]["state"] = {
            "input": {"follow_up_id": 123, "visit_start_at": "2026-10-12T10:00:00+08:00",
                      "visit_end_at": "2026-10-12T12:00:00+08:00", "location": "台中",
                      "assignee_hrm_employee_id": GOBTA_HRM},
            "parties": {"sales": {"hrm_employee_id": GOBTA_HRM, "agent_id": "gobta"},
                        "manager": {"hrm_employee_id": "85f9387a-6a8f-4283-92dc-71ec63de4447"}},
            "steps": {"draft_vehicle": {"output": {"start_at": "2026-10-12T10:00:00+08:00",
                                                   "end_at": "2026-10-12T12:00:00+08:00"}}},
        }

    def test_reports_before_booking_window_ends(self):
        """ADM 只在預約結束前收取車登記：結束前 30 分鐘內就要回報，不能等到傍晚。"""
        self.controller_style_flow()
        code, out = self.report(now="2026-10-12T11:00:00+08:00")
        self.assertEqual(out["outcome"], "no_pending_trip", out)
        self.assertEqual(out["skipped"][0]["reason"], "visit_not_finished")
        code, out = self.report(now="2026-10-12T11:45:00+08:00")
        self.assertEqual(out["outcome"], "trip_reported", out)

    def test_context_window_text_without_year(self):
        """看不到流程詳情時，用派車確認頁 context 的「10/12 10:00–12:00」判斷時段。"""
        self.controller_style_flow(viewers=())
        self.ctl.notifications["gobta"].clear()
        path = os.path.join(self.state_dir, "agent-inbox", "gobta")
        os.makedirs(path)
        import json
        with open(os.path.join(path, "handled.json"), "w", encoding="utf-8") as fh:
            json.dump({"approvals": {"ap-1": {"flow_id": "flow-c", "status": "decided",
                                              "flow_title": "10/12 拜訪 客戶1",
                                              "context": {"visit": "10/12 10:00–12:00",
                                                          "deadline": "10/11 18:00"}}}}, fh)
        code, out = self.report(now="2026-10-12T11:00:00+08:00")
        self.assertEqual(out["skipped"][0]["reason"], "visit_not_finished", out)
        code, out = self.report(now="2026-10-12T11:40:00+08:00")
        self.assertEqual(out["outcome"], "trip_reported", out)

    def test_explicit_flow_and_pickup(self):
        self.make_flow(visit_at="2026-10-14T10:00:00+08:00", viewers=())
        code, out = self.report("--flow", "flow-1", "--pickup-odometer", "5000", "--distance", "42")
        self.assertEqual(out["outcome"], "trip_reported", out)
        body = self.ctl.events[0][2]
        self.assertEqual((body["pickup_odometer"], body["return_odometer"]), (5000, 5042))

    def test_odometer_unknown_is_announced(self):
        self.make_flow()
        self.adm.vehicle_bookings.clear()
        code, out = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "report_failed")
        self.assertTrue(out["announce"])

    def test_lost_event_response_keeps_same_numbers(self):
        self.make_flow()
        self.ctl.faults[("POST", "/api/flows/flow-1/events")] = [503]
        code, out = self.report()
        self.assertEqual(code, 1)
        planned = self.state("agent-crm", "gobta", "trips.json")["flow-1"]
        code, out = self.report()
        self.assertEqual(out["outcome"], "trip_reported", out)
        body = self.ctl.events[0][2]
        self.assertEqual(body["return_odometer"], planned["return_odometer"])

    def test_missing_controller_token(self):
        self.env.pop("FLOW_CONTROLLER_TOKEN_GOBTA")
        code, out = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")


class DryRunOfflineTests(SkillTestCase):
    """--dry-run 在沒有網路的情況下也能跑：所有網址都指向不存在的埠，狀態檔也不寫。"""

    def test_all_commands_offline(self):
        env = dict(os.environ)
        env.update(self.env)
        env.update({
            "CRM_API_BASE": "http://127.0.0.1:9/api", "ADM_API_BASE": "http://127.0.0.1:9/api/v1",
            "FLOW_CONTROLLER_URL": "http://127.0.0.1:9",
        })
        for key in list(env):
            if key.startswith(("CRM_AGENT", "ADM_AGENT", "FLOW_CONTROLLER_TOKEN")):
                env.pop(key)
        commands = [
            ("crm", ["decide", "--agent", "gobta", "--force", "visit", "--date", MONDAY, "--now", MORNING]),
            ("crm", ["visit", "--agent", "gobta", "--date", MONDAY, "--now", MORNING]),
            ("crm", ["report-trip", "--agent", "gobta"]),
            ("crm", ["status", "--agent", "gobta"]),
            ("adm", ["decide", "--agent", "luminous", "--force", "both", "--date", MONDAY, "--now", MORNING]),
            ("adm", ["book-room", "--agent", "souei", "--date", MONDAY, "--now", MORNING]),
            ("adm", ["status", "--agent", "souei"]),
            ("inbox", ["check", "--agent", "main", "--simulate", "expense", "--amount", "800"]),
        ]
        import json
        for name, argv in commands:
            proc = subprocess.run([sys.executable, SCRIPTS[name], *argv, "--dry-run"],
                                  capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(proc.returncode, 0, f"{name} {argv}: {proc.stdout} {proc.stderr}")
            out = json.loads(proc.stdout)
            self.assertTrue(out["ok"])
            self.assertFalse(out["announce"])
        self.assertFalse(os.path.exists(self.state_dir), "dry-run 不應寫狀態檔")
        self.assertEqual(self.cron_calls(), [])


if __name__ == "__main__":
    unittest.main()
