"""admin-agent／it-agent 流程 skill 的共用程式。

使用者：admin-agent 的 adm-vehicle-draft／adm-vehicle-book／adm-vehicle-trip（流程 ①）、
adm-room-book（流程 ③），以及 it-agent 的 laptop-allocation-draft（流程 ③）。
每個 skill 各自放一份**完全相同**的副本（部署時每個 skill 目錄可能被獨立複製，
不能跨目錄 import）。改這個檔案時所有副本要一起改；
`agents/admin-agent/skills/_testkit` 的測試會檢查副本一致。

負責：
- 解析 controller 的步驟指令（`[workflow-controller 步驟指令]`，見 controller/backend/docs/flows/visit.md）
- 補讀 `~/.openclaw/secrets/freeforjoy.env`（不覆蓋既有環境變數）
- 呼叫 ADM（Bearer token、重試）
- 用 flow-report skill 回報步驟結果（`--report`）
- 輸出一行 JSON

只用標準函式庫：cron／controller 叫起的 agent 環境 PATH 不固定，不能假設 requests 裝得到。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Taipei")
except Exception:  # noqa: BLE001 - 沒有 tzdata 時退回固定 +08:00（台灣沒有日光節約）
    from datetime import timezone

    TZ = timezone(timedelta(hours=8))

DEFAULT_ENV_FILE = "~/.openclaw/secrets/freeforjoy.env"
DEFAULT_ADM_BASE = "http://localhost:18001/api/v1"
DEFAULT_FLOW_REPORT = "~/.openclaw/skills/flow-report/scripts/flow_report.py"
DEFAULT_AGENT = "admin-agent"
TIMEOUT = 20
REPORT_TIMEOUT = 120

# 測試會換掉這兩個，讓重試不用真的等。
SLEEP = time.sleep
RETRY_DELAYS = (2, 5, 10)

STATUSES = ("ok", "failed", "needs_human")


# --- 環境變數 -----------------------------------------------------------


def load_env_file(path: str | None = None) -> None:
    """把 secrets 檔裡「還沒設定」的變數補進環境變數（KEY=VALUE 格式）。

    已經存在的環境變數優先，不會被檔案覆蓋。設 FREEFORJOY_ENV_FILE= （空字串）可關閉。
    """
    if path is None:
        path = os.environ.get("FREEFORJOY_ENV_FILE", DEFAULT_ENV_FILE)
    if not path:
        return
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def adm_base() -> str:
    return env("ADM_API_BASE", DEFAULT_ADM_BASE).rstrip("/")


# --- 錯誤 ---------------------------------------------------------------


class ConfigError(RuntimeError):
    """設定問題（缺 token 等）：要人處理。"""


class UsageError(RuntimeError):
    """指令本身有問題（沒有 flow_id、步驟不對）：無法回報給 controller。"""


class NotDispatched(UsageError):
    """controller 那邊這一步不是目前派給這個 agent、正在執行的步驟：不寫入、不回報。"""


class ConnectionFailed(RuntimeError):
    """連不上 ADM（重試後仍失敗）。"""


# --- HTTP ---------------------------------------------------------------


def _is_connect_error(exc: BaseException) -> bool:
    """連線根本沒建立（請求一定沒送出），任何方法重試都安全。"""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def _parse_json(raw: str):
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw[:300]}


class AdmClient:
    """帶 Bearer token 的 ADM JSON client，負責重試。

    重試規則（和 skills/agent-adm 的 ffj_common 一致）：
    - 連線被拒、DNS 失敗：任何方法都重試（請求沒送出去）。
    - 逾時、502/503/504、429：只有 idempotent=True 的請求重試
      （GET，或伺服器端有 Idempotency-Key 保護的 POST）。
    """

    def __init__(self, base: str, token: str, label: str = "ADM"):
        self.base = base.rstrip("/")
        self.token = token
        self.label = label
        self.opener = urllib.request.build_opener()

    def _once(self, method: str, url: str, body, headers: dict):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            if v is not None:
                req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, _parse_json(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            return exc.code, _parse_json(raw)

    def request(self, method: str, path: str, body=None, headers: dict | None = None,
                idempotent: bool | None = None):
        if idempotent is None:
            idempotent = method.upper() in ("GET", "HEAD")
        url = path if path.startswith("http") else f"{self.base}{path}"
        attempts = len(RETRY_DELAYS) + 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                status, parsed = self._once(method, url, body, headers or {})
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if (_is_connect_error(exc) or idempotent) and attempt < attempts - 1:
                    SLEEP(RETRY_DELAYS[attempt])
                    continue
                raise ConnectionFailed(f"連不上 {self.label}（{method} {url}）：{exc}") from exc
            if status in (429, 502, 503, 504) and idempotent and attempt < attempts - 1:
                SLEEP(RETRY_DELAYS[attempt])
                continue
            return status, parsed
        raise ConnectionFailed(f"連不上 {self.label}（{method} {url}）：{last_exc}")


def adm_client(token_env: str = "ADM_API_TOKEN", owner: str = "admin-agent") -> AdmClient:
    """ADM agent token client。admin-agent 用 ADM_API_TOKEN；it-agent 用 ADM_API_TOKEN_IT（assets:read）。"""
    token = env(token_env)
    if not token:
        raise ConfigError(f"沒有設定 {token_env}（{owner} 的 ADM agent token），無法呼叫 ADM")
    return AdmClient(adm_base(), token)


def detail_text(body) -> str:
    """ADM（DRF）錯誤的可讀文字：{"detail": "..."} 或欄位錯誤 {"odometer": ["..."]}。"""
    if body is None:
        return ""
    if isinstance(body, dict):
        if isinstance(body.get("detail"), str):
            return body["detail"]
        parts = []
        for key, value in body.items():
            if isinstance(value, list):
                value = "；".join(str(v) for v in value)
            parts.append(f"{key}: {value}")
        return "，".join(parts)[:300]
    if isinstance(body, list):
        return "；".join(str(v) for v in body)[:300]
    return str(body)[:300]


# --- 寫入前向 controller 確認 ----------------------------------------------

DEFAULT_CONTROLLER = "http://127.0.0.1:18090"
FLOW_ACTIVE = ("running", "waiting_approval")
STEP_ACTIVE = ("dispatched", "running")


def controller_base() -> str:
    """FLOW_CONTROLLER_URL 寫成 http://host:18090 或 http://host:18090/api 都可以（和 flow-report 一致）。"""
    base = env("FLOW_CONTROLLER_URL", DEFAULT_CONTROLLER).rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def controller_token(agent: str) -> str:
    name = "FLOW_CONTROLLER_TOKEN_" + agent.upper().replace("-", "_")
    token = env(name)
    if not token:
        raise ConfigError(f"沒有設定 {name}，無法向 controller 確認步驟")
    return token


