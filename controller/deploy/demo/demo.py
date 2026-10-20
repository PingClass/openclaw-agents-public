#!/usr/bin/env python3
"""課堂 demo（P6）：流程 ① 客戶拜訪派車報帳，10 分鐘內從觸發跑到報帳核准。

在 gateway 主機上跑（講師的筆電只開前端）。步驟：

  check    課前檢查：gateway readyz、controller healthz、admin 登入、腳本是否就位；--ping 叫 3 隻助理 agent 回 PONG
  prepare  把確認者（main、gobta）切成「真人確認」；以 gobta 身分在 CRM 建一筆「今天、約 20 分鐘後開始」的拜訪待辦
  start    用 openclaw agent 叫 cs-agent 執行 crm-visit-list（--date 今天 --demo --follow-up <id>），
           印出 flow_id 與前端網址（示範模式 ?demo=1）。cs-agent 失敗時可以加 --no-agent 由 admin 直接啟動（備援）
  trip     拜訪時段內（開始前 15 分鐘起）以 gobta 執行 agent-crm 的 report-trip --flow <id>，回報取車／還車里程
  status   看目前的示範流程走到哪、確認者是不是真人確認
  reset    課後：把確認者切回「agent 代理」，必要時取消還沒跑完的示範流程（--cancel）

每一步都會印出講師在前端要做什麼。所有子指令都有 --dry-run：只印出會做的事，不連網、不寫任何東西。
完整的講師手冊：docs/classroom-demo.md。

憑證一律從環境變數讀（另外補讀 ~/.openclaw/secrets/freeforjoy.env，已設定的環境變數優先）：

  FLOW_CONTROLLER_URL              controller API（預設 http://127.0.0.1:18090）
  FLOW_CONTROLLER_PUBLIC_URL       前端網址（印給講師的連結；預設同 FLOW_CONTROLLER_URL）
  FLOW_CONTROLLER_ADMIN_USER       controller 的 admin 帳號（預設 teacher）
  FLOW_CONTROLLER_ADMIN_PASSWORD   admin 密碼
  DEMO_CRM_SCRIPT                  agent-crm 的 crm.py（預設 ~/.openclaw/skills/agent-crm/scripts/crm.py）
  DEMO_VISIT_LIST_SCRIPT           cs-agent 的 crm_visit_list.py（寫進給 cs-agent 的訊息；預設在 workspace-cs-agent）
  DEMO_OPENCLAW_BIN                openclaw 執行檔（預設 PATH 上的 openclaw，再找 /opt/homebrew/bin/openclaw）
  DEMO_STATE_DIR                   示範狀態檔目錄（預設 ~/.openclaw/state/classroom-demo）
  FREEFORJOY_ENV_FILE              secrets 檔路徑（設成空字串可關閉補讀）

只用標準函式庫。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
DEFAULT_ENV_FILE = "~/.openclaw/secrets/freeforjoy.env"
TERMINAL = {"succeeded", "failed", "expired", "cancelled"}

# 示範拜訪：今天、約 20 分鐘後開始。ADM 從預約開始前 15 分鐘起才接受登記取車，
# 講師核准、admin-agent 代訂完（約 5 分鐘）時剛好進入可以回報里程的時段。
DEFAULT_LEAD_MINUTES = 20
DEFAULT_DURATION = 60
PICKUP_OPEN_BEFORE = timedelta(minutes=15)
# controller 的示範模式只接受「今天、3 小時內開始」的拜訪（visit.DEMO_MAX_LEAD）
DEMO_MAX_LEAD = timedelta(hours=3)


class DemoError(Exception):
    pass


# ---------------------------------------------------------------- 輸出


def say(text: str = "") -> None:
    print(text, flush=True)


def step_title(text: str) -> None:
    say()
    say(f"== {text} ==")


def teacher(lines: list[str]) -> None:
    """講師在前端要做的事（醒目一點，投影前講師自己看）。"""
    say()
    say("▶ 講師在前端要做的事：")
    for index, line in enumerate(lines, 1):
        say(f"   {index}. {line}")


# ---------------------------------------------------------------- 設定


def load_env_file() -> None:
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
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def api_base() -> str:
    return env("FLOW_CONTROLLER_URL", "http://127.0.0.1:18090").rstrip("/")


def public_base() -> str:
    return env("FLOW_CONTROLLER_PUBLIC_URL", api_base()).rstrip("/")


def crm_script() -> str:
    return os.path.expanduser(env("DEMO_CRM_SCRIPT", "~/.openclaw/skills/agent-crm/scripts/crm.py"))


def visit_list_script() -> str:
    return os.path.expanduser(env(
        "DEMO_VISIT_LIST_SCRIPT", "~/.openclaw/workspace-cs-agent/skills/crm-visit-list/scripts/crm_visit_list.py"))


def openclaw_bin() -> str:
    configured = env("DEMO_OPENCLAW_BIN")
    if configured:
        return os.path.expanduser(configured)
    return shutil.which("openclaw") or "/opt/homebrew/bin/openclaw"


def state_path() -> str:
    return os.path.join(os.path.expanduser(env("DEMO_STATE_DIR", "~/.openclaw/state/classroom-demo")), "state.json")


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    path = state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_tpe(args) -> datetime:
    if getattr(args, "now", None):
        parsed = datetime.fromisoformat(args.now)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=TPE)
    return datetime.now(TPE)


def hm(value: datetime) -> str:
    return value.astimezone(TPE).strftime("%H:%M")


# ---------------------------------------------------------------- controller（admin 登入）


class Controller:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> tuple[int, object]:
        url = f"{self.base}/api{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=15) as response:  # noqa: S310
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw.decode(errors="replace")
        except urllib.error.URLError as exc:
            raise DemoError(f"連不上 controller（{self.base}）：{exc.reason}") from exc

    def must(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        status, data = self.call(method, path, body, params)
        if status >= 300:
            raise DemoError(f"controller {method} {path} 失敗 {status}：{data}")
        return data

    def login(self) -> None:
        user = env("FLOW_CONTROLLER_ADMIN_USER", "teacher")
        password = env("FLOW_CONTROLLER_ADMIN_PASSWORD")
        if not password:
            raise DemoError("沒有設定 FLOW_CONTROLLER_ADMIN_PASSWORD（controller 的 admin 密碼）")
        status, data = self.call("POST", "/auth/login", {"username": user, "password": password})
        if status != 200:
            raise DemoError(f"controller admin 登入失敗 {status}：{data}")
        if not isinstance(data, dict) or data.get("role") != "admin":
            raise DemoError(f"{user} 不是 admin 帳號，不能切換確認者")

    def set_mode(self, recipient: str, mode: str) -> dict:
        return self.must("PUT", "/inbox/confirm-mode", {"mode": mode}, {"recipient": recipient})

    def flow(self, flow_id: str) -> dict:
        return self.must("GET", f"/flows/{urllib.parse.quote(flow_id)}")


def admin_controller() -> Controller:
    ctl = Controller(api_base())
    ctl.login()
    return ctl


# ---------------------------------------------------------------- 子行程


def run_json(cmd: list[str], *, timeout: float, stdin: str | None = None) -> tuple[int, dict, str]:
    """執行會輸出單行 JSON 的腳本；回傳 (exit code, JSON, 原始輸出)。"""
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise DemoError(f"找不到 {cmd[0]}：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DemoError(f"{' '.join(cmd[:3])} 超過 {int(timeout)} 秒沒有結束") from exc
    out = proc.stdout.strip()
    parsed: dict = {}
    for line in reversed(out.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
            break
    if not parsed:
        try:
            whole = json.loads(out) if out else {}
            parsed = whole if isinstance(whole, dict) else {}
        except json.JSONDecodeError:
            parsed = {}
    return proc.returncode, parsed, out + (("\n" + proc.stderr.strip()) if proc.stderr.strip() else "")


def show_cmd(cmd: list[str]) -> str:
    return " ".join(cmd)


# ---------------------------------------------------------------- 網址


def urls(state: dict) -> dict:
    base = public_base()
    flow_id = state.get("flow_id")
    out = {
        "overview": f"{base}/?demo=1",
        "flow": f"{base}/flows/{flow_id}?demo=1" if flow_id else None,
    }
    for key in ("sales", "manager"):
        person = (state.get("confirmers") or {}).get(key) or {}
        recipient = person.get("hrm_employee_id") or person.get("agent_id") or ""
        out[f"inbox_{key}"] = f"{base}/inbox?recipient={urllib.parse.quote(recipient)}" if recipient else f"{base}/inbox"
    return out


def person_name(state: dict, key: str, fallback: str) -> str:
    person = (state.get("confirmers") or {}).get(key) or {}
    return person.get("display_name") or fallback


# ---------------------------------------------------------------- prepare


def visit_time(now: datetime, lead_minutes: int) -> datetime:
    """約 lead 分鐘後，進位到 5 分鐘。"""
    target = now + timedelta(minutes=lead_minutes)
    minute = int(math.ceil(target.minute / 5.0) * 5)
    rounded = target.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    return rounded


def cmd_prepare(args) -> int:
    now = now_tpe(args)
    start = visit_time(now, args.lead_minutes)
    if start.date() != now.date() or start + timedelta(minutes=args.duration) > start.replace(hour=23, minute=59):
        raise DemoError("太晚了：示範拜訪要排在今天之內（拜訪結束不能超過 23:59）")
    if args.lead_minutes < 1 or start - now > DEMO_MAX_LEAD:
        raise DemoError("--lead-minutes 要在 1～180 之間：controller 的示範模式只接受 3 小時內開始的拜訪")
    if args.duration < 1:
        raise DemoError("--duration 要是正整數（分鐘）")
    crm_cmd = [sys.executable, crm_script(), "visit", "--agent", args.sales, "--days", "0",
               "--time", hm(start), "--duration", str(args.duration), "--seats", str(args.seats), "--no-schedule"]
    if args.customer:
        crm_cmd += ["--customer", args.customer]

    step_title("prepare：準備示範資料")
    old = load_state()
    if old.get("flow_id") and old.get("flow_status") not in TERMINAL and not args.dry_run:
        say(f"注意：上一次的示範流程 {old['flow_id']} 可能還沒結束（{old.get('flow_status') or '未知'}）。"
            "要清掉請先跑 reset --cancel。")

    if args.dry_run:
        say(f"[dry-run] controller {api_base()}：以 {env('FLOW_CONTROLLER_ADMIN_USER', 'teacher')} 登入")
        for agent in (args.manager, args.sales):
            say(f"[dry-run] PUT /api/inbox/confirm-mode?recipient={agent}  {{\"mode\": \"human\"}}")
        say(f"[dry-run] 以 {args.sales} 身分建立拜訪待辦：{show_cmd(crm_cmd)}")
        say(f"[dry-run] 拜訪時間 {hm(start)}–{hm(start + timedelta(minutes=args.duration))}，狀態檔 {state_path()}")
        state = {"confirmers": {"sales": {"agent_id": args.sales}, "manager": {"agent_id": args.manager}}}
    else:
        ctl = admin_controller()
        confirmers = {}
        for key, agent in (("manager", args.manager), ("sales", args.sales)):
            view = ctl.set_mode(agent, "human")
            recipient = view.get("recipient") or {}
            confirmers[key] = {"agent_id": agent, "hrm_employee_id": recipient.get("hrm_employee_id"),
                               "display_name": recipient.get("display_name") or agent}
            say(f"✔ {confirmers[key]['display_name']}（{agent}）改成真人確認"
                + ("" if view.get("changed") else "（原本就是）"))

        code, result, raw = run_json(crm_cmd, timeout=120)
        if code != 0 or not result.get("follow_up_id"):
            raise DemoError(f"建立拜訪待辦失敗（exit {code}）：{result.get('error') or result.get('detail') or raw}")
        state = {
            "created_at": now.isoformat(),
            "sales_agent": args.sales,
            "manager_agent": args.manager,
            "confirmers": confirmers,
            "follow_up_id": result["follow_up_id"],
            "customer": result.get("customer"),
            "location": result.get("location"),
            "seats": result.get("seats") or args.seats,
            "visit_start_at": result.get("due_at") or start.isoformat(),
            "duration_minutes": result.get("duration_minutes") or args.duration,
            "title": result.get("title"),
        }
        save_state(state)
        say(f"✔ CRM 拜訪待辦 {state['follow_up_id']}：{state['customer']}，"
            f"{hm(datetime.fromisoformat(state['visit_start_at']))} 開始、{state['duration_minutes']} 分鐘"
            f"（{result.get('outcome')}）")

    links = urls(state)
    teacher([
        f"投影電腦開 {links['overview']}（總覽，示範模式字放大），用 admin 帳號登入",
        f"操作用另一個瀏覽器或無痕視窗（不開示範模式）登入，開收件匣 {links['inbox_sales']}，"
        "確認上方顯示「確認方式：真人確認」",
        f"切到 {person_name(state, 'manager', args.manager)} 的收件匣也應該是「真人確認」",
        "準備好就跑：demo.py start",
    ])
    return 0


# ---------------------------------------------------------------- start


def agent_message(state: dict, today: str) -> str:
    return "\n".join([
        "用 crm-visit-list skill 啟動課堂示範的拜訪派車流程（只處理講師指定的這一筆）。",
        "",
        f"執行：python3 {visit_list_script()} --agent cs-agent --date {today} --demo --follow-up {state['follow_up_id']}",
        "",
        "照 skill 的規則處理輸出：最後用一句話回報 detail 和啟動的 flow_id，不要發其他訊息。",
    ])


def openclaw_reply(result: dict) -> str:
    inner = result.get("result") if isinstance(result.get("result"), dict) else {}
    texts = [p.get("text") for p in inner.get("payloads") or [] if isinstance(p, dict) and p.get("text")]
    if texts:
        return "\n".join(texts)
    meta = inner.get("meta") if isinstance(inner.get("meta"), dict) else {}
    return str(meta.get("finalAssistantVisibleText") or "")


def find_demo_flow(ctl: Controller, follow_up_id, *, wait: float) -> dict | None:
    deadline = time.monotonic() + wait
    while True:
        listing = ctl.must("GET", "/flows", params={"type": "visit", "limit": 30})
        for item in listing.get("items") or []:
            if not item.get("demo"):
                continue
            detail = ctl.flow(item["id"])
            if str(((detail.get("state") or {}).get("input") or {}).get("follow_up_id")) == str(follow_up_id):
                return detail
        if time.monotonic() >= deadline:
            return None
        time.sleep(3)


def direct_input(state: dict) -> dict:
    start = datetime.fromisoformat(state["visit_start_at"])
    end = start + timedelta(minutes=int(state.get("duration_minutes") or DEFAULT_DURATION))
    sales = (state.get("confirmers") or {}).get("sales") or {}
    if not sales.get("hrm_employee_id"):
        raise DemoError("狀態檔沒有業務的 hrm_employee_id，請重跑 prepare")
    return {
        "follow_up_id": state["follow_up_id"],
        "customer": {"name": state.get("customer") or "示範客戶", "external_id": None},
        "visit_start_at": start.isoformat(),
        "visit_end_at": end.isoformat(),
        "location": state.get("location") or "示範地點",
        "seats": int(state.get("seats") or 1),
        "assignee_hrm_employee_id": sales["hrm_employee_id"],
        "subject": "課堂示範",
        "demo": True,
    }


def cmd_start(args) -> int:
    now = now_tpe(args)
    state = load_state()
    step_title("start：觸發流程 ①（模擬 08:00 的 cron 叫 cs-agent 掃拜訪）")
    if not state.get("follow_up_id"):
        if not args.dry_run:
            raise DemoError("找不到示範資料，請先跑 demo.py prepare")
        state = {"follow_up_id": "<prepare 建立的待辦 id>", "confirmers": {}}
    today = now.date().isoformat()

    if args.dry_run:
        if args.no_agent:
            say("[dry-run] 不叫 cs-agent，以 admin 直接 POST /api/flows（type=visit、input.demo=true）")
        else:
            say(f"[dry-run] {openclaw_bin()} agent --agent cs-agent --session-id demo-<時間> "
                f"--message-file <暫存檔> --timeout {args.timeout} --json")
            say("[dry-run] 訊息內容：")
            for line in agent_message(state, today).splitlines():
                say(f"           {line}")
        say("[dry-run] 之後以 admin 查 GET /api/flows?type=visit，找 input.follow_up_id 相同的示範流程")
    else:
        ctl = admin_controller()
        if args.no_agent:
            say("備援：不叫 cs-agent，以 admin 帳號直接啟動示範流程")
            flow = ctl.must("POST", "/flows", {"type": "visit", "input": direct_input(state)})
            say(f"✔ 已啟動 {flow['id']}（created={flow.get('created')}）")
        else:
            fd, path = tempfile.mkstemp(prefix="demo-msg-", suffix=".txt")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(agent_message(state, today))
                cmd = [openclaw_bin(), "agent", "--agent", "cs-agent",
                       "--session-id", f"classroom-demo-{now.strftime('%Y%m%d%H%M%S')}",
                       "--message-file", path, "--timeout", str(args.timeout), "--json"]
                say(f"叫 cs-agent 執行 crm-visit-list（最多等 {args.timeout} 秒）…")
                code, result, raw = run_json(cmd, timeout=args.timeout + 60)
            finally:
                os.unlink(path)
            if code != 0 or result.get("status") != "ok":
                say(f"✘ openclaw agent 失敗（exit {code}，status={result.get('status')}）：{raw[-500:]}")
                say("  備援：demo.py start --no-agent（以 admin 直接啟動同一筆示範流程）")
                return 1
            reply = openclaw_reply(result).strip()
            if reply:
                say(f"cs-agent：{reply[:500]}")
        detail = find_demo_flow(ctl, state["follow_up_id"], wait=args.wait)
        if detail is None:
            say("✘ 找不到這筆拜訪的示範流程。看 cs-agent 的回覆；必要時 demo.py start --no-agent")
            return 1
        state.update(flow_id=detail["id"], flow_status=detail["status"], started_at=now.isoformat())
        save_state(state)
        say(f"✔ flow_id：{detail['id']}「{detail['title']}」，目前在「{detail.get('current_step_title')}」")

    links = urls(state)
    sales = person_name(state, "sales", state.get("sales_agent") or args.sales)
    teacher([
        f"投影：{links['flow'] or '<流程詳情>?demo=1'}（流程詳情，示範模式）",
        "講：cs-agent 找到今天的拜訪、啟動流程 → controller 解析業務／主管／財會 → 派工給 admin-agent 查空車",
        f"等時間軸出現「確認派車：等待確認」（約 1～2 分鐘），到收件匣 {links['inbox_sales']}（{sales}）",
        "按「開啟確認頁」→ 看車輛與截止時間 →「核准派車」（可以寫一句意見）",
        "回到流程詳情，看 admin-agent 代訂 ADM 公務車（book_vehicle）完成後，跑 demo.py trip",
    ])
    return 0


# ---------------------------------------------------------------- trip


def cmd_trip(args) -> int:
    now = now_tpe(args)
    state = load_state()
    step_title("trip：業務回報取車／還車里程")
    flow_id = args.flow or state.get("flow_id")
    if not flow_id:
        if not args.dry_run:
            raise DemoError("沒有 flow_id：先跑 demo.py start，或用 --flow 指定")
        flow_id = "<start 印出的 flow_id>"
    sales = state.get("sales_agent") or args.sales
    cmd = [sys.executable, crm_script(), "report-trip", "--agent", sales, "--flow", flow_id]
    if args.pickup_odometer is not None:
        cmd += ["--pickup-odometer", str(args.pickup_odometer)]
    if args.distance is not None:
        cmd += ["--distance", str(args.distance)]

    if state.get("visit_start_at"):
        opens = datetime.fromisoformat(state["visit_start_at"]) - PICKUP_OPEN_BEFORE
        if now < opens and not args.force:
            wait = int((opens - now).total_seconds() // 60) + 1
            say(f"還不能回報：ADM 從 {hm(opens)}（拜訪開始前 15 分鐘）起才接受登記取車，約 {wait} 分鐘後再跑。"
                "（確定要送就加 --force）")
            return 1 if not args.dry_run else 0

    if args.dry_run:
        say(f"[dry-run] 以 {sales} 身分回報：{show_cmd(cmd)}")
    else:
        ctl = admin_controller()
        detail = ctl.flow(flow_id)
        if detail.get("current_step") != "await_trip":
            say(f"流程目前在「{detail.get('current_step_title') or detail.get('current_step')}」"
                f"（{detail.get('status')}），還沒到等業務回報里程；等 book_vehicle 完成再跑。")
            return 1
        code, result, raw = run_json(cmd, timeout=120)
        reported = result.get("reported") or []
        if code != 0 or not reported:
            say(f"✘ 回報失敗（exit {code}，outcome={result.get('outcome')}）：{result.get('error') or result.get('detail') or raw[-500:]}")
            say("  看 skipped／failed 的原因；車輛里程查不到時加 --pickup-odometer <里程>")
            return 1
        trip = reported[0]
        say(f"✔ {sales} 回報取車 {trip['pickup_odometer']} km、還車 {trip['return_odometer']} km，"
            f"里程差 {trip['distance_km']} km")
        state.update(trip_reported_at=now.isoformat())
        save_state(state)

    links = urls(state)
    manager = person_name(state, "manager", state.get("manager_agent") or args.manager)
    teacher([
        "投影流程詳情：admin-agent 代登記取車、還車（record_trip）→ finance-agent 產生交通費報帳草稿（draft_claim）",
        "講：報帳草稿只存在 controller，主管核准前沒有任何系統寫入金額（demo 假費率）",
        f"出現「核准交通費報帳：等待確認」後，到收件匣 {links['inbox_manager']}（{manager}）",
        "按「開啟確認頁」→ 看里程 × 費率 = 金額 →「核准」",
        "回到流程詳情：controller 通知財會（notify_finance）→ cs-agent 在 CRM 記拜訪互動、完成待辦（close_visit）→ 完成",
        "收尾：課後跑 demo.py reset",
    ])
    return 0


# ---------------------------------------------------------------- status


def cmd_status(args) -> int:
    state = load_state()
    step_title("status：目前的示範")
    if not state:
        say("沒有示範狀態檔（還沒跑 prepare）")
        return 0
    say(f"拜訪待辦：{state.get('follow_up_id')}｜{state.get('customer')}｜{state.get('visit_start_at')}")
    if args.dry_run:
        say("[dry-run] 不連 controller")
        return 0
    ctl = admin_controller()
    for key in ("sales", "manager"):
        person = (state.get("confirmers") or {}).get(key) or {}
        if person.get("agent_id"):
            view = ctl.must("GET", "/inbox/confirm-mode", params={"recipient": person["agent_id"]})
            label = "真人確認" if view.get("human") else "agent 代理"
            say(f"{person.get('display_name') or person['agent_id']}：{label}")
    if state.get("flow_id"):
        detail = ctl.flow(state["flow_id"])
        state["flow_status"] = detail["status"]
        save_state(state)
        say(f"流程 {detail['id']}：{detail['status']}，目前「{detail.get('current_step_title') or '—'}」")
        if detail.get("stuck"):
            say(f"  卡住：{detail['stuck']['reason']}")
        for step in detail.get("steps") or []:
            say(f"  {step['name']:<16} {step['status']:<16} {step.get('summary') or step.get('error') or ''}")
    say(f"前端：{urls(state)['flow'] or urls(state)['overview']}")
    return 0


# ---------------------------------------------------------------- reset


def cmd_reset(args) -> int:
    state = load_state()
    step_title("reset：課後恢復")
    agents = [a for a in (state.get("manager_agent") or args.manager, state.get("sales_agent") or args.sales) if a]
    if args.dry_run:
        for agent in agents:
            say(f"[dry-run] PUT /api/inbox/confirm-mode?recipient={agent}  {{\"mode\": \"agent\"}}")
        if args.cancel and state.get("flow_id"):
            say(f"[dry-run] 取消示範流程 {state['flow_id']}（POST /api/flows/{{id}}/cancel）")
        say(f"[dry-run] 狀態檔 {state_path()} 改名保存（.done-<時間>）")
        return 0
    ctl = admin_controller()
    for agent in agents:
        view = ctl.set_mode(agent, "agent")
        name = (view.get("recipient") or {}).get("display_name") or agent
        say(f"✔ {name}（{agent}）改回 agent 代理" + ("" if view.get("changed") else "（原本就是）"))
    flow_id = state.get("flow_id")
    if flow_id:
        detail = ctl.flow(flow_id)
        if detail["status"] not in TERMINAL:
            if args.cancel:
                ctl.must("POST", f"/flows/{flow_id}/cancel",
                         {"revision": detail["revision"], "reason": "課堂示範結束（demo.py reset --cancel）"})
                say(f"✔ 已取消還沒跑完的示範流程 {flow_id}")
            else:
                say(f"注意：示範流程 {flow_id} 還在「{detail.get('current_step_title')}」（{detail['status']}）。"
                    "要取消請加 --cancel；不取消的話，切回 agent 代理後員工 agent 會在下次檢查收件匣時接手。")
        else:
            say(f"示範流程 {flow_id}：{detail['status']}")
    if state:
        stamp = datetime.now(TPE).strftime("%Y%m%d-%H%M%S")
        os.replace(state_path(), f"{state_path()}.done-{stamp}")
        say(f"狀態檔已改名保存：{state_path()}.done-{stamp}")
    teacher([
        "收件匣切到 Gobta、Main，確認上方顯示「確認方式：agent 代理」",
        "示範時建立的 CRM 拜訪待辦與 ADM 預約是假資料，留著不刪（計畫 0.6）",
    ])
    return 0


# ---------------------------------------------------------------- check（事前檢查）


PING_AGENTS = ("cs-agent", "admin-agent", "finance-agent")


def http_get(url: str, timeout: float = 5) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(getattr(exc, "reason", exc))


def ping_agent(agent: str, timeout: int) -> tuple[bool, str]:
    fd, path = tempfile.mkstemp(prefix="demo-ping-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("課前連線檢查：不要執行任何工具或 skill，只回覆 PONG 四個字母。")
        cmd = [openclaw_bin(), "agent", "--agent", agent, "--session-id",
               f"classroom-demo-ping-{agent}-{int(time.time())}", "--message-file", path,
               "--timeout", str(timeout), "--json"]
        try:
            code, result, raw = run_json(cmd, timeout=timeout + 30)
        except DemoError as exc:
            return False, str(exc)
    finally:
        os.unlink(path)
    reply = openclaw_reply(result)
    ok = code == 0 and result.get("status") == "ok" and "PONG" in reply.upper()
    return ok, reply.strip()[:80] or raw[-200:]


def cmd_check(args) -> int:
    step_title("check：課前檢查")
    gateway = env("FLOW_CONTROLLER_GATEWAY_URL", "http://127.0.0.1:18789").rstrip("/")
    items: list[tuple[str, bool, str]] = []
    if args.dry_run:
        say(f"[dry-run] GET {gateway}/readyz、GET {api_base()}/healthz、admin 登入、確認 skill 腳本與 openclaw 存在")
        if args.ping:
            say(f"[dry-run] 對 {', '.join(PING_AGENTS)} 各送一次「只回覆 PONG」（會用到模型額度）")
        return 0

    status, body = http_get(f"{gateway}/readyz")
    items.append(("OpenClaw gateway /readyz", status == 200, f"{status} {body[:120]}"))
    status, body = http_get(f"{api_base()}/healthz")
    items.append(("controller /healthz", status == 200, f"{status} {body[:120]}"))
    try:
        ctl = admin_controller()
        summary = ctl.must("GET", "/stats/summary")
        gw = (summary.get("gateway") or {}).get("status")
        items.append(("controller admin 登入", True, env("FLOW_CONTROLLER_ADMIN_USER", "teacher")))
        items.append(("controller 看到的 gateway", gw in ("ok", None, "unknown"), str(gw)))
        stuck = summary.get("counts", {}).get("stuck", 0)
        items.append(("目前沒有卡住的流程", not stuck, f"卡住 {stuck} 條（總覽頁看原因）"))
    except DemoError as exc:
        items.append(("controller admin 登入", False, str(exc)))
    for label, path in (("agent-crm crm.py", crm_script()), ("cs-agent crm_visit_list.py", visit_list_script())):
        items.append((label, os.path.exists(path), path))
    binary = openclaw_bin()
    items.append(("openclaw 執行檔", bool(shutil.which(binary)) or os.path.exists(binary), binary))
    if args.ping:
        for agent in PING_AGENTS:
            ok, reply = ping_agent(agent, args.timeout)
            items.append((f"{agent} 回 PONG", ok, reply))

    failed = 0
    for label, ok, detail in items:
        say(f"{'✔' if ok else '✘'} {label}：{detail}")
        failed += 0 if ok else 1
    if not args.ping:
        say("（加 --ping 會叫 3 隻助理 agent 各回一次 PONG，順便確認模型額度；會用到少量額度）")
    teacher([
        "到模型供應商的用量頁確認額度（PONG 失敗、或 agent 回覆 quota／rate limit 時就是額度問題）",
        "投影電腦開總覽，確認右上角沒有「gateway 離線」",
        "備援錄影放在桌面，出錯時直接切過去（見 docs/classroom-demo.md）",
    ])
    return 1 if failed else 0


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="課堂 demo：流程 ① 客戶拜訪派車報帳（在 gateway 主機上跑）")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--dry-run", action="store_true", help="只印出會做的事，不連網、不寫入")
        p.add_argument("--sales", default="gobta", help="扮演業務（派車確認者、回報里程）的員工 agent")
        p.add_argument("--manager", default="main", help="業務主管（報帳確認者）的員工 agent")
        p.add_argument("--now", help="覆寫現在時間（ISO，測試用）")
        return p

    p = common(sub.add_parser("check", help="課前檢查：gateway、controller、腳本、（--ping）agent PONG"))
    p.add_argument("--ping", action="store_true", help="叫 3 隻助理 agent 各回一次 PONG（會用到模型額度）")
    p.add_argument("--timeout", type=int, default=120, help="每隻 agent PONG 的逾時（秒）")
    p.set_defaults(func=cmd_check)

    p = common(sub.add_parser("prepare", help="切成真人確認、建立今天約 20 分鐘後的拜訪待辦"))
    p.add_argument("--lead-minutes", type=int, default=DEFAULT_LEAD_MINUTES, help="拜訪幾分鐘後開始（預設 20）")
    p.add_argument("--duration", type=int, default=DEFAULT_DURATION, help="拜訪時長（分鐘，預設 60）")
    p.add_argument("--seats", type=int, default=1, help="出發人數")
    p.add_argument("--customer", help="客戶名稱或 external id（預設由 agent-crm 隨機挑）")
    p.set_defaults(func=cmd_prepare)

    p = common(sub.add_parser("start", help="叫 cs-agent 啟動示範流程，印出 flow_id 與前端網址"))
    p.add_argument("--no-agent", action="store_true", help="備援：不叫 cs-agent，以 admin 直接啟動示範流程")
    p.add_argument("--timeout", type=int, default=300, help="openclaw agent 的逾時（秒）")
    p.add_argument("--wait", type=float, default=30, help="啟動後最多等幾秒找到流程")
    p.set_defaults(func=cmd_start)

    p = common(sub.add_parser("trip", help="以業務身分回報取車／還車里程"))
    p.add_argument("--flow", help="流程 id（預設用 start 記下的）")
    p.add_argument("--pickup-odometer", type=int, help="手動指定取車里程（查不到車輛里程時）")
    p.add_argument("--distance", type=int, help="手動指定往返公里數")
    p.add_argument("--force", action="store_true", help="不檢查是否已進入 ADM 可登記取車的時段")
    p.set_defaults(func=cmd_trip)

    p = common(sub.add_parser("status", help="看示範流程與確認者狀態"))
    p.set_defaults(func=cmd_status)

    p = common(sub.add_parser("reset", help="課後：確認者切回 agent 代理"))
    p.add_argument("--cancel", action="store_true", help="一併取消還沒跑完的示範流程")
    p.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file()
    try:
        return args.func(args)
    except DemoError as exc:
        say(f"✘ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
