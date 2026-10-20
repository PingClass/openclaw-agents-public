"""flow-start、flow-report 的測試：假的 controller（http.server）＋ 實際執行腳本（subprocess）。

執行：python3 -m unittest discover -s skills/tests -v
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

from fakes import FakeServer
from harness import SKILLS

REPORT = os.path.join(SKILLS, "flow-report", "scripts", "flow_report.py")
START = os.path.join(SKILLS, "flow-start", "scripts", "flow_start.py")

TOKENS = {"tok-cs": "cs-agent", "tok-admin": "admin-agent", "tok-fin": "finance-agent"}
VISIT_INPUT = {
    "follow_up_id": 123,
    "customer": {"name": "客戶 C", "external_id": "C-003"},
    "visit_start_at": "2026-10-15T10:00:00+08:00",
    "visit_end_at": "2026-10-15T12:00:00+08:00",
    "location": "台中 客戶 C 工廠",
    "seats": 2,
    "assignee_hrm_employee_id": "da1b72d2-89cb-4879-9c50-73e03a1ae993",
    "subject": "新品試吃",
}
REQUIRED_OUTPUT = {"book_vehicle": ["booking_id", "vehicle_id", "start_at", "end_at"]}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def err(status, code, message, **extra):
    return status, {"detail": {"code": code, "message": message, **extra}}, {}


class FakeFlowController(FakeServer):
    """只實作 flow-start／flow-report 用到的端點，錯誤格式照 controller 的 {"detail": {code, message}}。"""

    def __init__(self):
        super().__init__()
        self.flows: dict = {}
        self.revision_conflicts = 0  # 接下來幾次回報要回 409 revision_conflict（模擬同時更新）

    def add_flow(self, flow_id="fl_1", step="book_vehicle", assignee="admin-agent", attempts=1):
        self.flows[flow_id] = {
            "id": flow_id, "type": "visit", "title": "10/15 拜訪 客戶 C", "status": "running",
            "revision": 3, "current_step": step, "created_by": "agent:cs-agent", "dedupe_key": None,
            "steps": {step: {"assignee": assignee, "status": "dispatched", "attempts": attempts}},
            "audit": [],
        }
        return self.flows[flow_id]

    def _agent(self, headers):
        auth = headers.get("Authorization") or ""
        return TOKENS.get(auth[7:]) if auth.startswith("Bearer ") else None

    def summary(self, flow):
        return {k: flow[k] for k in ("id", "type", "title", "status", "revision", "current_step")}

    def route(self, method, path, query, body, headers, cookies):
        agent = self._agent(headers)
        if agent is None:
            return err(401, "unauthorized", "agent token 無效或已停用")
        parts = path.strip("/").split("/")
        if method == "POST" and parts == ["api", "flows"]:
            return self.create(agent, body)
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "flows"]:
            flow = self.flows.get(parts[2])
            if flow is None:
                return err(404, "flow_not_found", "找不到流程")
            steps = [{"name": name, **row} for name, row in flow["steps"].items()]
            return 200, {**self.summary(flow), "steps": steps, "audit": flow["audit"]}, {}
        if (method == "POST" and len(parts) == 6 and parts[:2] == ["api", "flows"]
                and parts[3] == "steps" and parts[5] == "result"):
            return self.report(agent, parts[2], parts[4], body)
        return 404, {"detail": "Not Found"}, {}

    def create(self, agent, body):
        if not isinstance(body, dict) or not isinstance(body.get("type"), str):
            return err(422, "validation_error", "請求內容格式錯誤",
                       errors=[{"loc": ["body", "type"], "msg": "Field required"}])
        if body["type"] not in ("visit", "demo"):
            return err(422, "unknown_flow_type", f"沒有這種流程：{body['type']}")
        if agent != "cs-agent":
            return err(403, "not_allowed_to_start", f"{agent} 不能啟動 {body['type']} 流程")
        data = body.get("input") or {}
        if body["type"] == "visit":
            if "follow_up_id" not in data:
                return err(422, "invalid_input", "缺少 follow_up_id")
            dedupe = f"crm:follow-up:{data['follow_up_id']}"
        else:
            dedupe = body.get("dedupe_key")
        for flow in self.flows.values():
            if dedupe and flow["dedupe_key"] == dedupe:
                return 200, {**self.summary(flow), "created": False}, {}
        flow_id = f"fl_{len(self.flows) + 1:04d}"
        self.flows[flow_id] = {
            "id": flow_id, "type": body["type"], "title": body.get("title") or "10/15 拜訪 客戶 C",
            "status": "running", "revision": 1, "current_step": "resolve_parties",
            "created_by": f"agent:{agent}", "dedupe_key": dedupe, "steps": {}, "audit": [],
        }
        return 201, {**self.summary(self.flows[flow_id]), "created": True}, {}

    def report(self, agent, flow_id, step_name, body):
        if not isinstance(body, dict) or body.get("status") not in ("ok", "failed", "needs_human"):
            return err(422, "validation_error", "請求內容格式錯誤",
                       errors=[{"loc": ["body", "status"], "msg": "Input should be 'ok', 'failed' or 'needs_human'"}])
        flow = self.flows.get(flow_id)
        if flow is None:
            return err(404, "flow_not_found", "找不到流程")
        if body.get("revision") is not None and body["revision"] != flow["revision"]:
            return err(409, "revision_conflict", "流程已被更新，請重新整理")
        if self.revision_conflicts:
            self.revision_conflicts -= 1
            return err(409, "revision_conflict", "流程同時被更新，請重新整理後再試")
        if flow["status"] not in ("running", "waiting"):
            return err(409, "flow_finished", f"流程已結束（{flow['status']}）")
        step = flow["steps"].get(step_name)
        if step is None:
            return err(404, "step_not_found", f"找不到步驟 {step_name}")
        if step["assignee"] != agent:
            return err(403, "not_assignee", "這個步驟不是指派給你的")
        if flow["current_step"] != step_name:
            return err(409, "step_not_current", "這個步驟不是目前的步驟")
        if step["status"] not in ("dispatched", "running"):
            return err(409, "step_not_running", f"步驟目前狀態為 {step['status']}，不接受回報")
        if body.get("attempt") is not None and body["attempt"] != step["attempts"]:
            return err(409, "stale_attempt", f"回報的 attempt {body['attempt']} 已過期（目前 {step['attempts']}）")
        output = body.get("output") or {}
        if body["status"] == "ok":
            missing = [f for f in REQUIRED_OUTPUT.get(step_name, []) if output.get(f) is None]
            if missing:
                return err(422, "missing_output_fields", f"output 缺少欄位：{', '.join(missing)}")
        flow["audit"].append({"action": "step.reported", "actor": f"agent:{agent}",
                              "detail": {"step": step_name, "status": body["status"],
                                         "summary": body.get("summary"), "attempt": step["attempts"]}})
        step["status"] = "succeeded" if body["status"] == "ok" else "failed"
        flow["revision"] += 1
        if body["status"] == "ok":
            flow["current_step"] = "await_trip"
        return 200, self.summary(flow), {}


class FlowSkillTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flow-skill-test-")
        self.ctl = FakeFlowController()
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.tmp,  # secrets 檔的預設位置 ~/.openclaw/secrets/freeforjoy.env 指到暫存目錄
            "FLOW_CONTROLLER_URL": self.ctl.url,
            "FLOW_CONTROLLER_TOKEN_CS_AGENT": "tok-cs",
            "FLOW_CONTROLLER_TOKEN_ADMIN_AGENT": "tok-admin",
            "FLOW_SKILL_RETRY_DELAYS": "0,0,0",
        }

    def tearDown(self):
        self.ctl.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, data) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
        return path

    def run_cli(self, script: str, *args, env: dict | None = None, drop: tuple = (),
                stdin: str | None = None):
        full_env = {**self.env, **(env or {})}
        for key in drop:
            full_env.pop(key, None)
        proc = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                              env=full_env, input=stdin, timeout=60)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"stdout 應該只有一行 JSON：{proc.stdout!r} {proc.stderr!r}")
        out = json.loads(lines[0])
        self.assertEqual(out["ok"], proc.returncode == 0, out)
        return proc.returncode, out

    def posts(self, path: str) -> list:
        return [call for call in self.calls() if call[0] == "POST" and call[1] == path]

    def calls(self) -> list:
        return list(self.ctl.calls)


# --- flow-report ---------------------------------------------------------


class FlowReportTests(FlowSkillTestCase):
    PATH = "/api/flows/fl_1/steps/book_vehicle/result"
    OUTPUT = {"booking_id": 55, "vehicle_id": 2, "start_at": "2026-10-15T10:00:00+08:00",
              "end_at": "2026-10-15T23:59:00+08:00", "created": True}

    def setUp(self):
        super().setUp()
        self.flow = self.ctl.add_flow()
        self.output_file = self.write("output.json", self.OUTPUT)

    def report(self, *extra, env=None, drop=(), stdin=None, output_file=True, status="ok"):
        args = ["--flow", "fl_1", "--step", "book_vehicle", "--status", status,
                "--summary", "已代訂 ABC-1234", "--agent", "admin-agent"]
        if output_file:
            args += ["--output-file", self.output_file if output_file is True else output_file]
        return self.run_cli(REPORT, *args, *extra, env=env, drop=drop, stdin=stdin)

    def test_ok_posts_contract_body(self):
        code, out = self.report("--attempt", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["flow_id"], "fl_1")
        self.assertEqual(out["step"], "book_vehicle")
        self.assertEqual(out["flow_status"], "running")
        self.assertEqual(out["current_step"], "await_trip")
        posts = self.posts(self.PATH)
        self.assertEqual(len(posts), 1)
        _, _, _, body, headers = posts[0]
        self.assertEqual(headers["Authorization"], "Bearer tok-admin")
        self.assertEqual(body, {"status": "ok", "summary": "已代訂 ABC-1234", "output": self.OUTPUT,
                                "attempt": 1})

    def test_output_from_stdin(self):
        code, out = self.report(output_file="-", stdin=json.dumps(self.OUTPUT))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.posts(self.PATH)[0][3]["output"], self.OUTPUT)

    def test_failed_business_conflict(self):
        path = self.write("conflict.json", {"conflict": True, "http_status": 409, "vehicle_id": 2})
        code, out = self.report("--error-kind", "business", "--error", "ADM 回 409 時段衝突",
                                output_file=path, status="failed")
        self.assertEqual(code, 0, out)
        body = self.posts(self.PATH)[0][3]
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error_kind"], "business")
        self.assertEqual(body["error"], "ADM 回 409 時段衝突")
        self.assertTrue(body["output"]["conflict"])

    def test_failed_without_output_uses_summary_as_error(self):
        code, out = self.report(output_file=False, status="needs_human")
        self.assertEqual(code, 0, out)
        body = self.posts(self.PATH)[0][3]
        self.assertEqual(body, {"status": "needs_human", "summary": "已代訂 ABC-1234", "output": {}})

    def test_revision_conflict_is_retried(self):
        self.ctl.revision_conflicts = 2
        code, out = self.report()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["retried"])
        self.assertEqual(len(self.posts(self.PATH)), 3)

    def test_revision_conflict_persisting_is_reported(self):
        self.ctl.revision_conflicts = 10
        code, out = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["http_status"], 409)
        self.assertEqual(out["error"]["code"], "revision_conflict")
        self.assertTrue(out["error"]["retryable"])
        self.assertIn("已重送 3 次", out["error"]["message"])
        self.assertEqual(len(self.posts(self.PATH)), 4)

    def test_explicit_revision_conflict_is_not_retried(self):
        code, out = self.report("--revision", "1")
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "revision_conflict")
        self.assertFalse(out["error"]["retryable"])
        self.assertIn("--revision", out["error"]["hint"])
        self.assertEqual(len(self.posts(self.PATH)), 1)

    def test_not_assignee_403(self):
        code, out = self.run_cli(REPORT, "--flow", "fl_1", "--step", "book_vehicle", "--status", "ok",
                                 "--summary", "x", "--output-file", self.output_file, "--agent", "cs-agent")
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["http_status"], 403)
        self.assertEqual(out["error"]["code"], "not_assignee")
        self.assertFalse(out["error"]["retryable"])
        self.assertIn("hint", out["error"])

    def test_missing_output_fields_422(self):
        path = self.write("partial.json", {"booking_id": 55})
        code, out = self.report(output_file=path)
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["http_status"], 422)
        self.assertEqual(out["error"]["code"], "missing_output_fields")
        self.assertIn("vehicle_id", out["error"]["message"])
        self.assertFalse(out["error"]["retryable"])
        self.assertEqual(len(self.posts(self.PATH)), 1)
        # 補齊後可以再送（步驟狀態沒變）
        code, out = self.report()
        self.assertEqual(code, 0, out)

    def test_bad_token_401(self):
        code, out = self.report(env={"FLOW_CONTROLLER_TOKEN_ADMIN_AGENT": "wrong"})
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["http_status"], 401)
        self.assertEqual(out["error"]["code"], "unauthorized")

    def test_stale_attempt_409(self):
        code, out = self.report("--attempt", "0")
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "stale_attempt")
        self.assertEqual(out["error"]["http_status"], 409)

    def test_step_not_current_409(self):
        self.flow["current_step"] = "draft_vehicle"
        code, out = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "step_not_current")

    def test_server_503_is_retried(self):
        self.ctl.faults[("POST", self.PATH)] = [503, 502]
        code, out = self.report()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["retried"])
        self.assertEqual(len(self.posts(self.PATH)), 3)

    def test_dropped_connection_is_retried(self):
        self.ctl.faults[("POST", self.PATH)] = ["drop"]
        code, out = self.report()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["retried"])
        self.assertEqual(self.flow["steps"]["book_vehicle"]["status"], "succeeded")

    def test_lost_response_then_already_reported(self):
        # 第一次 controller 已處理，但回應遺失；重送得到 409 → 查稽核紀錄確認是自己的回報
        self.ctl.faults[("POST", self.PATH)] = ["drop_after"]
        code, out = self.report("--attempt", "1")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["already_reported"])
        self.assertEqual(len(self.flow["audit"]), 1)

    def test_duplicate_report_is_ok_but_different_status_is_not(self):
        self.assertEqual(self.report()[0], 0)
        code, out = self.report()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["already_reported"])
        code, out = self.report(output_file=False, status="needs_human")
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "step_not_current")

    def test_old_attempt_report_is_not_mistaken_for_this_one(self):
        # attempt 1 回報 failed 後 controller 重派 attempt 2；attempt 2 還沒回報、流程已被取消。
        # 這時 attempt 2 的 session 送 failed 得到 409，不能把 attempt 1 的舊回報當成「已回報」。
        self.flow["audit"].append({"action": "step.reported", "actor": "agent:admin-agent",
                                   "detail": {"step": "book_vehicle", "status": "failed", "attempt": 1}})
        self.flow["steps"]["book_vehicle"]["attempts"] = 2
        self.flow["status"] = "cancelled"
        code, out = self.report(output_file=False, status="failed")
        self.assertEqual(code, 1, out)
        self.assertEqual(out["error"]["code"], "flow_finished")
        self.assertNotIn("already_reported", out)
        code, out = self.report("--attempt", "1", output_file=False, status="failed")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["already_reported"])

    def test_unreachable_after_retries(self):
        code, out = self.report(env={"FLOW_CONTROLLER_URL": f"http://127.0.0.1:{free_port()}"})
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "unreachable")
        self.assertTrue(out["error"]["retryable"])

    def test_dry_run_needs_no_network_or_token(self):
        code, out = self.report("--dry-run", env={"FLOW_CONTROLLER_URL": f"http://127.0.0.1:{free_port()}"},
                                drop=("FLOW_CONTROLLER_TOKEN_ADMIN_AGENT",))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["request"]["method"], "POST")
        self.assertTrue(out["request"]["url"].endswith(self.PATH))
        self.assertEqual(out["request"]["body"]["output"], self.OUTPUT)
        self.assertEqual(out["token_env"], "FLOW_CONTROLLER_TOKEN_ADMIN_AGENT")
        self.assertFalse(out["token_set"])
        self.assertEqual(self.calls(), [])

    def test_usage_errors_exit_2(self):
        code, out = self.report(output_file=False)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "bad_input")
        code, out = self.report(output_file=self.write("bad.json", "{not json"))
        self.assertEqual(code, 2)
        code, out = self.report(output_file=self.write("list.json", [1, 2]))
        self.assertEqual(code, 2)
        self.assertIn("object", out["error"]["message"])
        code, out = self.report("--error-kind", "system")
        self.assertEqual(code, 2)
        code, out = self.report(drop=("FLOW_CONTROLLER_TOKEN_ADMIN_AGENT",))
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "no_token")
        self.assertIn("FLOW_CONTROLLER_TOKEN_ADMIN_AGENT", out["error"]["message"])
        self.assertEqual(self.calls(), [])

    def test_agent_from_env_and_api_suffix(self):
        code, out = self.run_cli(REPORT, "--flow", "fl_1", "--step", "book_vehicle", "--status", "ok",
                                 "--summary", "x", "--output-file", self.output_file,
                                 env={"OPENCLAW_AGENT_ID": "admin-agent",
                                      "FLOW_CONTROLLER_URL": self.ctl.url + "/api/"})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["agent"], "admin-agent")
        code, out = self.run_cli(REPORT, "--flow", "fl_1", "--step", "book_vehicle", "--status", "ok",
                                 "--summary", "x", "--output-file", self.output_file)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "no_agent")

    def test_token_and_url_from_secrets_file(self):
        secrets = os.path.join(self.tmp, ".openclaw", "secrets")
        os.makedirs(secrets)
        with open(os.path.join(secrets, "freeforjoy.env"), "w", encoding="utf-8") as fh:
            fh.write(f"# controller\nexport FLOW_CONTROLLER_TOKEN_ADMIN_AGENT='tok-admin'\n"
                     f"FLOW_CONTROLLER_URL=\"{self.ctl.url}\"\nFLOW_CONTROLLER_TOKEN_CS_AGENT=from-file\n")
        code, out = self.report(drop=("FLOW_CONTROLLER_TOKEN_ADMIN_AGENT", "FLOW_CONTROLLER_URL"))
        self.assertEqual(code, 0, out)
        # 既有環境變數優先，不被檔案覆蓋
        code, out = self.run_cli(START, "--type", "visit", "--input-file", self.write("in.json", VISIT_INPUT),
                                 "--agent", "cs-agent")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.posts("/api/flows")[0][4]["Authorization"], "Bearer tok-cs")


# --- flow-start ----------------------------------------------------------


class FlowStartTests(FlowSkillTestCase):
    def setUp(self):
        super().setUp()
        self.input_file = self.write("input.json", VISIT_INPUT)

    def start(self, *extra, agent="cs-agent", flow_type="visit", input_file=None, env=None, drop=(),
              stdin=None):
        return self.run_cli(START, "--type", flow_type, "--input-file", input_file or self.input_file,
                            "--agent", agent, *extra, env=env, drop=drop, stdin=stdin)

    def test_start_and_dedupe(self):
        code, out = self.start()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["created"])
        self.assertTrue(out["flow_id"].startswith("fl_"))
        self.assertEqual(out["flow_status"], "running")
        _, _, _, body, headers = self.posts("/api/flows")[0]
        self.assertEqual(body, {"type": "visit", "input": VISIT_INPUT})
        self.assertEqual(headers["Authorization"], "Bearer tok-cs")
        code, again = self.start()
        self.assertEqual(code, 0, again)
        self.assertFalse(again["created"])
        self.assertEqual(again["flow_id"], out["flow_id"])

    def test_input_from_stdin_with_title_and_dedupe_key(self):
        code, out = self.start("--dedupe-key", "demo:1", "--title", "示範", flow_type="demo",
                               input_file="-", stdin=json.dumps({"topic": "x"}))
        self.assertEqual(code, 0, out)
        body = self.posts("/api/flows")[0][3]
        self.assertEqual(body, {"type": "demo", "input": {"topic": "x"}, "title": "示範",
                                "dedupe_key": "demo:1"})

    def test_not_allowed_to_start_403(self):
        code, out = self.start(agent="admin-agent")
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["http_status"], 403)
        self.assertEqual(out["error"]["code"], "not_allowed_to_start")
        self.assertFalse(out["error"]["retryable"])
        self.assertIn("hint", out["error"])

    def test_invalid_input_422(self):
        code, out = self.start(input_file=self.write("bad.json", {"customer": {"name": "x"}}))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["http_status"], 422)
        self.assertEqual(out["error"]["code"], "invalid_input")
        self.assertIn("follow_up_id", out["error"]["message"])

    def test_unknown_flow_type_422(self):
        code, out = self.start(flow_type="refund")
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "unknown_flow_type")
        code, out = self.start(flow_type="Bad Type!")
        self.assertEqual(code, 2)

    def test_visit_retries_on_503_and_lost_response(self):
        self.ctl.faults[("POST", "/api/flows")] = [503, "drop_after"]
        code, out = self.start()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["retried"])
        self.assertFalse(out["created"])  # 第二次其實已建立，第三次回傳既有流程
        self.assertEqual(len(self.ctl.flows), 1)
        self.assertEqual(len(self.posts("/api/flows")), 3)

    def test_without_dedupe_key_no_retry_after_send(self):
        self.ctl.faults[("POST", "/api/flows")] = [503]
        code, out = self.start(flow_type="demo", input_file=self.write("demo.json", {"topic": "x"}))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["http_status"], 503)
        self.assertTrue(out["error"]["retryable"])
        self.assertEqual(len(self.posts("/api/flows")), 1)

    def test_connection_refused_is_retried_then_unreachable(self):
        code, out = self.start(env={"FLOW_CONTROLLER_URL": f"http://127.0.0.1:{free_port()}"})
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "unreachable")

    def test_dry_run(self):
        code, out = self.start("--dry-run", env={"FLOW_CONTROLLER_URL": f"http://127.0.0.1:{free_port()}"},
                               drop=("FLOW_CONTROLLER_TOKEN_CS_AGENT",))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["request"]["body"], {"type": "visit", "input": VISIT_INPUT})
        self.assertTrue(out["request"]["url"].endswith("/api/flows"))
        self.assertFalse(out["token_set"])
        self.assertEqual(self.calls(), [])

    def test_bad_input_file(self):
        code, out = self.start(input_file=self.write("arr.json", "[]"))
        self.assertEqual(code, 2)
        code, out = self.start(input_file=os.path.join(self.tmp, "missing.json"))
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "bad_input")
        self.assertEqual(self.calls(), [])


class FlowCommonCopiesTests(unittest.TestCase):
    def test_two_copies_are_identical(self):
        a = os.path.join(SKILLS, "flow-start", "scripts", "flow_common.py")
        b = os.path.join(SKILLS, "flow-report", "scripts", "flow_common.py")
        self.assertTrue(filecmp.cmp(a, b, shallow=False), "flow-start 與 flow-report 的 flow_common.py 不一致")


if __name__ == "__main__":
    unittest.main()
