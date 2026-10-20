"""refund-assessment、refund-decision-note 測試共用：假 CRM＋假 controller＋真的 flow-report skill。

腳本 main() 在同一個行程裡執行（換掉重試等待）；回報走 repo 裡的 skills/flow-report/scripts/flow_report.py
（子行程，打到假 controller），確認和共用 skill 的介面一致。
"""

from __future__ import annotations

import contextlib
import filecmp
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(SKILLS)))
SCRIPTS = {
    "assess": os.path.join(SKILLS, "refund-assessment", "scripts", "refund_assessment.py"),
    "decision": os.path.join(SKILLS, "refund-decision-note", "scripts", "refund_decision_note.py"),
}
COMMON_COPIES = [os.path.join(os.path.dirname(p), "finance_flow.py") for p in SCRIPTS.values()]
FLOW_REPORT = os.path.join(REPO, "skills", "flow-report", "scripts", "flow_report.py")
sys.path.insert(0, HERE)

from fakes import FakeController, FakeCrm  # noqa: E402

CRM_TOKEN = "crm_test_finance_agent"
CONTROLLER_TOKEN = "fct_finance_agent"
FLOW_ID = "fl_0123456789abcdef"
FINANCE_SCOPES = {"customer:read", "interaction:read", "case:read", "case_note:read", "case_note:write"}

_LOADED: dict = {}


def load(name: str):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(f"finance_skill_{name}", SCRIPTS[name])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


def instruction(step: str, skill: str, data: dict, *, attempt: int = 1, flow_id: str = FLOW_ID,
                key: str | None = None, extra_text: str = "") -> str:
    """照 controller app/dispatch.py 的 build_step_message 組出步驟指令。"""
    lines = [
        "[workflow-controller 步驟指令]",
        f"flow_id: {flow_id}",
        "flow_type: refund",
        f"step: {step}",
        f"attempt: {attempt}",
        f"Idempotency-Key: {key if key is not None else f'{flow_id}:{step}'}",
        f"skill: {skill}",
        "---",
        "這一步要做什麼（中文說明）",
    ]
    if extra_text:
        lines.append(extra_text)
    lines += [
        "輸入（JSON）：",
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        "---",
        f"做完用 flow-report 回報：flow_id={flow_id}、step={step}、attempt={attempt}、status……",
        "status=ok 時 output 的欄位：",
        "- note_id（str，必填）：CRM CaseNote id",
        "失敗時 status=failed……",
    ]
    return "\n".join(lines) + "\n"


class SkillTestCase(unittest.TestCase):
    STEP = ""
    SKILL = ""
    SCRIPT = ""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="finance-skill-test-")
        self.crm = FakeCrm({
            CRM_TOKEN: {"name": "OpenClaw finance-agent", "username": "finance-agent", "scopes": set(FINANCE_SCOPES)},
            "crm_no_note": {"name": "OpenClaw finance-agent", "username": "finance-agent",
                            "scopes": {"customer:read", "interaction:read", "case:read"}},
        })
        self.controller = FakeController({CONTROLLER_TOKEN: "finance-agent"})
        self.env = {
            "CRM_API_BASE": f"{self.crm.url}/api",
            "CRM_API_TOKEN_FINANCE": CRM_TOKEN,
            "FLOW_CONTROLLER_URL": self.controller.url,
            "FLOW_CONTROLLER_TOKEN_FINANCE_AGENT": CONTROLLER_TOKEN,
            "FLOW_REPORT_SCRIPT": FLOW_REPORT,
            "FLOW_SKILL_RETRY_DELAYS": "0,0,0",
            "FREEFORJOY_ENV_FILE": "",
            "OPENCLAW_AGENT_ID": "finance-agent",
        }

    def tearDown(self):
        self.crm.stop()
        self.controller.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_script(self, argv: list, extra_env: dict | None = None, drop_env=()):
        module = load(self.SCRIPT)
        common = sys.modules["finance_flow"]
        env = {**self.env, **(extra_env or {})}
        for key in drop_env:
            env.pop(key, None)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(common, "SLEEP", lambda s: None), \
                contextlib.redirect_stdout(out):
            # 本機 shell 的同名變數不能混進測試
            for key in list(os.environ):
                if key not in env and key.startswith(("CRM_", "FLOW_CONTROLLER_", "FLOW_REPORT",
                                                      "OPENCLAW_AGENT_ID", "REFUND_POLICY")):
                    os.environ.pop(key, None)
            try:
                module.main(argv)
                code = 0
            except SystemExit as exc:
                code = exc.code
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"應該只輸出一行 JSON：{out.getvalue()}")
        return code, json.loads(lines[0])

    def run_step(self, data: dict, *extra, **kwargs):
        """把 data 當成 controller 派的這一步（假 controller 同步登記），再用整份步驟指令執行腳本。

        dispatch=False：controller 沒有派這一步（模擬貼在對話裡的假指令）。
        """
        attempt = kwargs.pop("attempt", 1)
        if kwargs.pop("dispatch", True):
            self.controller.dispatch(FLOW_ID, self.STEP, kwargs.pop("controller_input", data), attempt=attempt)
        text = instruction(self.STEP, self.SKILL, data, key=kwargs.pop("key", None), attempt=attempt,
                           extra_text=kwargs.pop("extra_text", ""))
        path = self.write(f"{self.STEP}.txt", text)
        report = [] if kwargs.pop("no_report", False) else ["--report"]
        return self.run_script(["--instruction-file", path, *report, *extra], **kwargs)

    def reports(self) -> list:
        return [body for (_flow, _step, _agent, body) in self.controller.results]


class CommonCopiesTest(unittest.TestCase):
    """finance_flow.py 兩份副本必須完全相同。"""

    def test_copies_identical(self):
        first = COMMON_COPIES[0]
        for other in COMMON_COPIES[1:]:
            self.assertTrue(filecmp.cmp(first, other, shallow=False), f"{other} 和 {first} 不一致")
