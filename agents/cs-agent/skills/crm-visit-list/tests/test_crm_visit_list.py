"""crm-visit-list 的測試：假 CRM＋假 controller＋假 flow-start。

執行：python3 -m unittest discover -s agents/cs-agent/skills/crm-visit-list/tests -v
"""

from __future__ import annotations

import filecmp
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_services import SkillTestCase  # noqa: E402

SKILL_DIR = os.path.dirname(HERE)
SKILLS_DIR = os.path.dirname(SKILL_DIR)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(SKILLS_DIR)))
SCRIPT = os.path.join(SKILL_DIR, "scripts", "crm_visit_list.py")

FRIDAY_8AM = "2026-10-09T08:00:00+08:00"
TUESDAY_8AM = "2026-10-13T08:00:00+08:00"
HRM = "da1b72d2-89cb-4879-9c50-73e03a1ae993"


def title(customer="客戶 C", location="台中 客戶 C 工廠", seats=2, minutes=120, purpose="新品試吃"):
    return f"[拜訪] {customer}｜地點：{location}｜人數：{seats} 人｜時長：{minutes} 分鐘｜目的：{purpose}"


class VisitListTest(SkillTestCase):
    SCRIPT = SCRIPT
    MODULE = "crm_visit_list"

    def setUp(self):
        super().setUp()
        self.cust = self.crm.add_customer("客戶 C", "C-003")

    def add(self, due, **kw):
        hrm = kw.pop("hrm", HRM)
        kind = kw.pop("kind", "visit")
        status = kw.pop("status", "pending")
        raw = kw.pop("raw_title", None)
        return self.crm.add_followup(self.cust, raw if raw is not None else title(**kw), due,
                                     kind=kind, status=status, hrm=hrm)

    def flows_posted(self):
        return self.ctl.count("POST", "/api/flows")

    # --- 期間 --------------------------------------------------------------

    def test_friday_scans_through_monday(self):
        monday = self.add("2026-10-12T10:00:00+08:00")
        self.add("2026-10-09T15:00:00+08:00")                       # 今天（週五）：不算
        self.add("2026-10-13T10:00:00+08:00")                       # 週二：不算
        self.add("2026-10-12T11:00:00+08:00", kind="general")       # 不是拜訪
        self.add("2026-10-12T14:00:00+08:00", status="done")        # 已完成
        code, out = self.run_script("--now", FRIDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "flows_started")
        self.assertFalse(out["announce"])
        self.assertEqual(out["target_date"], "2026-10-12")
        self.assertEqual(out["window"], {"due_after": "2026-10-10T00:00:00+08:00",
                                         "due_before": "2026-10-13T00:00:00+08:00"})
        self.assertEqual([s["follow_up_id"] for s in out["started"]], [monday])
        flow = next(iter(self.ctl.flows.values()))
        self.assertEqual(flow["type"], "visit")
        self.assertEqual(flow["input"], {
            "follow_up_id": monday,
            "customer": {"name": "客戶 C", "external_id": "C-003"},
            "visit_start_at": "2026-10-12T10:00:00+08:00",
            "visit_end_at": "2026-10-12T12:00:00+08:00",
            "location": "台中 客戶 C 工廠",
            "seats": 2,
            "assignee_hrm_employee_id": HRM,
            "subject": "新品試吃",
        })
        # 請求帶 controller 要的 Bearer token；查詢 CRM 用 kind／status／日期篩選
        query = [c for c in self.crm.calls if c[1] == "/api/v1/follow-ups/"][0][2]
        self.assertEqual(query["kind"], "visit")
        self.assertEqual(query["status"], "pending")

    def test_friday_also_covers_weekend_visits(self):
        saturday = self.add("2026-10-10T10:00:00+08:00")
        code, out = self.run_script("--now", FRIDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertEqual([s["follow_up_id"] for s in out["started"]], [saturday])

    def test_weekday_scans_tomorrow_only(self):
        wed = self.add("2026-10-14T14:30:00+08:00", minutes=60)
        self.add("2026-10-15T10:00:00+08:00")
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["target_date"], "2026-10-14")
        self.assertEqual([s["follow_up_id"] for s in out["started"]], [wed])
        flow = next(iter(self.ctl.flows.values()))
        self.assertEqual(flow["input"]["visit_end_at"], "2026-10-14T15:30:00+08:00")

    def test_explicit_date(self):
        thu = self.add("2026-10-15T10:00:00+08:00")
        code, out = self.run_script("--now", TUESDAY_8AM, "--date", "2026-10-15")
        self.assertEqual(code, 0, out)
        self.assertEqual([s["follow_up_id"] for s in out["started"]], [thu])

    # --- 課堂示範 ----------------------------------------------------------

    def test_demo_starts_only_given_follow_up_with_demo_flag(self):
        demo = self.add("2026-10-13T10:20:00+08:00", minutes=60)
        other = self.add("2026-10-13T14:00:00+08:00")          # 同一天的正式拜訪：不碰
        code, out = self.run_script("--now", TUESDAY_8AM, "--date", "2026-10-13", "--demo",
                                    "--follow-up", str(demo))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "flows_started")
        self.assertTrue(out["demo"])
        self.assertEqual([s["follow_up_id"] for s in out["started"]], [demo])
        self.assertNotIn(other, [f["input"]["follow_up_id"] for f in self.ctl.flows.values()])
        flow = next(iter(self.ctl.flows.values()))
        self.assertIs(flow["input"]["demo"], True)
        self.assertEqual(flow["input"]["visit_end_at"], "2026-10-13T11:20:00+08:00")

    def test_without_demo_flag_input_has_no_demo(self):
        self.add("2026-10-14T10:00:00+08:00")
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertNotIn("demo", next(iter(self.ctl.flows.values()))["input"])

    def test_demo_follow_up_not_found(self):
        self.add("2026-10-13T10:20:00+08:00")
        code, out = self.run_script("--now", TUESDAY_8AM, "--date", "2026-10-13", "--demo",
                                    "--follow-up", "no-such-id")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "no_visits")
        self.assertIn("no-such-id", out["detail"])
        self.assertEqual(self.flows_posted(), 0)

    def test_demo_dry_run(self):
        code, out = self.run_script("--now", TUESDAY_8AM, "--date", "2026-10-13", "--demo",
                                    "--follow-up", "abc", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_scan")
        self.assertIn("示範模式", out["detail"])

    # --- 安靜結束 ----------------------------------------------------------

    def test_no_visits_is_silent(self):
        self.add("2026-10-15T10:00:00+08:00")
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "no_visits")
        self.assertFalse(out["announce"])
        self.assertEqual(self.flows_posted(), 0)

    # --- 解析失敗 ----------------------------------------------------------

    def test_bad_title_is_skipped_and_reported(self):
        good = self.add("2026-10-14T10:00:00+08:00")
        bad = self.add("2026-10-14T11:00:00+08:00", raw_title="拜訪客戶 C，記得帶樣品")
        no_loc = self.add("2026-10-14T13:00:00+08:00",
                          raw_title="[拜訪] 客戶 C｜人數：2 人｜時長：60 分鐘｜目的：x")
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "needs_attention")
        self.assertTrue(out["announce"])
        self.assertEqual([s["follow_up_id"] for s in out["started"]], [good])
        reasons = {s["follow_up_id"]: s["reason"] for s in out["skipped"]}
        self.assertIn("[拜訪]", reasons[bad])
        self.assertIn("地點", reasons[no_loc])
        self.assertEqual(self.flows_posted(), 1)

    def test_missing_assignee_is_skipped(self):
        fid = self.add("2026-10-14T10:00:00+08:00", hrm=None)
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["announce"])
        self.assertEqual(out["skipped"][0]["follow_up_id"], fid)
        self.assertIn("負責業務", out["skipped"][0]["reason"])
        self.assertEqual(self.flows_posted(), 0)

    # --- 重跑 --------------------------------------------------------------

    def test_rerun_does_not_start_twice(self):
        self.add("2026-10-14T10:00:00+08:00")
        self.add("2026-10-14T14:00:00+08:00", customer="客戶 C", purpose="續約")
        code, first = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, first)
        self.assertEqual(len(first["started"]), 2)
        code, second = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, second)
        self.assertEqual(second["outcome"], "already_started")
        self.assertFalse(second["announce"])
        self.assertEqual(second["started"], [])
        self.assertEqual(sorted(e["flow_id"] for e in second["existing"]),
                         sorted(s["flow_id"] for s in first["started"]))
        self.assertEqual(len(self.ctl.flows), 2)
        # 去重鍵交給 controller；呼叫端仍帶 crm:follow-up:<id>
        bodies = [c[3] for c in self.ctl.calls if c[1] == "/api/flows"]
        self.assertTrue(all(b["dedupe_key"] == f"crm:follow-up:{b['input']['follow_up_id']}" for b in bodies))

    # --- 其他 --------------------------------------------------------------

    def test_paging_and_customer_fallback(self):
        ids = [self.add(f"2026-10-14T1{h}:00:00+08:00") for h in range(3)]
        ghost = self.crm.add_followup("no-such-customer", title(customer="客戶 Z"),
                                      "2026-10-14T15:00:00+08:00", hrm=HRM)
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertEqual([s["follow_up_id"] for s in out["started"]], ids + [ghost])
        names = {f["input"]["follow_up_id"]: f["input"]["customer"] for f in self.ctl.flows.values()}
        self.assertEqual(names[ghost], {"name": "客戶 Z", "external_id": None})

    def test_crm_errors_are_retried(self):
        self.add("2026-10-14T10:00:00+08:00")
        self.crm.faults[("GET", "/api/v1/follow-ups/")] = [502, "drop"]
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out["started"]), 1)
        self.assertEqual(self.crm.count("GET", "/api/v1/follow-ups/"), 3)

    def test_flow_start_failure_is_reported(self):
        self.add("2026-10-14T10:00:00+08:00")
        self.ctl.faults[("POST", "/api/flows")] = [500]
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "needs_attention")
        self.assertTrue(out["announce"])
        self.assertEqual(len(out["failed"]), 1)

    def test_crm_unreachable_is_quiet_error(self):
        os.environ["CRM_API_BASE"] = "http://127.0.0.1:9/api"
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 1)
        self.assertFalse(out["announce"])

    def test_missing_token_is_config_error(self):
        os.environ.pop("CRM_API_TOKEN")
        code, out = self.run_script("--now", TUESDAY_8AM)
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertTrue(out["announce"])

    def test_dry_run_does_not_touch_network(self):
        os.environ.pop("CRM_API_TOKEN")
        code, out = self.run_script("--now", FRIDAY_8AM, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "would_scan")
        self.assertEqual(out["target_date"], "2026-10-12")
        self.assertEqual(self.crm.calls, [])
        self.assertEqual(self.ctl.calls, [])


