"""agent-adm（adm.py）的測試：假 ADM。"""

from __future__ import annotations

import unittest

from fakes import FakeAdm
from harness import SkillTestCase

MONDAY = "2026-10-12"
MORNING = "2026-10-12T09:50:00+08:00"
AGENTS = ("main", "shion", "souei", "gobta", "mjolmire-businessman", "luminous", "hinata")


class AdmTestBase(SkillTestCase):
    def setUp(self):
        super().setUp()
        self.adm = FakeAdm(
            {a: {"password": "apw", "id": i} for i, a in enumerate(AGENTS, start=1)},
            [{"id": 1, "code": "R-201", "name": "小會議室", "capacity": 4},
             {"id": 2, "code": "R-301", "name": "大會議室", "capacity": 10}],
            [{"id": 1, "code": "CAR-1", "plate_number": "ABC-1234", "name": "Altis", "seats": 5,
              "odometer": 12000}],
        )
        self.env.update({"ADM_API_BASE": f"{self.adm.url}/api/v1", "ADM_AGENT_PASSWORD": "apw"})

    def tearDown(self):
        self.adm.stop()
        super().tearDown()

    def decide(self, agent="souei", *extra, date=MONDAY, now=MORNING):
        return self.run_script("adm", ["decide", "--agent", agent, "--date", date, "--now", now, *extra])


class DecideTests(AdmTestBase):
    def test_room_roll_hit_books_room(self):
        self.set_rng(0.05)  # < 10%
        code, out = self.decide()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "booked")
        self.assertEqual(len(self.adm.room_bookings), 1)
        booking = self.adm.room_bookings[0]
        self.assertEqual(booking["booked_by_username"], "souei")
        self.assertTrue(booking["_key"].startswith("agent-adm:souei:room:"))
        self.assertEqual(self.adm.vehicle_bookings, [])
        self.assertFalse(out["announce"])

    def test_room_roll_miss(self):
        self.set_rng(0.10)
        code, out = self.decide()
        self.assertEqual(out["outcome"], "no_booking")
        self.assertEqual(self.adm.calls, [])

    def test_rerun_is_idempotent(self):
        self.set_rng(0.05)
        self.decide()
        self.set_rng(0.99)
        code, out = self.decide()
        self.assertEqual(out["outcome"], "already_decided")
        self.assertEqual(len(self.adm.room_bookings), 1)
        self.assertEqual(self.adm.count("POST", "/api/v1/room-bookings/"), 1)

    def test_only_luminous_rolls_for_vehicle(self):
        # room 0.5（不中）、vehicle 0.04（中）
        self.set_rng(0.5, 0.04)
        code, out = self.decide("luminous")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["decision"], {"room": False, "vehicle": True})
        self.assertEqual(len(self.adm.vehicle_bookings), 1)
        booking = self.adm.vehicle_bookings[0]
        self.assertEqual(booking["booked_by_username"], "luminous")
        self.assertIsNone(booking["booked_for_username"])
        self.assertTrue(booking["destination"])
        self.assertTrue(booking["purpose"].startswith("行政外出"))

        self.set_rng(0.5, 0.04)
        _, out = self.decide("shion")
        self.assertEqual(out["decision"], {"room": False, "vehicle": False})
        self.assertIsNone(out["rolls"]["vehicle"])

    def test_conflict_retries_twice_then_succeeds(self):
        self.adm.force_conflicts = 2
        code, out = self.decide("souei", "--force", "room")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "booked")
        attempts = out["bookings"]["room"]["attempts"]
        self.assertEqual([a["result"] for a in attempts], ["conflict", "conflict", "booked"])
        self.assertEqual(len({(a["start_at"], a["end_at"]) for a in attempts}), 3, "每次要換時段")

    def test_gives_up_after_three_attempts(self):
        self.adm.force_conflicts = 10
        code, out = self.decide("souei", "--force", "room")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "unavailable")
        self.assertEqual(self.adm.count("POST", "/api/v1/room-bookings/"), 3)
        # 重跑不會再試
        code, out = self.decide("souei")
        self.assertEqual(self.adm.count("POST", "/api/v1/room-bookings/"), 3)

    def test_lost_response_replays_same_key(self):
        self.adm.faults[("POST", "/api/v1/room-bookings/")] = ["drop_after"]
        code, out = self.decide("souei", "--force", "room")
        # POST 帶冪等鍵，視為可重試：同鍵同內容拿回原本那筆
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "booked")
        self.assertEqual(len(self.adm.room_bookings), 1)

    def test_network_error_then_rerun_reuses_attempt(self):
        self.adm.faults[("POST", "/api/v1/room-bookings/")] = [503, 503, 503, 503]
        code, out = self.decide("souei", "--force", "room")
        self.assertEqual(code, 1)
        self.assertFalse(out["announce"])
        state = self.state("agent-adm", "souei", f"{MONDAY}.json")
        sent = state["room"]["attempts"][-1]
        self.assertEqual(sent["result"], "sent")
        code, out = self.decide("souei")
        self.assertEqual(out["outcome"], "booked", out)
        self.assertEqual(self.adm.room_bookings[0]["_key"], sent["key"])
        self.assertEqual(len(self.adm.room_bookings), 1)

    def test_get_retry_on_503(self):
        self.adm.faults[("GET", "/api/v1/rooms/availability/")] = [503]
        code, out = self.decide("souei", "--force", "room")
        self.assertEqual(out["outcome"], "booked", out)

    def test_no_room_big_enough(self):
        self.adm.rooms = [{"id": 1, "code": "R-1", "name": "電話亭", "capacity": 1}]
        code, out = self.decide("souei", "--force", "room")
        self.assertEqual(out["outcome"], "unavailable")
        self.assertEqual(self.adm.count("POST", "/api/v1/room-bookings/"), 0)

    def test_weekend_and_missing_password(self):
        _, out = self.decide("souei", date="2026-10-11", now="2026-10-11T09:50:00+08:00")
        self.assertEqual(out["outcome"], "not_working_day")
        code, out = self.run_script("adm", ["decide", "--agent", "souei", "--force", "room",
                                            "--date", MONDAY, "--now", MORNING],
                                    {"ADM_AGENT_PASSWORD": ""})
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertTrue(out["announce"])