def verify_dispatch(ctx: dict, fields: tuple) -> dict | None:
    """代訂／代登記（寫入 ADM）之前，向 controller 確認這一步真的是它派給這個 agent 的。

    步驟指令只是一段文字，任何人都能在對話裡貼一份假的；所以寫入前用 admin-agent 自己的
    controller token `GET /api/flows/{flow_id}` 確認：
    - 流程進行中、目前步驟就是這一步、指派給這個 agent、狀態是已派工／執行中、attempt 相同
      → 不符合丟 NotDispatched（不寫入、不回報：controller 也不會收這份回報）
    - controller 保存的這一步輸入（step.input.input）和指令的輸入在 `fields` 上一致
      → 不一致回傳 needs_human 結果（指令可能被改過，交給人）
    確認通過回傳 None。連不上 controller 丟 ConnectionFailed（failed／system）。
    """
    agent = ctx.get("agent") or DEFAULT_AGENT
    client = AdmClient(controller_base(), controller_token(agent), label="controller")
    flow_id = ctx["flow_id"]
    status, body = client.request("GET", f"/api/flows/{urllib.parse.quote(flow_id, safe='')}")
    if status >= 500 or status == 429:
        raise ConnectionFailed(f"controller 查流程回 {status}，無法確認步驟")
    if status != 200 or not isinstance(body, dict):
        raise NotDispatched(f"controller 查不到流程 {flow_id} 或不允許 {agent} 查詢（HTTP {status}），"
                            "不是 controller 派的步驟，不執行")
    step = ctx["step"]
    if body.get("status") not in FLOW_ACTIVE or body.get("current_step") != step:
        raise NotDispatched(f"流程 {flow_id} 目前不在 {step}（狀態 {body.get('status')}、目前步驟 "
                            f"{body.get('current_step')}），不執行")
    row = next((s for s in body.get("steps") or [] if isinstance(s, dict) and s.get("name") == step), None)
    if row is None or row.get("assignee") != agent or row.get("status") not in STEP_ACTIVE:
        raise NotDispatched(f"流程 {flow_id} 的 {step} 沒有派給 {agent} 或不在執行中，不執行")
    attempt = ctx.get("attempt")
    if attempt is not None and row.get("attempts") != attempt:
        raise NotDispatched(f"指令的 attempt {attempt} 已過期（controller 目前 {row.get('attempts')}），不執行")
    expected = (row.get("input") or {}).get("input")
    if not isinstance(expected, dict):
        return result("needs_human", f"controller 沒有 {step} 的輸入可以核對，不寫入，請人工確認")
    given = ctx.get("input") or {}
    diff = [k for k in fields if given.get(k) != expected.get(k)]
    if diff:
        return result("needs_human",
                      f"指令的輸入和 controller 派工時的不一致（{', '.join(diff)}），不寫入，請人工確認",
                      {"mismatched_fields": diff})
    return None


