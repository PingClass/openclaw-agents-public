"""共用模組 ffj_common 的測試。"""

from __future__ import annotations

import filecmp
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

from harness import SKILLS, SkillTestCase
from fakes import FakeServer


class Flaky(FakeServer):
    def route(self, method, path, query, body, headers, cookies):
        return 200, {"ok": True, "method": method}, {}


class CommonTests(SkillTestCase):
    def test_three_copies_are_identical(self):
        base = os.path.join(SKILLS, "agent-crm", "scripts", "ffj_common.py")
        for skill in ("agent-adm", "agent-inbox", "agent-hrm"):
            other = os.path.join(SKILLS, skill, "scripts", "ffj_common.py")
            self.assertTrue(filecmp.cmp(base, other, shallow=False),
                            f"{skill} 的 ffj_common.py 和 agent-crm 的不一致")

    def test_load_env_file_does_not_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# comment\nexport CRM_AGENT_PASSWORD='from-file'\nADM_AGENT_PASSWORD=\"x\"\n"
                     "FLOW_CONTROLLER_URL=http://file\n")
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {"FLOW_CONTROLLER_URL": "http://env"}, clear=False):
                os.environ.pop("CRM_AGENT_PASSWORD", None)
                os.environ.pop("ADM_AGENT_PASSWORD", None)
                self.c.load_env_file(path)
                self.assertEqual(os.environ["CRM_AGENT_PASSWORD"], "from-file")
                self.assertEqual(os.environ["ADM_AGENT_PASSWORD"], "x")
                self.assertEqual(os.environ["FLOW_CONTROLLER_URL"], "http://env")
        finally:
            os.unlink(path)

    def test_controller_token_name(self):
        with mock.patch.dict(os.environ, {"FLOW_CONTROLLER_TOKEN_MJOLMIRE_BUSINESSMAN": "t1"}):
            self.assertEqual(self.c.controller_token("mjolmire-businessman"), "t1")
        with self.assertRaises(self.c.ConfigError):
            self.c.controller_token("gobta")

    def test_add_workdays_skips_weekend(self):
        friday = date(2026, 10, 9)
        self.assertEqual(self.c.add_workdays(friday, 1), date(2026, 10, 12))
        self.assertEqual(self.c.add_workdays(friday, 3), date(2026, 10, 14))
        self.assertFalse(self.c.is_workday(date(2026, 10, 10)))

    def test_parse_dt(self):
        self.assertEqual(self.c.parse_dt("2026-10-12T02:00:00Z").hour, 10)
        self.assertEqual(self.c.parse_dt("2026-10-12 10:00").hour, 10)
        self.assertIsNone(self.c.parse_dt("not a date"))

    def test_get_retries_on_503(self):
        server = Flaky()
        try:
            server.faults[("GET", "/x")] = [503, 503]
            client = self.c.HttpClient(server.url, "fake")
            status, body = client.request("GET", "/x")
            self.assertEqual(status, 200)
            self.assertEqual(server.count("GET", "/x"), 3)
        finally:
            server.stop()

    def test_get_retries_on_dropped_connection(self):
        server = Flaky()
        try:
            server.faults[("GET", "/x")] = ["drop"]
            client = self.c.HttpClient(server.url, "fake")
            status, _ = client.request("GET", "/x")
            self.assertEqual(status, 200)
        finally:
            server.stop()

    def test_non_idempotent_post_is_not_retried_after_send(self):
        server = Flaky()
        try:
            server.faults[("POST", "/x")] = [503]
            client = self.c.HttpClient(server.url, "fake")
            status, _ = client.request("POST", "/x", {"a": 1}, idempotent=False)
            self.assertEqual(status, 503)
            self.assertEqual(server.count("POST", "/x"), 1)

            server.faults[("POST", "/y")] = ["drop"]
            with self.assertRaises(self.c.ServiceError):
                client.request("POST", "/y", {"a": 1}, idempotent=False)
            self.assertEqual(server.count("POST", "/y"), 1)
        finally:
            server.stop()

    def test_connection_refused_is_retried_then_fails_quietly(self):
        sleeps = []
        with mock.patch.object(self.c, "SLEEP", sleeps.append):
            client = self.c.HttpClient("http://127.0.0.1:9", "nowhere")
            with self.assertRaises(self.c.ServiceError) as ctx:
                client.request("POST", "/x", {}, idempotent=False)
        self.assertEqual(len(sleeps), len(self.c.RETRY_DELAYS))
        self.assertFalse(ctx.exception.announce)

    def test_unknown_agent_is_config_error(self):
        code, out = self.run_script("adm", ["status", "--agent", "nobody", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertEqual(out["outcome"], "config_error")
        self.assertTrue(out["announce"])

    def test_agent_from_environment(self):
        code, out = self.run_script("adm", ["status", "--dry-run"], {"OPENCLAW_AGENT_ID": "shion"})
        self.assertEqual(code, 0)
        self.assertEqual(out["agent"], "shion")


if __name__ == "__main__":
    unittest.main()