class ParseTitleTest(SkillTestCase):
    SCRIPT = SCRIPT
    MODULE = "crm_visit_list"

    def setUp(self):
        self.cs = sys.modules["cs_common"]

    def test_agent_crm_format(self):
        parsed = self.cs.parse_visit_title(
            "[拜訪] 好吃食品｜地點：台北市內湖區瑞光路｜人數：3 人｜時長：90 分鐘｜目的：年度供貨合約續約洽談")
        self.assertEqual(parsed, {"customer_name": "好吃食品", "location": "台北市內湖區瑞光路",
                                  "seats": 3, "duration_minutes": 90, "subject": "年度供貨合約續約洽談"})

    def test_lenient_separators_and_optional_purpose(self):
        parsed = self.cs.parse_visit_title("[拜訪] A | 地點: 桃園 | 人數: 1 | 時長: 60")
        self.assertEqual(parsed["location"], "桃園")
        self.assertEqual(parsed["seats"], 1)
        self.assertEqual(parsed["duration_minutes"], 60)
        self.assertIsNone(parsed["subject"])

    def test_invalid_titles(self):
        for bad in (None, "", "拜訪 A｜地點：x｜人數：1 人｜時長：60 分鐘",
                    "[拜訪] A｜地點：｜人數：1 人｜時長：60 分鐘",
                    "[拜訪] A｜地點：x｜人數：兩人｜時長：60 分鐘",
                    "[拜訪] A｜地點：x｜人數：0 人｜時長：60 分鐘",
                    "[拜訪] A｜地點：x｜人數：2 人"):
            with self.assertRaises(self.cs.TitleError, msg=bad):
                self.cs.parse_visit_title(bad)