class BookRoomTests(AdmTestBase):
    def test_book_specific_slot_then_rerun(self):
        argv = ["book-room", "--agent", "hinata", "--on", "2026-10-13", "--start", "14:00",
                "--duration", "60", "--capacity", "6", "--purpose", "客訴案例檢討",
                "--date", MONDAY, "--now", MORNING]
        code, out = self.run_script("adm", argv)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["outcome"], "room_booked")
        booking = self.adm.room_bookings[0]
        self.assertEqual(booking["start_at"], "2026-10-13T14:00:00+08:00")
        self.assertEqual(booking["end_at"], "2026-10-13T15:00:00+08:00")
        self.assertEqual(booking["room"], 2)  # 只有大會議室坐得下 6 人
        code, out = self.run_script("adm", argv)
        self.assertEqual(out["outcome"], "already_booked")
        self.assertEqual(len(self.adm.room_bookings), 1)

    def test_conflict_moves_to_another_slot_same_day(self):
        self.adm.room_bookings.append({
            "id": 99, "room": 2, "start_at": "2026-10-13T14:00:00+08:00",
            "end_at": "2026-10-13T15:00:00+08:00", "status": "active",
            "booked_by_username": "main", "booked_for_username": None, "_key": "x", "_digest": "y"})
        code, out = self.run_script("adm", ["book-room", "--agent", "hinata", "--on", "2026-10-13",
                                            "--start", "14:00", "--capacity", "6",
                                            "--date", MONDAY, "--now", MORNING])
        self.assertEqual(out["outcome"], "room_booked", out)
        mine = [b for b in self.adm.room_bookings if b["booked_by_username"] == "hinata"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["start_at"][:10], "2026-10-13")
        self.assertNotEqual(mine[0]["start_at"], "2026-10-13T14:00:00+08:00")

    def test_status_lists_upcoming(self):
        self.run_script("adm", ["book-room", "--agent", "hinata", "--on", "2026-10-13", "--start", "10:00",
                                "--date", MONDAY, "--now", MORNING])
        code, out = self.run_script("adm", ["status", "--agent", "hinata", "--date", MONDAY,
                                            "--now", MORNING])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out["room_bookings"]), 1)


if __name__ == "__main__":
    unittest.main()
