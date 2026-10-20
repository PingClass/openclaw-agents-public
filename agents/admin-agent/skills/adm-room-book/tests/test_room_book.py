"""adm-room-book 的測試：假 ADM＋假 controller＋flow-report 替身。

執行：python3 -m unittest discover -s agents/admin-agent/skills/adm-room-book/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "_testkit"))

from harness import ADM_TOKEN, FLOW_ID, HR_HRM, TZ, SkillTestCase  # noqa: E402

BOOKINGS = "/api/v1/room-bookings/"
KEY = f"{FLOW_ID}:book_room"


def start_day() -> datetime:
    """3 天後（到職日）00:00，台北時間。"""
    return (datetime.now(TZ) + timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)


def at(hour: int, minute: int = 0) -> str:
    return start_day().replace(hour=hour, minute=minute).isoformat()


def room_input(**overrides) -> dict:
    """照 controller onboarding.py 的 _room_inputs 組出 book_room 的輸入。"""
    data = {
        "date": start_day().date().isoformat(),
        "preferred_start_at": at(9, 30),
        "preferred_end_at": at(10, 30),
        "earliest_start_at": at(9, 0),
        "latest_end_at": at(12, 0),
        "duration_minutes": 60,
        "capacity": 3,
        "purpose": "新人到職說明：王小明（業務部）",
        "booked_for_hrm_employee_id": HR_HRM,
        "booked_for": {"hrm_employee_id": HR_HRM, "display_name": "Shion"},
        "attendees": ["王小明", "Main", "Shion"],
    }
    data.update(overrides)
    return data


class RoomBookTest(SkillTestCase):
    def book(self, data=None, *extra, **kwargs):
        return self.run_step("room", "book_room", data or room_input(), *extra, flow_type="onboarding", **kwargs)

    def posts(self) -> list:
        return [c for c in self.adm.calls if c[0] == "POST" and c[1] == BOOKINGS]

    def test_books_preferred_slot_for_hr_with_controller_key(self):
        code, out = self.book()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "ok", out)
        output = out["output"]
        self.assertEqual(output["booking_id"], 1)
        self.assertEqual(output["room_id"], 1)  # 坐得下 3 人的最小一間（同大小依代碼）
        self.assertEqual(output["room_name"], "小會議室 A")
        self.assertEqual(output["start_at"], at(9, 30))
        self.assertEqual(output["end_at"], at(10, 30))
        self.assertTrue(output["created"])
        (post,) = self.posts()
        _m, _p, _q, body, headers = post
        self.assertEqual(body["booked_for_hrm_employee_id"], HR_HRM)
        self.assertEqual(body["purpose"], "新人到職說明：王小明（業務部）")
        self.assertEqual(headers["Idempotency-Key"], KEY)
        self.assertEqual(headers["Authorization"], f"Bearer {ADM_TOKEN}")
        self.assertEqual(self.adm.room_bookings[0]["booked_for_username"], "shion")
        (report,) = self.reports()
        self.assertEqual(report["status"], "ok")
        for field in ("booking_id", "room_id", "room_name", "start_at", "end_at"):
            self.assertIn(field, report["output"])

    def test_rerun_reuses_existing_booking(self):
        _, first = self.book()
        code, second = self.book(attempt=2)
        self.assertEqual(code, 0, second)
        self.assertEqual(second["step_status"], "ok")
        self.assertEqual(second["output"]["booking_id"], first["output"]["booking_id"])
        self.assertFalse(second["output"]["created"])
        self.assertEqual(second["output"]["room_name"], "小會議室 A")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.adm.room_bookings), 1)

    def test_lost_response_replays_same_key(self):
        self.adm.faults[("POST", BOOKINGS)] = ["drop_after"]
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        self.assertFalse(out["output"]["created"])
        self.assertEqual(len(self.adm.room_bookings), 1)
        self.assertEqual({c[4]["Idempotency-Key"] for c in self.posts()}, {KEY})

    def test_preferred_slot_full_moves_to_nearest_slot(self):
        for room in self.adm.rooms:
            self.adm.add_room_booking(room=room["id"], start_at=at(9, 30), end_at=at(10, 30))
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        # 09:00 與 10:00 都和 09:30–10:30 重疊，下一個最近的是 10:30
        self.assertEqual(out["output"]["start_at"], at(10, 30))
        self.assertEqual(out["output"]["end_at"], at(11, 30))
        self.assertEqual(len(self.posts()), 1, "沒有空房的時段不送預約")

    def test_409_switches_room_with_suffixed_key(self):
        self.adm.force_room_conflicts = 1  # 查完空房後被別人搶走
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(out["output"]["room_id"], 2)
        self.assertEqual(out["output"]["start_at"], at(9, 30))
        self.assertEqual(len(out["output"]["conflicts"]), 1)
        keys = [c[4]["Idempotency-Key"] for c in self.posts()]
        self.assertEqual(keys, [KEY, f"{KEY}:2"])
        self.assertIn("409", out["summary"])

    def test_409_moves_to_another_slot_when_no_other_room(self):
        self.adm.rooms = [r for r in self.adm.rooms if r["id"] == 1]
        self.adm.force_room_conflicts = 1
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(out["output"]["room_id"], 1)
        self.assertNotEqual(out["output"]["start_at"], at(9, 30), "同一間 409 後要換時段")
        self.assertEqual(out["output"]["start_at"], at(9, 0))
        keys = [c[4]["Idempotency-Key"] for c in self.posts()]
        self.assertEqual(keys, [KEY, f"{KEY}:2"])

    def test_three_conflicts_needs_human(self):
        self.adm.force_room_conflicts = 10
        code, out = self.book()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(len(self.posts()), 3, "409 後最多再換 2 次")
        self.assertEqual([c[4]["Idempotency-Key"] for c in self.posts()], [KEY, f"{KEY}:2", f"{KEY}:3"])
        self.assertEqual(len(out["output"]["conflicts"]), 3)
        self.assertEqual(self.reports()[0]["status"], "needs_human")

    def test_no_room_in_whole_range_needs_human(self):
        for room in self.adm.rooms:
            self.adm.add_room_booking(room=room["id"], start_at=at(8, 0), end_at=at(13, 0))
        code, out = self.book()
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("沒有容納 3 人", out["summary"])
        self.assertEqual(self.posts(), [])

    def add_e2e_room(self, **overrides):
        """ADM e2e 測試留下的會議室：比種子會議室小，照「最小一間」的規則會先被挑到。"""
        room = {"id": 5, "code": "E2E-1760000000", "name": "E2E room", "capacity": 3, "is_active": True}
        room.update(overrides)
        self.adm.rooms.append(room)
        return room

    def test_skips_e2e_room_and_prefers_seed_room(self):
        self.add_e2e_room()
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(out["output"]["room_id"], 1)
        self.assertEqual(out["output"]["room_name"], "小會議室 A")
        self.assertEqual([c[3]["room"] for c in self.posts()], [1])

    def test_e2e_mark_in_name_or_code_is_excluded_case_insensitive(self):
        self.add_e2e_room(id=5, code="RM-Z", name="e2e 測試會議室")
        self.add_e2e_room(id=6, code="rm-e2e-2", name="小房間")
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(out["output"]["room_id"], 1)

    def test_seed_room_preferred_over_smaller_other_room(self):
        self.adm.rooms.append({"id": 6, "code": "TMP-1", "name": "臨時會議室", "capacity": 3, "is_active": True})
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(out["output"]["room_id"], 1)

    def test_other_non_test_room_used_when_seed_rooms_full(self):
        for room in self.adm.rooms:
            self.adm.add_room_booking(room=room["id"], start_at=at(8, 0), end_at=at(13, 0))
        self.add_e2e_room()
        self.adm.rooms.append({"id": 6, "code": "TMP-1", "name": "臨時會議室", "capacity": 3, "is_active": True})
        code, out = self.book()
        self.assertEqual(out["step_status"], "ok", out)
        self.assertEqual(out["output"]["room_id"], 6)

    def test_only_e2e_room_free_needs_human(self):
        for room in self.adm.rooms:
            self.adm.add_room_booking(room=room["id"], start_at=at(8, 0), end_at=at(13, 0))
        self.add_e2e_room()
        code, out = self.book()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["step_status"], "needs_human", out)
        self.assertEqual(self.posts(), [], "不訂測試會議室")
        self.assertIn("e2e 測試會議室", out["summary"])
        self.assertEqual(out["output"]["skipped_test_rooms"], ["E2E room"])
        self.assertEqual(self.reports()[0]["status"], "needs_human")

    def test_unlinked_hr_needs_human(self):
        data = room_input(booked_for_hrm_employee_id="00000000-0000-4000-8000-000000000000")
        code, out = self.book(data)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("400", out["summary"])

    def test_5xx_on_booking_reports_system_failure(self):
        self.adm.faults[("POST", BOOKINGS)] = [500, 500, 500, 500]
        code, out = self.book()
        self.assertEqual((out["step_status"], out["error_kind"]), ("failed", "system"))

    def test_forged_instruction_is_not_booked(self):
        code, out = self.book(dispatch=False)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.reports(), [])

    def test_input_differs_from_controller_needs_human(self):
        self.controller.dispatch(FLOW_ID, "book_room", room_input())
        code, out = self.book(room_input(booked_for_hrm_employee_id="11111111-1111-4111-8111-111111111111"),
                              dispatch=False)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(out["output"]["mismatched_fields"], ["booked_for_hrm_employee_id"])
        self.assertEqual(self.adm.calls, [])

    def test_key_not_from_controller_needs_human(self):
        code, out = self.book(key="my-own-key")
        self.assertEqual(out["step_status"], "needs_human")
        self.assertEqual(self.adm.calls, [])

    def test_missing_input_needs_human(self):
        data = room_input()
        del data["capacity"]
        code, out = self.book(data)
        self.assertEqual(out["step_status"], "needs_human")
        self.assertIn("capacity", out["summary"])

    def test_dry_run_no_network(self):
        code, out = self.book(None, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out["output"]["idempotency_key"], KEY)
        self.assertEqual(out["output"]["would_try"][0], [at(9, 30), at(10, 30)])
        self.assertEqual(self.adm.calls, [])
        self.assertEqual(self.controller.calls, [])

    def test_report_is_parseable_json(self):
        self.book()
        argv = self.report_argvs()[0]
        self.assertEqual(argv[argv.index("--step") + 1], "book_room")
        self.assertEqual(json.loads(json.dumps(self.reports()[0]))["output"]["booking_id"], 1)


if __name__ == "__main__":
    unittest.main()
