"""flow-start、flow-report 共用的 controller client。

兩個 skill 各放一份**完全相同**的副本（部署時每個 skill 目錄獨立複製到 ~/.openclaw/skills/，
不能跨目錄 import，也不依賴 skills/agent-*）。改的時候兩份一起改；
`skills/tests/test_flow.py` 有測試檢查兩份一致。

只用標準函式庫：cron 或 controller 叫起的 agent 環境 PATH 不固定，不能假設 requests 裝得到。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENV_FILE = "~/.openclaw/secrets/freeforjoy.env"
DEFAULT_CONTROLLER = "http://127.0.0.1:18090"
TIMEOUT = 20
SLEEP = time.sleep

# 網路錯誤重試的等待秒數（共 1 + len 次）。FLOW_SKILL_RETRY_DELAYS="0,0,0" 可覆寫（測試用）。
DEFAULT_RETRY_DELAYS = (2, 5, 10)

AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# exit code：0 成功；1 controller 拒絕或連不上；2 參數、設定、輸入檔錯誤（呼叫端要改）
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

# controller 錯誤碼 → 給 agent 的下一步建議。retry=True 代表「稍後原樣再送一次是安全的」。
HINTS = {
    "unauthorized": ("controller 不認這把 token（沒設定、錯誤或已撤銷）。不要重試，回報給人處理。", False),
    "forbidden": ("controller 拒絕這個操作。不要重試，回報給人處理。", False),
    "not_allowed_to_start": ("這個 agent 不能啟動這種流程（只有流程定義的 starters 可以）。不要重試，回報給人。", False),
    "unknown_flow_type": ("沒有這種流程類型（visit／refund／onboarding）。檢查 --type。", False),
    "invalid_input": ("流程輸入不符合契約（缺欄位或格式錯）。照 message 修正 input 後再送。", False),
    "validation_error": ("請求格式錯誤（欄位型別、長度）。照 errors 修正後再送。", False),
    "missing_output_fields": ("status=ok 時 output 缺少必填欄位，步驟狀態沒有改變。補齊欄位後用同一個指令再送一次。", False),
    "revision_conflict": ("流程同時被其他操作更新。稍後原樣再送一次即可（報告沒有被接受）。", True),
    "not_assignee": ("這一步不是指派給這個 agent。檢查 --agent、--flow、--step 是否照步驟指令填寫；不要重試。", False),
    "step_not_current": ("流程目前不在這一步（可能已回報過、被取消或已往下走）。不要重送。", False),
    "step_not_running": ("這一步目前不接受回報（可能已回報過或已被重派）。不要重送。", False),
    "stale_attempt": ("這次派工已過期（controller 已重派新的一次）。不要重送；新的派工會另外送來。", False),
    "use_events": ("這一步不用 flow-report，要用 POST /api/flows/{id}/events 回報（例如 agent-crm report-trip）。", False),
    "flow_finished": ("流程已經結束，不接受回報。不要重送。", False),
    "flow_not_found": ("找不到這個 flow_id。檢查是否照步驟指令填寫。", False),
    "step_not_found": ("這條流程沒有這個步驟名稱。檢查 --step 是否照步驟指令填寫。", False),
    "not_involved": ("這個 agent 沒有參與這條流程，不能查詢。", False),
    "server_error": ("controller 內部錯誤。稍後再試一次；持續發生就回報給人。", True),
    "unreachable": ("連不上 controller（已重試）。稍後再試；持續發生就回報給人。", True),
}


class SkillError(Exception):
    """參數或設定錯誤：呼叫端要修正，不要原樣重試。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Unreachable(Exception):
    """重試後仍連不上 controller。"""


# --- 環境變數 -----------------------------------------------------------


def load_env_file(path: str | None = None) -> None:
    """把 secrets 檔裡「還沒設定」的變數補進環境變數（和 skills/agent-* 的 ffj_common.py 相同做法）。

    cron 或 controller 叫起的 agent 不一定繼承得到部署時 source 過的變數，所以補讀
    `~/.openclaw/secrets/freeforjoy.env`（KEY=VALUE，可有 export 與引號）。
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


def retry_delays() -> tuple:
    raw = env("FLOW_SKILL_RETRY_DELAYS")
    if not raw:
        return DEFAULT_RETRY_DELAYS
    try:
        return tuple(float(x) for x in raw.split(",") if x.strip())
    except ValueError:
        return DEFAULT_RETRY_DELAYS


def resolve_agent(arg: str | None) -> str:
    agent = (arg or os.environ.get("OPENCLAW_AGENT_ID") or "").strip()
    if not agent:
        raise SkillError("no_agent", "沒有指定 agent：請帶 --agent <agent id>，或設定 OPENCLAW_AGENT_ID")
    if not AGENT_ID_RE.match(agent):
        raise SkillError("bad_agent", f"agent id 格式不對：{agent!r}")
    return agent


def token_env_name(agent: str) -> str:
    """agent id → token 環境變數名稱：FLOW_CONTROLLER_TOKEN_<大寫、- 換 _>。"""
    return "FLOW_CONTROLLER_TOKEN_" + agent.upper().replace("-", "_")


def controller_token(agent: str) -> str:
    name = token_env_name(agent)
    token = env(name)
    if not token:
        raise SkillError("no_token", f"沒有設定 {name}（也不在 ~/.openclaw/secrets/freeforjoy.env），無法呼叫 controller")
    return token


def controller_base() -> str:
    """FLOW_CONTROLLER_URL 寫成 http://host:18090 或 http://host:18090/api 都可以。"""
    base = env("FLOW_CONTROLLER_URL", DEFAULT_CONTROLLER).rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def api_url(*parts: str) -> str:
    quoted = "/".join(urllib.parse.quote(str(p), safe="") for p in parts)
    return f"{controller_base()}/api/{quoted}"


