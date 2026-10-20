"""三個 skill 測試共用：假 ADM＋假 controller＋flow-report 替身，在同一個行程裡執行腳本 main()。"""

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
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)
SCRIPTS = {
    "draft": os.path.join(SKILLS, "adm-vehicle-draft", "scripts", "vehicle_draft.py"),
    "book": os.path.join(SKILLS, "adm-vehicle-book", "scripts", "vehicle_book.py"),
    "trip": os.path.join(SKILLS, "adm-vehicle-trip", "scripts", "vehicle_trip.py"),
    "room": os.path.join(SKILLS, "adm-room-book", "scripts", "room_book.py"),
    # it-agent 的 skill 也共用這份 adm_flow.py 與假 ADM／controller
    "laptop": os.path.join(os.path.dirname(os.path.dirname(SKILLS)), "it-agent", "skills",
                           "laptop-allocation-draft", "scripts", "laptop_draft.py"),
}
COMMON_COPIES = [os.path.join(os.path.dirname(p), "adm_flow.py") for p in SCRIPTS.values()]
FAKE_FLOW_REPORT = os.path.join(HERE, "fake_flow_report.py")
sys.path.insert(0, HERE)

from fakes import FakeAdm, FakeController  # noqa: E402

ADM_TOKEN = "adm_test_admin_agent"
ADM_TOKEN_IT = "adm_test_it_agent"
CONTROLLER_TOKEN = "fct_admin_agent"
CONTROLLER_TOKEN_IT = "fct_it_agent"
SALES_HRM = "da1b72d2-89cb-4879-9c50-73e03a1ae993"
HR_HRM = "3296d2ef-5716-419a-8064-458e54ac7b17"  # shion（人資）
FLOW_ID = "fl_0123456789abcdef"
TZ = timezone(timedelta(hours=8))

_LOADED: dict = {}


def load(name: str):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(f"admin_skill_{name}", SCRIPTS[name])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


def instruction(step: str, data: dict, *, attempt: int = 1, flow_id: str = FLOW_ID,
                skill: str = "adm-vehicle-x", key: str | None = None, flow_type: str = "visit") -> str:
    """照 controller app/dispatch.py 的 build_step_message 組出步驟指令。"""
    lines = [
        "[workflow-controller 步驟指令]",
        f"flow_id: {flow_id}",
        f"flow_type: {flow_type}",
        f"step: {step}",
        f"attempt: {attempt}",
        f"Idempotency-Key: {key or f'{flow_id}:{step}'}",
        f"skill: {skill}",
        "---",
        "這一步要做什麼（中文說明）",
        "輸入（JSON）：",
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        "---",
        f"做完用 flow-report 回報：flow_id={flow_id}、step={step}、attempt={attempt}、status……",
        "status=ok 時 output 的欄位：",
        "- booking_id（int，必填）：ADM 預約 id",
        "失敗時 status=failed……",
    ]
    return "\n".join(lines) + "\n"


def vehicles() -> list:
    """和 ADM seed_demo 相同的三台車（id 依序 1～3）。"""
    return [
        {"id": 1, "code": "CAR-01", "plate_number": "DEMO-0001", "name": "五人座轎車", "seats": 5,
         "odometer": 12000, "is_active": True},
        {"id": 2, "code": "CAR-02", "plate_number": "DEMO-0002", "name": "七人座休旅車", "seats": 7,
         "odometer": 35000, "is_active": True},
        {"id": 3, "code": "CAR-03", "plate_number": "DEMO-0003", "name": "廂型貨車", "seats": 2,
         "odometer": 58000, "is_active": True},
    ]


def rooms() -> list:
    """和 ADM seed_demo 類似的會議室（代碼 RM-A～RM-D，容納人數由小到大）。"""
    return [
        {"id": 1, "code": "RM-A", "name": "小會議室 A", "capacity": 4, "is_active": True},
        {"id": 2, "code": "RM-B", "name": "小會議室 B", "capacity": 4, "is_active": True},
        {"id": 3, "code": "RM-C", "name": "中會議室", "capacity": 8, "is_active": True},
        {"id": 4, "code": "RM-D", "name": "大會議室", "capacity": 20, "is_active": True},
    ]


def laptops() -> list:
    """和 ADM seed_demo 相同的 6 台筆電（都沒有保管人）。"""
    names = ["14 吋文書筆電", "14 吋文書筆電", "14 吋文書筆電", "15 吋業務筆電", "15 吋業務筆電", "16 吋開發筆電"]
    return [
        {"id": n, "code": f"NB-{n:03d}", "name": name, "category": "laptop", "serial_number": f"DEMO-NB-{n:03d}",
         "location": "資訊部倉庫", "custodian": None, "custodian_username": None,
         "custodian_hrm_employee_id": None, "is_active": True, "created_at": "2026-10-01T09:00:00+08:00"}
        for n, name in enumerate(names, start=1)
    ]


