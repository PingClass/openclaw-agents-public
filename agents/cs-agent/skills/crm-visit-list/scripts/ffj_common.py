"""FreeForJoy 員工模擬 skill 的共用工具（agent-crm、agent-adm、agent-inbox 共用）。

三個 skill 各自放一份完全相同的副本（部署時每個 skill 目錄是獨立複製的，
不能跨目錄 import）。改這個檔案時三份要一起改；`skills/tests` 有測試檢查三份一致。

只用標準函式庫：cron 叫起的 agent 環境 PATH 不固定，不能假設 requests 裝得到。
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from datetime import time as dtime

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Taipei")
except Exception:  # noqa: BLE001 - 沒有 tzdata 時退回固定 +08:00（台灣沒有日光節約）
    from datetime import timezone

    TZ = timezone(timedelta(hours=8))

# --- 員工 agent 的身分（計畫 9.3 方案甲）--------------------------------
# 帳號名稱＝agent id；HRM 員工 UUID 不是憑證，可以寫在程式裡。
AGENTS = {
    "mjolmire-businessman": {
        "hrm_employee_id": "da1b72d2-89cb-4879-9c50-73e03a1ae993",
        "department": "業務部",
        "title": "業務專員",
        "role": "sales",
    },
    "gobta": {
        "hrm_employee_id": "3c75cb71-5e9c-4cc7-8bce-dcc79817299f",
        "department": "業務部",
        "title": "業務專員",
        "role": "sales",
    },
    "main": {
        "hrm_employee_id": "85f9387a-6a8f-4283-92dc-71ec63de4447",
        "department": "業務部",
        "title": "業務主管",
        "role": "sales_manager",
    },
    "hinata": {
        "hrm_employee_id": "db57df3a-d001-416b-a5aa-1add62e69144",
        "department": "客服部",
        "title": "客服主管",
        "role": "cs_manager",
    },
    "souei": {
        "hrm_employee_id": "895ced65-5f19-4b5b-a8b3-46529c931c1d",
        "department": "資訊部",
        "title": "IT 人員",
        "role": "it",
    },
    "luminous": {
        "hrm_employee_id": "6ad36d41-7561-41c5-bf1c-9f7a42ee6b2a",
        "department": "管理部",
        "title": "行政財會",
        "role": "admin_finance",
    },
    "shion": {
        "hrm_employee_id": "3296d2ef-5716-419a-8064-458e54ac7b17",
        "department": "人資部",
        "title": "人資",
        "role": "hr",
    },
}

DEFAULT_ENV_FILE = "~/.openclaw/secrets/freeforjoy.env"
TIMEOUT = 20

# 測試會換掉這兩個，讓重試不用真的等。
SLEEP = time.sleep
RETRY_DELAYS = (2, 5, 10)

# 隨機來源集中在這裡，--seed 與測試都改這一個。
RNG = random.Random()


# --- 環境變數 -----------------------------------------------------------


def load_env_file(path: str | None = None) -> None:
    """把 secrets 檔裡「還沒設定」的變數補進環境變數。

    憑證一律從環境變數讀；cron 叫起的 agent 不一定繼承得到部署時 source 過的變數，
    所以這裡補讀 `~/.openclaw/secrets/freeforjoy.env`（KEY=VALUE 格式）。
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


def agent_env_suffix(agent: str) -> str:
    """agent id → 環境變數尾碼：轉大寫、- 換成 _。"""
    return agent.upper().replace("-", "_")


def controller_base() -> str:
    return env("FLOW_CONTROLLER_URL", "http://127.0.0.1:18090").rstrip("/")


def controller_token(agent: str) -> str:
    name = f"FLOW_CONTROLLER_TOKEN_{agent_env_suffix(agent)}"
    token = env(name)
    if not token:
        raise ConfigError(f"沒有設定 {name}，無法呼叫 controller")
    return token


# --- 錯誤 ---------------------------------------------------------------


class ServiceError(RuntimeError):
    """呼叫系統失敗。

    announce=False 代表基礎設施問題（後端沒開、逾時），cron 記成 error 就好，
    不要七隻 agent 同時在頻道喊「連不上」；announce=True 才是需要人去看的問題。
    """

    def __init__(self, message: str, announce: bool = False, kind: str = "error"):
        super().__init__(message)
        self.announce = announce
        self.kind = kind


class ConfigError(ServiceError):
    """設定問題（缺帳號、缺密碼、缺 token）：一定要讓人知道。"""

    def __init__(self, message: str):
        super().__init__(message, announce=True, kind="config_error")


class ScheduleError(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, announce=True, kind="schedule_failed")


# --- HTTP ---------------------------------------------------------------


def _is_connect_error(exc: BaseException) -> bool:
    """連線根本沒建立（請求一定沒送出），任何方法重試都安全。"""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