# --- 輸入 ---------------------------------------------------------------


def read_json_object(path: str | None, what: str, *, default: dict | None = None) -> dict:
    """讀 JSON 檔（- 代表 stdin），必須是 JSON object。"""
    if path is None:
        if default is not None:
            return dict(default)
        raise SkillError("bad_input", f"需要 {what}")
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        raise SkillError("bad_input", f"讀不到 {what}（{path}）：{exc}") from exc
    if not text.strip():
        if default is not None:
            return dict(default)
        raise SkillError("bad_input", f"{what} 是空的（{path}）")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SkillError("bad_input", f"{what} 不是合法的 JSON（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        raise SkillError("bad_input", f"{what} 必須是 JSON object（{{...}}），收到 {type(data).__name__}")
    return data


# --- HTTP ---------------------------------------------------------------


def _is_connect_error(exc: BaseException) -> bool:
    """連線根本沒建立（請求一定沒送出），任何請求都可以安全重試。"""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def _parse_json(raw: str):
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw[:300]}


def _once(method: str, url: str, token: str, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, _parse_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        finally:
            exc.close()
        return exc.code, _parse_json(raw)


def request(method: str, url: str, token: str, body=None, *, idempotent: bool) -> dict:
    """送一次請求，網路錯誤時重試。

    - 連線被拒、DNS 失敗：一律重試（請求沒送出去）。
    - 逾時、連線中斷、502／503／504、429：只有 idempotent=True 時重試（可能已經送到 controller）。

    回傳 {"status", "body", "uncertain"}；uncertain=True 代表先前有一次「可能已送到」的請求，
    呼叫端遇到「已經處理過」的錯誤時要再確認。重試後仍連不上時丟 Unreachable。
    """
    delays = retry_delays()
    attempts = len(delays) + 1
    uncertain = False
    last_error = ""
    for attempt in range(attempts):
        try:
            status, parsed = _once(method, url, token, body)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
            connect = _is_connect_error(exc)
            if not connect:
                uncertain = True
            if (connect or idempotent) and attempt < attempts - 1:
                SLEEP(delays[attempt])
                continue
            raise Unreachable(f"連不上 controller（{method} {url}）：{last_error}") from exc
        if status in (429, 502, 503, 504):
            uncertain = uncertain or status != 429
            if idempotent and attempt < attempts - 1:
                SLEEP(delays[attempt])
                continue
        return {"status": status, "body": parsed, "uncertain": uncertain, "tries": attempt + 1}
    raise Unreachable(f"連不上 controller（{method} {url}）：{last_error}")


def error_detail(status: int, body) -> dict:
    """把 controller 的錯誤（{"detail": {"code", "message", "errors"?}}）整理成固定欄位。"""
    code = None
    message = None
    errors = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message")
        errors = detail.get("errors")
    elif isinstance(detail, str):
        message = detail
    if not code:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 422: "validation_error"}.get(
            status, "server_error" if status >= 500 else f"http_{status}")
    out = {"http_status": status, "code": code, "message": message or f"controller 回 HTTP {status}"}
    if errors:
        out["errors"] = [
            {"loc": e.get("loc"), "msg": e.get("msg")} if isinstance(e, dict) else e for e in errors
        ][:10]
    hint, retryable = HINTS.get(code, ("", status >= 500))
    if hint:
        out["hint"] = hint
    out["retryable"] = retryable
    return out


# --- 輸出 ---------------------------------------------------------------


def emit(payload: dict, code: int) -> None:
    """stdout 輸出一行 JSON 並結束。"""
    payload["ok"] = code == EXIT_OK
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def run(handler, result: dict) -> None:
    """包住主程式：所有例外都轉成一行 JSON，不讓 traceback 吃掉輸出。"""
    try:
        code = handler()
    except SkillError as exc:
        result.update(error={"code": exc.code, "message": exc.message, "retryable": False})
        code = EXIT_USAGE
    except Unreachable as exc:
        hint, _ = HINTS["unreachable"]
        result.update(error={"http_status": None, "code": "unreachable", "message": str(exc),
                             "hint": hint, "retryable": True})
        code = EXIT_FAILED
    except Exception as exc:  # noqa: BLE001
        result.update(error={"code": "internal_error", "message": f"{type(exc).__name__}: {exc}",
                             "retryable": False})
        code = EXIT_FAILED
    emit(result, code)


def token_status(agent: str) -> dict:
    """--dry-run 用：只說 token 有沒有設定，不輸出任何內容。"""
    name = token_env_name(agent)
    return {"token_env": name, "token_set": bool(env(name))}