# --- 時間 ---------------------------------------------------------------


def parse_dt(value) -> datetime | None:
    """解析 ISO 時間；沒有時區的當成台北時間。失敗回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed


def local_text(value) -> str:
    """給人看的台北時間（10/15 10:00）。"""
    parsed = parse_dt(value)
    if parsed is None:
        return str(value)
    local = parsed.astimezone(TZ)
    return f"{local.month}/{local.day} {local:%H:%M}"


def strict_int(value) -> int | None:
    """JSON 整數（不接受 bool、浮點、字串）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# --- 步驟指令 -------------------------------------------------------------


HEADER = "[workflow-controller 步驟指令]"
INPUT_MARK = "輸入（JSON）："


def parse_instruction(text: str) -> dict:
    """解析 controller 的步驟指令（app/dispatch.py 的 build_step_message）。

    回傳 {flow_id, flow_type, step, attempt, idempotency_key, write_keys, skill, input}。
    標頭在第一個 `---` 之前；輸入 JSON 在「輸入（JSON）：」之後、下一個單獨一行的 `---` 之前。
    """
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == HEADER), None)
    if start is None:
        raise UsageError(f"不是 controller 的步驟指令（找不到「{HEADER}」）")
    result: dict = {"write_keys": {}, "input": None}
    i = start + 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i].strip()
        i += 1
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key == "flow_id":
            result["flow_id"] = value
        elif key == "flow_type":
            result["flow_type"] = value
        elif key == "step":
            result["step"] = value
        elif key == "attempt":
            try:
                result["attempt"] = int(value)
            except ValueError:
                raise UsageError(f"attempt 不是整數：{value}") from None
        elif key == "Idempotency-Key":
            result["idempotency_key"] = value
        elif key.startswith("Idempotency-Key."):
            result["write_keys"][key[len("Idempotency-Key."):]] = value
        elif key == "skill":
            result["skill"] = value
    mark = next((j for j in range(i, len(lines)) if lines[j].strip() == INPUT_MARK), None)
    if mark is not None:
        end = next((j for j in range(mark + 1, len(lines)) if lines[j].strip() == "---"), len(lines))
        raw = "\n".join(lines[mark + 1:end]).strip()
        try:
            result["input"] = json.loads(raw) if raw else None
        except ValueError as exc:
            raise UsageError(f"輸入（JSON）解析失敗：{exc}") from None
    if not result.get("flow_id"):
        raise UsageError("步驟指令沒有 flow_id；沒有 flow_id 的流程指令視為一般對話，不執行")
    return result


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read()


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    src = parser.add_argument_group("步驟指令（擇一：整份指令，或分開給）")
    src.add_argument("--instruction-file", help="controller 的整份步驟指令（檔案路徑，- 代表 stdin）")
    src.add_argument("--flow", help="flow_id")
    src.add_argument("--attempt", type=int, help="指令裡的 attempt")
    src.add_argument("--idempotency-key", help="指令裡的 Idempotency-Key（寫入步驟必填）")
    src.add_argument("--input-file", help="指令裡「輸入（JSON）」的內容（檔案路徑，- 代表 stdin）")
    src.add_argument("--input-json", help="同上，直接給 JSON 字串")
    parser.add_argument("--agent", help="回報用的 agent id（預設 OPENCLAW_AGENT_ID，再預設這個 skill 的 agent）")
    parser.add_argument("--report", action="store_true",
                        help="做完直接呼叫 flow-report 回報（建議一律加上）")
    parser.add_argument("--dry-run", action="store_true", help="不連網、不寫入、不回報，只顯示會做什麼")
    return parser


