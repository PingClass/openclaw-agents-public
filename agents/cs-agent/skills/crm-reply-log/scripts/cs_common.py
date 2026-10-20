"""cs-agent 的 CRM（service token）與 controller 共用 skill 工具。

cs-agent 的每個 skill（crm-visit-list、crm-visit-close、crm-intake、crm-reply-draft、crm-reply-log）
各放一份**完全相同**的副本（部署時每個 skill 目錄獨立複製，不能跨目錄 import）；改的時候全部一起改，
測試會檢查一致。底層的 HTTP 重試、時間、環境變數
沿用 `ffj_common.py`（與 skills/agent-crm 的副本相同）。

只用標準函式庫。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.parse

import ffj_common as c

DEFAULT_AGENT = "cs-agent"
DEFAULT_CRM_BASE = "http://localhost:18081/api"
DEFAULT_FLOW_SKILLS_DIR = "~/.openclaw/skills"
FLOW_SKILL_TIMEOUT = 120

VISIT_PREFIX = "[拜訪]"


def resolve_agent(arg: str | None) -> str:
    """--agent → OPENCLAW_AGENT_ID → cs-agent（這兩個 skill 只給 cs-agent 用）。"""
    return (arg or os.environ.get("OPENCLAW_AGENT_ID") or DEFAULT_AGENT).strip()


# --- CRM（service credential）--------------------------------------------


def crm_base() -> str:
    """CRM_API_BASE 可以是 …/api 或 …/api/v1，統一成 …/api（路徑一律帶 /v1）。"""
    base = c.env("CRM_API_BASE", DEFAULT_CRM_BASE).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


class Crm:
    """CRM service credential client：`Authorization: Bearer ${CRM_API_TOKEN}`。

    寫入一律帶呼叫端給的 Idempotency-Key（CRM 重送同鍵同內容會回第一次的結果），
    所以帶鍵的 POST 可以安全重試；GET 也會重試。
    """

    def __init__(self, token_env: str = "CRM_API_TOKEN"):
        token = c.env(token_env)
        if not token:
            raise c.ConfigError(f"沒有設定 {token_env}，無法呼叫 CRM")
        self.http = c.HttpClient(crm_base(), "CRM", {"Authorization": f"Bearer {token}"})

    def get(self, path: str):
        return self.http.request("GET", path)

    def get_ok(self, path: str):
        status, body = self.get(path)
        if status != 200:
            raise c.ServiceError(f"CRM GET {path} 失敗 {status}: {c.short(body)}",
                                 announce=status in (400, 401, 403), kind=f"crm_{status}")
        return body

    def post(self, path: str, body: dict, key: str):
        if not key:
            raise c.ConfigError(f"CRM POST {path} 缺少 Idempotency-Key")
        return self.http.request("POST", path, body, headers={"Idempotency-Key": key},
                                 idempotent=True)

    def paged(self, path: str, max_pages: int = 10) -> list:
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


# --- 拜訪待辦標題 ---------------------------------------------------------


class TitleError(ValueError):
    """拜訪待辦標題不是固定格式。"""


_SEP = re.compile(r"[｜|]")
_KV = re.compile(r"^\s*([^：:]+?)\s*[：:]\s*(.*?)\s*$")


def parse_visit_title(title) -> dict:
    """解析 agent-crm 建立的拜訪待辦標題（見 skills/agent-crm/references/api.md）：

        [拜訪] <客戶>｜地點：<地點>｜人數：<n> 人｜時長：<分鐘> 分鐘｜目的：<一句話>

    地點、人數、時長必填（派車要用）；目的選填。格式不合丟 TitleError（訊息是原因）。
    """
    if not isinstance(title, str) or not title.strip():
        raise TitleError("標題是空的")
    parts = [p.strip() for p in _SEP.split(title.strip())]
    head = parts[0]
    if not head.startswith(VISIT_PREFIX):
        raise TitleError(f"標題不是以「{VISIT_PREFIX}」開頭")
    fields: dict = {}
    for part in parts[1:]:
        m = _KV.match(part)
        if m:
            fields[m.group(1)] = m.group(2)
    out: dict = {"customer_name": head[len(VISIT_PREFIX):].strip() or None}

    location = (fields.get("地點") or "").strip()
    if not location:
        raise TitleError("標題缺少「地點：…」")
    out["location"] = location

    seats_m = re.match(r"^(\d+)\s*(人)?$", (fields.get("人數") or "").strip())
    if not seats_m:
        raise TitleError("標題缺少「人數：n 人」或格式不對")
    seats = int(seats_m.group(1))
    if not 1 <= seats <= 20:
        raise TitleError(f"人數 {seats} 不在 1～20")
    out["seats"] = seats

    minutes_m = re.match(r"^(\d+)\s*(分鐘|分)?$", (fields.get("時長") or "").strip())
    if not minutes_m:
        raise TitleError("標題缺少「時長：m 分鐘」或格式不對")
    minutes = int(minutes_m.group(1))
    if not 1 <= minutes <= 24 * 60:
        raise TitleError(f"時長 {minutes} 分鐘不合理")
    out["duration_minutes"] = minutes

    purpose = (fields.get("目的") or "").strip()
    out["subject"] = purpose or None
    return out


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


# --- controller 的步驟指令（visit.md「派工指令格式」）--------------------

STEP_HEADER = "[workflow-controller 步驟指令]"
_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_.\-]*)\s*:\s*(.*)$")


class StepError(Exception):
    """這一步的結果不是 ok：帶 flow-report 的 status 與 error_kind。"""

    def __init__(self, message: str, status: str = "needs_human", error_kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.error_kind = error_kind


def parse_step_message(text: str) -> dict:
    """解析 controller 的步驟指令（app/dispatch.py build_step_message）。

    回傳 flow_id、flow_type、step、attempt、skill、keys（"" 是主鍵，其他是 Idempotency-Key.<名稱>）、input。
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


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read()


