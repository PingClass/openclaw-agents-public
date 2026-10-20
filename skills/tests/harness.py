"""測試共用：載入四個腳本、在同一個行程裡執行 main()、假的 openclaw 執行檔。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import random
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

SKILLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = {
    "crm": os.path.join(SKILLS, "agent-crm", "scripts", "crm.py"),
    "adm": os.path.join(SKILLS, "agent-adm", "scripts", "adm.py"),
    "inbox": os.path.join(SKILLS, "agent-inbox", "scripts", "inbox.py"),
    "hrm": os.path.join(SKILLS, "agent-hrm", "scripts", "hrm.py"),
}
_LOADED: dict = {}


def load(name: str):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(f"skill_{name}", SCRIPTS[name])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


def common():
    load("crm")
    return sys.modules["ffj_common"]


class StubRng(random.Random):
    """random() 依序回傳指定值（用完就重複最後一個），其餘方法照常（固定種子）。"""

    def __init__(self, values):
        super().__init__(1234)
        self.values = list(values)

    def random(self):  # noqa: D401
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


FAKE_OPENCLAW = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_OPENCLAW_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
if os.environ.get("FAKE_OPENCLAW_FAIL"):
    sys.stderr.write("boom")
    sys.exit(2)
print(json.dumps({"ok": True}))
"""


class SkillTestCase(unittest.TestCase):
    """每個測試一個乾淨的狀態目錄與假 openclaw；重試不等待。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skill-test-")
        self.state_dir = os.path.join(self.tmp, "state")
        self.cron_log = os.path.join(self.tmp, "cron.log")
        bin_path = os.path.join(self.tmp, "openclaw")
        with open(bin_path, "w", encoding="utf-8") as fh:
            fh.write(FAKE_OPENCLAW)
        os.chmod(bin_path, os.stat(bin_path).st_mode | stat.S_IEXEC)
        self.env = {
            "OPENCLAW_STATE_DIR": self.state_dir,
            "OPENCLAW_BIN": bin_path,
            "FAKE_OPENCLAW_LOG": self.cron_log,
            "FREEFORJOY_ENV_FILE": "",
        }
        self.c = common()
        self._patches = [
            mock.patch.object(self.c, "SLEEP", lambda s: None),
            mock.patch.object(self.c, "RNG", random.Random(42)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_rng(self, *values):
        patcher = mock.patch.object(self.c, "RNG", StubRng(values))
        patcher.start()
        self._patches.append(patcher)

    def run_script(self, name: str, argv: list, extra_env: dict | None = None):
        module = load(name)
        env = {**self.env, **(extra_env or {})}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out):
            # 本機 shell 的同名變數不能混進測試
            for key in list(os.environ):
                if key in env:
                    continue
                if key == "OPENCLAW_AGENT_ID" or key.startswith(
                        ("CRM_", "ADM_", "HRM_", "FLOW_CONTROLLER_", "AGENT_INBOX_")):
                    os.environ.pop(key, None)
            try:
                module.main(argv)
                code = 0
            except SystemExit as exc:
                code = exc.code
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"應該只輸出一行 JSON：{out.getvalue()}")
        return code, json.loads(lines[0])

    def cron_calls(self) -> list:
        try:
            with open(self.cron_log, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def state(self, *parts):
        path = os.path.join(self.state_dir, *parts)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


def argv_value(argv: list, flag: str):
    return argv[argv.index(flag) + 1]