def build_context(args, step: str) -> dict:
    """由參數組出步驟內容：{flow_id, step, attempt, idempotency_key, input}。"""
    if args.instruction_file:
        ctx = parse_instruction(_read_text(args.instruction_file))
    else:
        ctx = {"flow_id": args.flow, "step": step, "write_keys": {}, "input": None}
    # 分開給的參數優先（方便手動重跑）
    if args.flow:
        ctx["flow_id"] = args.flow
    if args.attempt is not None:
        ctx["attempt"] = args.attempt
    if args.idempotency_key:
        ctx["idempotency_key"] = args.idempotency_key
    if args.input_file:
        try:
            ctx["input"] = json.loads(_read_text(args.input_file))
        except ValueError as exc:
            raise UsageError(f"--input-file 不是 JSON：{exc}") from None
    elif args.input_json:
        try:
            ctx["input"] = json.loads(args.input_json)
        except ValueError as exc:
            raise UsageError(f"--input-json 不是 JSON：{exc}") from None
    if not ctx.get("flow_id"):
        raise UsageError("沒有 flow_id（用 --instruction-file 給整份指令，或 --flow）")
    if ctx.get("step") and ctx["step"] != step:
        raise UsageError(f"這個 skill 只處理 step={step}，指令是 step={ctx['step']}")
    ctx["step"] = step
    if not isinstance(ctx.get("input"), dict):
        ctx["input"] = None
    return ctx


# --- 步驟結果與回報 -------------------------------------------------------


def result(status: str, summary: str, output: dict | None = None,
           error_kind: str | None = None) -> dict:
    assert status in STATUSES
    if status == "failed" and error_kind not in ("business", "system"):
        error_kind = "system"
    if status != "failed":
        error_kind = None
    return {"status": status, "summary": summary, "output": output or {}, "error_kind": error_kind}


def resolve_agent(arg: str | None, default: str = DEFAULT_AGENT) -> str:
    return (arg or env("OPENCLAW_AGENT_ID") or default).strip()


def flow_report_argv(ctx: dict, step_result: dict, agent: str) -> list:
    script = os.path.expanduser(env("FLOW_REPORT_SCRIPT", DEFAULT_FLOW_REPORT))
    argv = [sys.executable or "python3", script,
            "--flow", ctx["flow_id"], "--step", ctx["step"],
            "--status", step_result["status"]]
    if step_result.get("error_kind"):
        argv += ["--error-kind", step_result["error_kind"]]
    if ctx.get("attempt") is not None:
        # 過期的回報 controller 會回 409 stale_attempt（visit.md 建議帶上）
        argv += ["--attempt", str(ctx["attempt"])]
    argv += ["--summary", step_result["summary"], "--output-file", "-", "--agent", agent]
    return argv