def load_step_job(args, *, step: str, flow_type: str, skill: str, write_keys: tuple = (),
                  key_overrides: dict | None = None, required_inputs: tuple = ()) -> dict:
    """從 --message-file（步驟指令全文）或 --flow＋--input-file 組出 job，並檢查。

    write_keys：這一步要用的 Idempotency-Key.<名稱>；缺少時**不自行產生**，丟 ConfigError。
    key_overrides：{名稱: 值}，來自命令列（值必須是 controller 指令裡的）。
    """
    if getattr(args, "message_file", None):
        job = parse_step_message(_read_text(args.message_file))
    else:
        if not (getattr(args, "flow", None) and getattr(args, "input_file", None)):
            raise c.ConfigError("請給 --message-file（controller 的步驟指令），或 --flow 加 --input-file")
        try:
            data = json.loads(_read_text(args.input_file))
        except ValueError as exc:
            raise c.ConfigError(f"輸入 JSON 解析失敗：{exc}") from exc
        job = {"flow_id": args.flow, "flow_type": flow_type, "step": step,
               "attempt": getattr(args, "attempt", None), "skill": skill, "keys": {}, "input": data}
    for name, value in (key_overrides or {}).items():
        if value:
            job["keys"][name] = value

    if not job.get("flow_id"):
        raise c.ConfigError("步驟指令缺少 flow_id；沒有 flow_id 的流程指令一律不執行")
    if job.get("step") != step:
        raise c.ConfigError(f"這個 skill 只處理 {step}，指令的 step 是 {job.get('step') or '（沒有）'}")
    if job.get("flow_type") not in (None, flow_type):
        raise c.ConfigError(f"這個 skill 只處理 {flow_type} 流程，指令的 flow_type 是 {job['flow_type']}")
    for name in write_keys:
        if not job["keys"].get(name):
            # 不得自己產生 Idempotency-Key（AGENTS.md「工作流協作」）
            raise c.ConfigError(f"步驟指令缺少 Idempotency-Key.{name}，不自行產生，請 controller 重派")
    if not isinstance(job["input"], dict):
        raise c.ConfigError("步驟輸入不是 JSON object")
    for field in required_inputs:
        if job["input"].get(field) in (None, ""):
            raise c.ConfigError(f"輸入缺少 {field}")
    return job


