"""hr-agent 的 HRM（service token）、controller 步驟指令與共用 skill 工具。

onboarding-list、onboarding-notice 各放一份**完全相同**的副本（部署時每個 skill 目錄獨立複製，
不能跨目錄 import）；改的時候兩份一起改，測試會檢查一致。底層的 HTTP 重試、時間、環境變數
沿用 `ffj_common.py`（與 skills/agent-crm 的副本相同）。

只用標準函式庫。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse

import ffj_common as c

DEFAULT_AGENT = "hr-agent"
DEFAULT_HRM_BASE = "http://localhost:18000/api"
DEFAULT_FLOW_SKILLS_DIR = "~/.openclaw/skills"
FLOW_SKILL_TIMEOUT = 120

STEP_HEADER = "[workflow-controller 步驟指令]"
_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_.\-]*)\s*:\s*(.*)$")


def resolve_agent(arg: str | None) -> str:
    """--agent → OPENCLAW_AGENT_ID → hr-agent（這兩個 skill 只給 hr-agent 用）。"""
    return (arg or os.environ.get("OPENCLAW_AGENT_ID") or DEFAULT_AGENT).strip()


# --- HRM（service token，唯讀）--------------------------------------------


def hrm_base() -> str:
    """HRM_API_BASE 寫 http://host:18000 或 …/api 都可以，統一成 …/api。"""
    base = c.env("HRM_API_BASE", DEFAULT_HRM_BASE).rstrip("/")
    if not base.endswith("/api"):
        base = f"{base}/api"
    return base


class Hrm:
    """HRM service token client：`Authorization: Bearer ${HRM_API_TOKEN}`。

    token 只有 employee:read、department:read（HRM docs/agent-service-api.md），只會發 GET；
    GET 遇到連線失敗、逾時、429、502～504 會自動重試（ffj_common.HttpClient）。
    """

    def __init__(self, token_env: str = "HRM_API_TOKEN"):
        token = c.env(token_env)
        if not token:
            raise c.ConfigError(f"沒有設定 {token_env}，無法呼叫 HRM")
        self.http = c.HttpClient(hrm_base(), "HRM", {"Authorization": f"Bearer {token}"})

    def get(self, path: str):
        return self.http.request("GET", path)

    def get_ok(self, path: str):
        status, body = self.get(path)
        if status != 200:
            raise c.ServiceError(f"HRM GET {path} 失敗 {status}: {c.short(body)}",
                                 announce=status in (400, 401, 403), kind=f"hrm_{status}")
        return body

    def paged(self, path: str, max_pages: int = 20) -> list:
        out: list = []
        body = self.get_ok(path)
        for _ in range(max_pages):
            if isinstance(body, list):
                out.extend(body)
                break
            out.extend((body or {}).get("results") or [])
            nxt = (body or {}).get("next")
            if not nxt:
                break
            parsed = urllib.parse.urlsplit(nxt)
            rel = parsed.path.split("/api", 1)[-1]
            if parsed.query:
                rel = f"{rel}?{parsed.query}"
            body = self.get_ok(rel)
        return out


# --- controller 的步驟指令 ---------------------------------------------------


def parse_step_message(text: str) -> dict:
    """解析 controller 的步驟指令（controller/backend/app/dispatch.py build_step_message）。

    回傳 {flow_id, flow_type, step, attempt, skill, keys, input}；keys[""] 是 Idempotency-Key，
    keys["<名稱>"] 是 Idempotency-Key.<名稱>。
    """
    if not text or STEP_HEADER not in text:
        raise c.ConfigError(f"輸入不是 controller 的步驟指令（缺少「{STEP_HEADER}」標頭）")
    body = text[text.index(STEP_HEADER) + len(STEP_HEADER):]
    lines = body.splitlines()
    header: dict = {}
    keys: dict = {}
    i = 0
    while i < len(lines) and lines[i].strip() != "---":
        m = _HEADER_LINE.match(lines[i].strip())
        if m:
            name, value = m.group(1), m.group(2).strip()
            if name == "Idempotency-Key":
                keys[""] = value
            elif name.startswith("Idempotency-Key."):
                keys[name[len("Idempotency-Key."):]] = value
            else:
                header[name] = value
        i += 1
    rest = "\n".join(lines[i + 1:])
    marker = "輸入（JSON）："
    if marker not in rest:
        raise c.ConfigError("步驟指令裡沒有「輸入（JSON）：」")
    chunk = rest.split(marker, 1)[1]
    json_lines = []
    for line in chunk.splitlines():
        if line.strip() == "---":
            break
        json_lines.append(line)
    try:
        data = json.loads("\n".join(json_lines))
    except ValueError as exc:
        raise c.ConfigError(f"步驟指令的輸入 JSON 解析失敗：{exc}") from exc
    attempt = header.get("attempt")
    return {
        "flow_id": header.get("flow_id"),
        "flow_type": header.get("flow_type"),
        "step": header.get("step"),
        "attempt": int(attempt) if attempt and attempt.isdigit() else None,
        "skill": header.get("skill"),
        "keys": keys,
        "input": data,
    }


# --- 共用 skill（flow-start、flow-report）--------------------------------


def flow_skill_path(skill: str, script: str) -> str:
    root = c.env("FLOW_SKILLS_DIR", DEFAULT_FLOW_SKILLS_DIR)
    return os.path.join(os.path.expanduser(root), skill, "scripts", script)


def _last_json(text: str):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def run_flow_skill(skill: str, script: str, args: list, stdin_json) -> dict:
    """呼叫 ~/.openclaw/skills/<skill>/scripts/<script>，stdin 傳 JSON，回傳它印出的 JSON。

    回傳 {"rc": int, "result": dict|None, "stderr": str}；找不到腳本丟 ConfigError。
    """
    path = flow_skill_path(skill, script)
    if not os.path.isfile(path):
        raise c.ConfigError(f"找不到共用 skill {skill}（{path}），請先部署到 ~/.openclaw/skills")
    try:
        proc = subprocess.run(
            [sys.executable, path, *args],
            input=json.dumps(stdin_json, ensure_ascii=False),
            capture_output=True, text=True, timeout=FLOW_SKILL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"rc": -1, "result": None, "stderr": f"{type(exc).__name__}: {exc}"}
    return {"rc": proc.returncode, "result": _last_json(proc.stdout), "stderr": proc.stderr[-500:]}