class HttpClient:
    """帶 cookie 的 JSON client，負責重試。

    重試規則：
    - 連線被拒、DNS 失敗：任何方法都重試（請求沒送出去）。
    - 逾時、502/503/504、429：只有 idempotent=True 的請求重試
      （GET，或伺服器端有 Idempotency-Key 保護的 POST）。
    """

    def __init__(self, base: str, name: str, headers: dict | None = None):
        self.base = base.rstrip("/")
        self.name = name
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.default_headers = dict(headers or {})

    def cookie(self, name: str) -> str | None:
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def _once(self, method: str, url: str, body, headers: dict):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in {**self.default_headers, **headers}.items():
            if v is not None:
                req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, _parse_json(raw)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            return exc.code, _parse_json(raw, fallback=True)

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
                raise ServiceError(f"連不上 {self.name}（{method} {url}）：{exc}") from exc
            if status in (429, 502, 503, 504) and idempotent and attempt < attempts - 1:
                SLEEP(RETRY_DELAYS[attempt])
                continue
            return status, parsed
        raise ServiceError(f"連不上 {self.name}（{method} {url}）：{last_exc}")


def _parse_json(raw: str, fallback: bool = False):
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw[:300]} if fallback else {"raw": raw[:300]}


def short(body) -> str:
    return json.dumps(body, ensure_ascii=False)[:300]


def error_code(body) -> str | None:
    """controller 的錯誤格式 {"detail": {"code": ...}}。"""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("code")
    return None


# --- 時間 ---------------------------------------------------------------


def now_tz(override: str | None = None) -> datetime:
    if override:
        return parse_dt(override)
    return datetime.now(TZ)


def parse_dt(value) -> datetime | None:
    """解析 ISO 時間；沒有時區的當成台北時間。失敗回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def at(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, dtime(h, m), tzinfo=TZ)


def is_workday(day: date) -> bool:
    """週一到週五算工作日。國定假日不在這裡判斷（排程本身只在週一到週五跑）。"""
    return day.weekday() < 5


def add_workdays(day: date, n: int) -> date:
    current = day
    added = 0
    while added < n:
        current += timedelta(days=1)
        if is_workday(current):
            added += 1
    return current


def iso(value: datetime) -> str:
    return value.astimezone(TZ).replace(microsecond=0).isoformat()


# --- 狀態檔 -------------------------------------------------------------


def state_root() -> str:
    return os.path.expanduser(env("OPENCLAW_STATE_DIR", "~/.openclaw/state"))


def state_path(skill: str, agent: str, name: str) -> str:
    return os.path.join(state_root(), skill, agent, name)


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {} if default is None else default


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def stable_key(*parts) -> str:
    """由內容算出固定的 Idempotency-Key（≤128 字元）。"""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return digest[:40]


# --- 排程 ---------------------------------------------------------------


def schedule_once(*, agent: str, name: str, declaration_key: str, when: datetime,
                  message: str, timeout_seconds: int = 300) -> str:
    """建一個一次性的 openclaw cron job（跑完自動刪除）。

    刻意由腳本自己呼叫 CLI，不交給 agent 下指令：排程時間是決策的一部分，
    不該讓模型有機會抄錯。declaration-key 讓同一件事重排時不會建出第二份。
    結果一律 --no-deliver：要不要發訊息由 agent 依 announce 欄位決定。
    """
    argv = [
        env("OPENCLAW_BIN", "openclaw"), "cron", "add",
        "--name", name,
        "--declaration-key", declaration_key,
        "--agent", agent,
        "--session", "isolated",
        "--at", iso(when),
        "--delete-after-run",
        "--no-deliver",
        "--timeout-seconds", str(timeout_seconds),
        "--message", message,
        "--json",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScheduleError(f"排程「{name}」失敗：{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise ScheduleError(f"排程「{name}」失敗（rc={proc.returncode}）：{proc.stderr[:200]}")
    return iso(when)


# --- 輸出 ---------------------------------------------------------------


def emit(payload: dict, ok: bool) -> None:
    """輸出單一行 JSON 並結束。成功 exit 0，失敗 exit 1（cron 會記成 error）。"""
    payload["ok"] = ok
    payload.setdefault("announce", False)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0 if ok else 1)


def resolve_agent(arg: str | None) -> str:
    agent = (arg or os.environ.get("OPENCLAW_AGENT_ID") or "").strip()
    if agent not in AGENTS:
        raise ConfigError(
            f"agent「{agent or '(未指定)'}」不是員工 agent。可用：{', '.join(sorted(AGENTS))}"
        )
    return agent


def run_main(handler, result: dict) -> None:
    """包住子指令：把所有例外都轉成一行 JSON。"""
    try:
        ok = handler()
        emit(result, ok)
    except ServiceError as exc:
        result.update(
            outcome=exc.kind,
            announce=exc.announce,
            announce_kind="error",
            error=str(exc),
        )
        emit(result, False)
    except Exception as exc:  # noqa: BLE001 - cron 環境不能讓 traceback 吃掉輸出
        result.update(
            outcome="error",
            announce=True,
            announce_kind="error",
            error=f"{type(exc).__name__}: {exc}",
        )
        emit(result, False)