def classify_crm(status: int, body, what: str) -> StepError:
    """CRM 回應 → 這一步要回報的狀態。5xx／429 交給 controller 重試，其他交給人。"""
    if status == 409:
        return StepError(f"{what}：CRM 回 409，Idempotency-Key 已用於不同內容，需要人確認：{c.short(body)}")
    if status in (401, 403):
        return StepError(f"{what}：CRM 拒絕（{status}），檢查 CRM_API_TOKEN 與 scope：{c.short(body)}")
    if status == 404:
        return StepError(f"{what}：CRM 找不到資料（404）：{c.short(body)}")
    if status >= 500 or status == 429:
        return StepError(f"{what}：CRM 回 {status}", status="failed", error_kind="system")
    return StepError(f"{what}：CRM 回 {status}：{c.short(body)}")


def body_text(body) -> str:
    return json.dumps(body, ensure_ascii=False) if body is not None else ""


def report_step(job: dict, agent: str, step_result: dict) -> dict:
    """用共用 skill flow-report 回報 {status, error_kind, summary, output}；帶上指令的 attempt。"""
    args = ["--flow", job["flow_id"], "--step", job["step"], "--status", step_result["status"],
            "--summary", step_result["summary"], "--output-file", "-", "--agent", agent]
    if step_result.get("error_kind"):
        args += ["--error-kind", step_result["error_kind"]]
    if job.get("attempt") is not None:
        # controller 已重派時，過期的回報會被拒（409 stale_attempt），不會蓋掉新的結果
        args += ["--attempt", str(job["attempt"])]
    return run_flow_skill("flow-report", "flow_report.py", args, step_result["output"])


def finish_step(args, job: dict, agent: str, step_result: dict, result: dict, ok_outcome: str) -> bool:
    """把這一步的結果放進輸出；有 --report 就用 flow-report 回報。回傳 exit 是否成功。"""
    result.update(report_status=step_result["status"], error_kind=step_result.get("error_kind"),
                  summary=step_result["summary"], output=step_result["output"])
    result["outcome"] = ok_outcome if step_result["status"] == "ok" else f"step_{step_result['status']}"
    result["detail"] = step_result["summary"]
    if not getattr(args, "report", False):
        result["next"] = ("用 flow-report 回報：--flow {f} --step {s} --status {st}{ek}{at} "
                          "--summary <summary> --output-file <output 存成的檔案>").format(
            f=job["flow_id"], s=job["step"], st=step_result["status"],
            ek=f" --error-kind {step_result['error_kind']}" if step_result.get("error_kind") else "",
            at=f" --attempt {job['attempt']}" if job.get("attempt") is not None else "")
        return step_result["status"] == "ok"
    res = report_step(job, agent, step_result)
    out = res.get("result") or {}
    if res["rc"] == 0 and out.get("ok"):
        result.update(reported=True, flow_status=out.get("flow_status"))
        return True
    result.update(reported=False, announce=True, announce_kind="error",
                  report_error=out.get("error") or out.get("detail") or res.get("stderr") or f"rc={res['rc']}",
                  detail=f"{step_result['summary']}；但 flow-report 回報失敗，請重跑本指令（CRM 寫入會冪等重播）")
    return False


# --- 金額（和 controller app/flows/refund.py 的 amounts_in 相同規則）------

# NT$1,200、NTD 1200、新台幣 1200、$1200、1,200 元、1200 塊（全形數字與符號先轉半形）
_AMOUNT_IN_TEXT = re.compile(r"(?:NT\$|NTD|新台幣|台幣|\$)\s*([\d,]+)|([\d,]+)\s*(?:元|塊)")


def amounts_in(text) -> list:
    found = []
    for match in _AMOUNT_IN_TEXT.finditer(unicodedata.normalize("NFKC", text or "")):
        digits = (match.group(1) or match.group(2) or "").replace(",", "")
        if digits.isdigit():
            found.append(int(digits))
    return found


def twd(amount) -> str:
    return f"NT${amount:,}" if isinstance(amount, int) and not isinstance(amount, bool) else f"NT${amount}"


def one_line(text) -> str:
    """多行收成一行（；分隔），去掉多餘空白。"""
    if not isinstance(text, str):
        return ""
    parts = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
    return "；".join(parts)