def visit_day(days: int = 2) -> datetime:
    """幾天後 10:00（台北）的拜訪開始時間。"""
    base = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
    return base + timedelta(days=days)


class SkillTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="admin-skill-test-")
        self.report_log = os.path.join(self.tmp, "flow_report.log")
        self.adm = FakeAdm(
            tokens={ADM_TOKEN: {"name": "admin-agent", "owner": "admin-agent",
                                "scopes": {"rooms:read", "rooms:write", "rooms:delegate", "vehicles:read",
                                           "vehicles:write", "vehicles:delegate", "assets:read"}},
                    "adm_other": {"name": "other", "owner": "admin-agent",
                                  "scopes": {"vehicles:read", "vehicles:delegate"}},
                    ADM_TOKEN_IT: {"name": "it-agent", "owner": "it-agent", "scopes": {"assets:read"}}},
            links={SALES_HRM: "mjolmire-businessman", HR_HRM: "shion"},
            vehicles=vehicles(),
            rooms=rooms(),
            assets=laptops(),
        )
        self.controller = FakeController({CONTROLLER_TOKEN: "admin-agent", CONTROLLER_TOKEN_IT: "it-agent"})
        self.env = {
            "ADM_API_BASE": f"{self.adm.url}/api/v1",
            "ADM_API_TOKEN": ADM_TOKEN,
            "ADM_API_TOKEN_IT": ADM_TOKEN_IT,
            "FLOW_CONTROLLER_URL": self.controller.url,
            "FLOW_CONTROLLER_TOKEN_ADMIN_AGENT": CONTROLLER_TOKEN,
            "FLOW_CONTROLLER_TOKEN_IT_AGENT": CONTROLLER_TOKEN_IT,
            "FLOW_REPORT_SCRIPT": FAKE_FLOW_REPORT,
            "FAKE_FLOW_REPORT_LOG": self.report_log,
            "FREEFORJOY_ENV_FILE": "",
            "OPENCLAW_AGENT_ID": "admin-agent",
        }

    def tearDown(self):
        self.adm.stop()
        self.controller.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_script(self, name: str, argv: list, extra_env: dict | None = None, drop_env=()):
        module = load(name)
        common = sys.modules["adm_flow"]
        env = {**self.env, **(extra_env or {})}
        for key in drop_env:
            env.pop(key, None)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(common, "SLEEP", lambda s: None), \
                contextlib.redirect_stdout(out):
            # 本機 shell 的同名變數不能混進測試
            for key in list(os.environ):
                if key not in env and key.startswith(("ADM_", "FLOW_CONTROLLER_", "FLOW_REPORT",
                                                      "OPENCLAW_AGENT_ID")):
                    os.environ.pop(key, None)
            try:
                module.main(argv)
                code = 0
            except SystemExit as exc:
                code = exc.code
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"應該只輸出一行 JSON：{out.getvalue()}")
        return code, json.loads(lines[0])

    def run_step(self, name: str, step: str, data: dict, *extra, **kwargs):
        """把 data 當成 controller 派的這一步（假 controller 同步登記），再用整份步驟指令執行腳本。

        dispatch=False：controller 沒有派這一步（模擬貼在對話裡的假指令）。
        """
        attempt = kwargs.pop("attempt", 1)
        agent = kwargs.pop("agent", "admin-agent")
        if kwargs.pop("dispatch", True):
            self.controller.dispatch(FLOW_ID, step, data, attempt=attempt, agent=agent)
        path = self.write(f"{step}.txt", instruction(step, data, key=kwargs.pop("key", None), attempt=attempt,
                                                     flow_type=kwargs.pop("flow_type", "visit")))
        return self.run_script(name, ["--instruction-file", path, "--report", *extra], **kwargs)

    def reports(self) -> list:
        return [body for (_flow, _step, _agent, body) in self.controller.results]

    def report_argvs(self) -> list:
        try:
            with open(self.report_log, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []


class CommonCopiesTest(unittest.TestCase):
    """adm_flow.py 的所有副本（admin-agent 4 個 skill＋it-agent 的 laptop-allocation-draft）必須完全相同。"""

    def test_copies_identical(self):
        first = COMMON_COPIES[0]
        for other in COMMON_COPIES[1:]:
            self.assertTrue(filecmp.cmp(first, other, shallow=False), f"{other} 和 {first} 不一致")