class CopiesTest(unittest.TestCase):
    """cs-agent 每個 skill 的共用模組、測試用假服務都要一模一樣（部署時各 skill 目錄獨立）。"""

    def test_shared_modules_are_identical(self):
        siblings = sorted(d for d in os.listdir(SKILLS_DIR)
                          if d != os.path.basename(SKILL_DIR)
                          and os.path.isdir(os.path.join(SKILLS_DIR, d, "scripts")))
        for expected in ("crm-visit-close", "crm-intake", "crm-reply-draft", "crm-reply-log"):
            self.assertIn(expected, siblings)
        mine = os.path.join(SKILL_DIR, "scripts")
        for skill in siblings:
            other = os.path.join(SKILLS_DIR, skill, "scripts")
            for name in ("cs_common.py", "ffj_common.py"):
                self.assertTrue(filecmp.cmp(os.path.join(mine, name), os.path.join(other, name), shallow=False),
                                f"{skill}/scripts/{name} 和 crm-visit-list 的副本不一致")
            tests_other = os.path.join(SKILLS_DIR, skill, "tests", "fake_services.py")
            self.assertTrue(filecmp.cmp(os.path.join(HERE, "fake_services.py"), tests_other, shallow=False),
                            f"{skill}/tests/fake_services.py 和 crm-visit-list 的副本不一致")
        upstream = os.path.join(REPO, "skills", "agent-crm", "scripts", "ffj_common.py")
        if os.path.exists(upstream):
            self.assertTrue(filecmp.cmp(os.path.join(mine, "ffj_common.py"), upstream, shallow=False),
                            "ffj_common.py 和 skills/agent-crm 的版本不一致")


if __name__ == "__main__":
    unittest.main()