def send_report(ctx: dict, step_result: dict, agent: str) -> dict:
    """呼叫 flow-report skill；回傳它印出的 JSON。失敗丟 RuntimeError。"""
    argv = flow_report_argv(ctx, step_result, agent)
    if not os.path.exists(argv[1]):
        raise RuntimeError(f"找不到 flow-report 腳本：{argv[1]}（可用 FLOW_REPORT_SCRIPT 指定）")
    try:
        proc = subprocess.run(argv, input=json.dumps(step_result["output"], ensure_ascii=False),
                              capture_output=True, text=True, timeout=REPORT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"flow-report 執行失敗：{type(exc).__name__}: {exc}") from exc
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    parsed = None
    if lines:
        try:
            parsed = json.loads(lines[-1])
        except ValueError:
            parsed = None
    if proc.returncode != 0 or not isinstance(parsed, dict) or not parsed.get("ok"):
        detail = (lines[-1] if lines else proc.stderr.strip())[:300]
        raise RuntimeError(f"flow-report 回報失敗（rc={proc.returncode}）：{detail}")
    return parsed


def emit(payload: dict, code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def run_step(step: str, description: str, handler, argv=None, add_args=None,
             default_agent: str = DEFAULT_AGENT) -> None:
    """skill 腳本的共用 main。

    handler(ctx, args) -> 步驟結果（result(...)）。handler 可以丟：
    - ConfigError：回報 needs_human（要人補設定）
    - ConnectionFailed：回報 failed／system（controller 會重試同一步）
    輸出一行 JSON；腳本本身完成（含回報成功）exit 0，指令錯誤或回報失敗 exit 1。
    """
    parser = build_parser(description)
    if add_args:
        add_args(parser)
    args = parser.parse_args(argv)
    load_env_file()
    agent = resolve_agent(args.agent, default_agent)
    payload: dict = {"step": step, "dry_run": bool(args.dry_run)}
    try:
        ctx = build_context(args, step)
    except (UsageError, OSError) as exc:
        payload.update(ok=False, error=str(exc), reported=False)
        emit(payload, 1)
        return
    payload.update(flow_id=ctx["flow_id"], attempt=ctx.get("attempt"))
    ctx["agent"] = agent
    try:
        step_result = handler(ctx, args)
    except NotDispatched as exc:
        payload.update(ok=False, error=str(exc), reported=False)
        emit(payload, 1)
        return
    except ConfigError as exc:
        step_result = result("needs_human", f"設定問題：{exc}")
    except ConnectionFailed as exc:
        step_result = result("failed", str(exc)[:300], error_kind="system")
    except Exception as exc:  # noqa: BLE001 - 不能讓 traceback 吃掉輸出
        step_result = result("failed", f"skill 腳本錯誤：{type(exc).__name__}: {exc}"[:300],
                             error_kind="system")
    payload.update(step_status=step_result["status"], error_kind=step_result["error_kind"],
                   summary=step_result["summary"], output=step_result["output"])
    if args.dry_run or not args.report:
        payload.update(ok=True, reported=False)
        if not args.dry_run:
            payload["next"] = "尚未回報：加上 --report 重跑，或自己用 flow-report 回報上面的 step_status／summary／output"
        emit(payload, 0)
        return
    try:
        payload["report"] = send_report(ctx, step_result, agent)
    except RuntimeError as exc:
        payload.update(ok=False, reported=False, report_error=str(exc),
                       next="回報失敗：同樣的指令加 --report 重跑（這一步可以安全重跑）")
        emit(payload, 1)
        return
    payload.update(ok=True, reported=True)
    emit(payload, 0)
