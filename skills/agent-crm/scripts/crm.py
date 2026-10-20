#!/usr/bin/env python3
"""FreeForJoy CRM 業務模擬 CLI（計畫 9.3、9.4）。

業務 agent（mjolmire-businessman、gobta）以 session 帳號登入 CRM，扮演業務本人：

  decide       每個工作日早上跑一次：30% 機率排一筆 1～3 個工作天後的客戶拜訪
               （CRM FollowUp kind=visit、負責人＝自己），並替拜訪結束前排一個
               一次性 cron 去跑 report-trip
  visit        立刻排一筆拜訪（demo 用，可指定客戶與天數）
  report-trip  拜訪結束前：找出自己等待回報還車的派車流程，產生取車／還車里程，
               呼叫 controller 的 POST /api/flows/{id}/events
  status       查自己的拜訪與狀態檔，除錯用

一律輸出單一行 JSON；成功 exit 0，失敗 exit 1。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
import uuid
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402

SKILL = "agent-crm"
SKILL_SCRIPT = os.path.abspath(__file__)

SALES_AGENTS = ("mjolmire-businessman", "gobta")
VISIT_CHANCE = 0.30

DEFAULT_CRM_BASE = "http://localhost:18081/api"
DEFAULT_ADM_BASE = "http://localhost:18001/api/v1"

VISIT_TIMES = ("10:00", "10:30", "11:00", "13:30", "14:00", "14:30", "15:00")
VISIT_DURATIONS = (60, 90, 120)
# 里程回報時間：拜訪（＝公務車預約時段）結束前 10～20 分鐘。
# ADM 只接受「預約開始前 15 分鐘～預約結束」之間登記取車（過了回 409 Booking window has ended），
# controller 收到 trip_report 後還要派工給 admin-agent 去登記，所以一定要在預約結束前送出。
# 拜訪最晚 15:00 開始、最長 120 分鐘，回報時間最晚 16:50，不會碰到 17:50 以後的下班打卡。
REPORT_LEAD_MINUTES = (10, 20)
# report-trip 最早在拜訪結束前幾分鐘就可以回報（比排程早一點，容許 cron 提早觸發）
REPORT_EARLIEST_BEFORE_END = 30
# cs-agent 每個工作日 08:00 掃「明天」的拜訪來啟動流程 ①（計畫 2 ① 步驟 1）；
# 08:00 以後才排的拜訪若是明天，就趕不上掃描，所以至少排到 2 個工作天後。
CS_SCAN_TIME = "08:00"

# 拜訪地點與單程概略公里數（往返里程以此為基準，落在 10～80 km）
LOCATIONS = (
    ("台北市內湖區瑞光路", 8),
    ("台北市大安區忠孝東路四段", 6),
    ("新北市三重區重新路", 12),
    ("新北市板橋區縣民大道", 11),
    ("新北市新店區北新路", 14),
    ("新北市林口區文化三路", 25),
    ("桃園市龜山區文化一路", 30),
    ("基隆市仁愛區愛一路", 32),
    ("新北市汐止區大同路", 13),
    ("桃園市桃園區中正路", 38),
)
PURPOSES = (
    "新品試吃與報價說明",
    "年度供貨合約續約洽談",
    "冷凍粉圓出貨品質回訪",
    "節慶檔期備貨討論",
    "新通路上架提案",
    "門市陳列與促銷活動規劃",
)
TERMINAL_FLOW = {"succeeded", "failed", "expired", "cancelled"}


# --- CRM ---------------------------------------------------------------


class Crm:
    """CRM session client：CSRF cookie → 登入 → 之後的寫入帶 X-CSRFToken。"""

    def __init__(self, base: str):
        headers = {}
        workspace = c.env("CRM_WORKSPACE_ID")
        if workspace:
            headers["X-Workspace-ID"] = workspace
        self.http = c.HttpClient(base, "CRM", headers)

    def login(self, username: str, password: str) -> None:
        status, body = self.http.request("GET", "/auth/csrf/")
        if status != 200:
            raise c.ServiceError(f"CRM 取 CSRF 失敗 {status}: {c.short(body)}")
        status, body = self.http.request(
            "POST", "/auth/login/", {"username": username, "password": password},
            headers={"X-CSRFToken": self.http.cookie("csrftoken") or ""},
            idempotent=True,  # 登入重送沒有副作用
        )
        if status != 200:
            raise c.ServiceError(
                f"CRM 登入失敗 {status}: {c.short(body)}", announce=status in (400, 401, 403),
                kind="login_failed",
            )

    def get(self, path: str):
        status, body = self.http.request("GET", path)
        if status != 200:
            raise c.ServiceError(f"CRM GET {path} 失敗 {status}: {c.short(body)}",
                                 announce=status in (401, 403))
        return body

    def post(self, path: str, body: dict):
        # session 使用者在 CRM 沒有伺服器端冪等保護，所以不重試已送出的請求；
        # 重跑時由狀態檔＋查詢既有待辦來避免重複建立。
        return self.http.request(
            "POST", path, body, headers={"X-CSRFToken": self.http.cookie("csrftoken") or ""},
            idempotent=False,
        )

    def paged(self, path: str, max_pages: int = 4) -> list:
        out: list = []
        body = self.get(path)
        for _ in range(max_pages):
            if isinstance(body, list):
                out.extend(body)
                break
            out.extend(body.get("results") or [])
            nxt = body.get("next")
            if not nxt:
                break
            parsed = urllib.parse.urlsplit(nxt)
            rel = parsed.path.split("/api", 1)[-1]
            if parsed.query:
                rel = f"{rel}?{parsed.query}"
            body = self.get(rel)
        return out


def crm_login(agent: str) -> Crm:
    password = c.env("CRM_AGENT_PASSWORD")
    if not password:
        raise c.ConfigError("沒有設定 CRM_AGENT_PASSWORD，無法登入 CRM")
    crm = Crm(c.env("CRM_API_BASE", DEFAULT_CRM_BASE))
    crm.login(agent, password)
    return crm


def my_pending_visits(crm: Crm, agent: str, due_after: datetime | None = None) -> list:
    hrm = c.AGENTS[agent]["hrm_employee_id"]
    query = {"kind": "visit", "status": "pending", "assignee_hrm_employee_id": hrm}
    if due_after is not None:
        query["due_after"] = c.iso(due_after)
    return crm.paged("/v1/follow-ups/?" + urllib.parse.urlencode(query))


def resolve_my_user_id(crm: Crm, agent: str) -> int:
    """找出自己在 CRM 的 user id（FollowUp.assignee 要用）。

    CRM 的 /api/me/ 目前不回 user id，依序：
    1. 狀態檔快取
    2. 環境變數 CRM_USER_ID_<AGENT>（手動指定）
    3. /api/me/ 若之後補上 id／user_id 欄位就直接用
    4. 查自己（assignee_hrm_employee_id）負責的任何待辦跟進，取其 assignee
    """
    cache_path = c.state_path(SKILL, agent, "profile.json")
    cache = c.load_json(cache_path)
    if isinstance(cache.get("crm_user_id"), int):
        return cache["crm_user_id"]

    found: int | None = None
    override = c.env(f"CRM_USER_ID_{c.agent_env_suffix(agent)}")
    if override.isdigit():
        found = int(override)
    if found is None:
        me = crm.get("/me/")
        for key in ("id", "user_id"):
            if isinstance(me, dict) and isinstance(me.get(key), int):
                found = me[key]
                break
    if found is None:
        hrm = c.AGENTS[agent]["hrm_employee_id"]
        rows = crm.paged(
            "/v1/follow-ups/?" + urllib.parse.urlencode({"assignee_hrm_employee_id": hrm}),
            max_pages=1,
        )
        for row in rows:
            if isinstance(row.get("assignee"), int):
                found = row["assignee"]
                break
    if found is None:
        raise c.ConfigError(
            f"找不到 {agent} 在 CRM 的 user id：CRM 的 /api/me/ 不回 id，且還沒有任何負責人是自己的待辦。"
            f"請設定環境變數 CRM_USER_ID_{c.agent_env_suffix(agent)}，"
            "或確認 CRM 的 UserProfile.hrm_employee_id 已對應"
        )
    cache["crm_user_id"] = found
    c.save_json(cache_path, cache)
    return found


def pick_customer(crm: Crm, exclude: set) -> dict:
    customers = [
        row for row in crm.paged("/v1/customers/")
        if row.get("status", "active") == "active" and row.get("id") not in exclude
    ]
    if not customers:
        customers = [row for row in crm.paged("/v1/customers/") if row.get("status", "active") == "active"]
    if not customers:
        raise c.ServiceError("CRM 沒有任何客戶可以拜訪（種子資料還沒建？）", announce=True,
                             kind="no_customer")
    return c.RNG.choice(customers)


def find_customer(crm: Crm, needle: str) -> dict:
    try:
        uuid.UUID(needle)
        return crm.get(f"/v1/customers/{needle}/")
    except ValueError:
        pass
    rows = crm.paged("/v1/customers/?" + urllib.parse.urlencode({"search": needle}), max_pages=1)
    if not rows:
        raise c.ServiceError(f"CRM 找不到客戶「{needle}」", kind="no_customer")
    exact = [r for r in rows if needle in (r.get("name"), r.get("external_customer_id"))]
    return (exact or rows)[0]


# --- 拜訪計畫 -----------------------------------------------------------


def location_for(customer_id: str) -> tuple:
    """同一位客戶固定在同一個地點（以客戶 id 雜湊挑選）。"""
    digest = c.stable_key("location", customer_id)
    return LOCATIONS[int(digest[:8], 16) % len(LOCATIONS)]


def visit_title(customer_name: str, location: str, seats: int, duration: int, purpose: str) -> str:
    """FollowUp 只有 title 可以放內容，格式固定好讓 cs-agent／admin-agent 解析：

    [拜訪] <客戶>｜地點：<地點>｜人數：<n> 人｜時長：<分鐘> 分鐘｜目的：<一句話>
    """
    title = (
        f"[拜訪] {customer_name}｜地點：{location}｜人數：{seats} 人｜"
        f"時長：{duration} 分鐘｜目的：{purpose}"
    )
    return title[:200]


def parse_title(title: str | None) -> dict:
    """從拜訪待辦標題讀回人數與時長（格式見 visit_title）。"""
    out: dict = {}
    if not title:
        return out
    seats = re.search(r"人數：(\d+)", title)
    minutes = re.search(r"時長：(\d+)", title)
    if seats:
        out["seats"] = int(seats.group(1))
    if minutes:
        out["duration_minutes"] = int(minutes.group(1))
    return out


def plan_visit(today: date, now: datetime, *, days: int | None = None, time_s: str | None = None,
               duration: int | None = None, seats: int | None = None) -> dict:
    if days is None:
        min_days = 1 if now < c.at(today, CS_SCAN_TIME) else 2
        days = c.RNG.randint(min_days, 3)
    day = c.add_workdays(today, days) if days > 0 else today
    due = c.at(day, time_s or c.RNG.choice(VISIT_TIMES))
    if due <= now:
        raise c.ServiceError("拜訪時間已經過去，請指定未來的時間", kind="invalid_time")
    return {
        "due_at": c.iso(due),
        "visit_date": day.isoformat(),
        "duration_minutes": duration or c.RNG.choice(VISIT_DURATIONS),
        "seats": seats or c.RNG.choice((1, 2, 2, 3)),
        "purpose": c.RNG.choice(PURPOSES),
    }


def report_time(due: datetime, duration: int) -> datetime:
    """拜訪結束前 10～20 分鐘（見 REPORT_LEAD_MINUTES），但不早於拜訪開始後 15 分鐘。"""
    end = due + timedelta(minutes=duration)
    lead = c.RNG.randint(*REPORT_LEAD_MINUTES)
    return max(due + timedelta(minutes=15), end - timedelta(minutes=lead))


def visits_index_path(agent: str) -> str:
    return c.state_path(SKILL, agent, "visits.json")


def remember_visit(agent: str, followup: dict, plan: dict, customer: dict, location: tuple) -> None:
    path = visits_index_path(agent)
    index = c.load_json(path)
    index[followup["id"]] = {
        "follow_up_id": followup["id"],
        "customer_id": customer.get("id"),
        "customer_name": customer.get("name"),
        "due_at": plan["due_at"],
        "visit_date": plan["visit_date"],
        "duration_minutes": plan["duration_minutes"],
        "seats": plan["seats"],
        "location": location[0],
        "one_way_km": location[1],
        "title": followup.get("title"),
    }
    c.save_json(path, index)


def schedule_report(agent: str, plan: dict, now: datetime) -> str:
    visit_day = date.fromisoformat(plan["visit_date"])
    due = c.parse_dt(plan["due_at"])
    when = report_time(due, plan["duration_minutes"])
    if when <= now:
        when = now + timedelta(minutes=2)
    # 同一天可能有兩筆拜訪（不同天 decide 排到同一天），一筆拜訪一個 job，
    # 各自在自己的預約時段內回報；declaration-key 帶上拜訪時間才不會互相覆蓋。
    slot = due.strftime("%H%M")
    return c.schedule_once(
        agent=agent,
        name=f"拜訪里程回報 {agent} {visit_day.isoformat()} {due.strftime('%H:%M')}",
        declaration_key=f"{SKILL}:report-trip:{agent}:{visit_day.isoformat()}T{slot}",
        when=when,
        message=(
            "用 agent-crm skill 回報今天拜訪客戶的取車／還車里程。\n\n"
            f"執行：python3 {SKILL_SCRIPT} report-trip --agent {agent}\n\n"
            "照 skill 的規則處理輸出：announce 是 true 才發訊息，false 就安靜結束、不要發任何訊息。"
        ),
    )


def create_visit(crm: Crm, agent: str, plan: dict, customer: dict, state: dict, persist) -> dict:
    """建立拜訪待辦。session 使用者在 CRM 沒有冪等鍵，所以：

    送出前先把計畫寫進狀態檔；重跑時先查「同客戶、同時間、負責人是自己」的待辦，
    有就沿用，不會建出第二筆。
    """
    location = location_for(customer["id"])
    title = visit_title(customer.get("name") or "客戶", location[0], plan["seats"],
                        plan["duration_minutes"], plan["purpose"])
    due = c.parse_dt(plan["due_at"])
    for row in my_pending_visits(crm, agent, due_after=due - timedelta(minutes=1)):
        if row.get("customer") == customer["id"] and c.parse_dt(row.get("due_at")) == due:
            # 沿用既有待辦：人數、時長以待辦標題為準（不是這次重新抽的）
            actual = {**plan, **parse_title(row.get("title"))}
            remember_visit(agent, row, actual, customer, location)
            return {"followup": row, "created": False, "location": location, "plan": actual}

    user_id = resolve_my_user_id(crm, agent)
    state["pending_create"] = {"customer": customer["id"], "due_at": plan["due_at"]}
    persist()
    status, body = crm.post(
        "/v1/follow-ups/",
        {
            "customer": customer["id"],
            "title": title,
            "kind": "visit",
            "due_at": plan["due_at"],
            "assignee": user_id,
        },
    )
    if status != 201 or not isinstance(body, dict):
        raise c.ServiceError(f"建立拜訪待辦失敗 {status}: {c.short(body)}",
                             announce=status in (400, 403))
    state.pop("pending_create", None)
    remember_visit(agent, body, plan, customer, location)
    return {"followup": body, "created": True, "location": location, "plan": plan}


def visit_result(result: dict, plan: dict, customer: dict, info: dict) -> None:
    followup = info["followup"]
    result.update(
        follow_up_id=followup.get("id"),
        customer=customer.get("name"),
        due_at=plan["due_at"],
        location=info["location"][0],
        seats=plan["seats"],
        duration_minutes=plan["duration_minutes"],
        title=followup.get("title"),
    )


# --- 子指令：decide -----------------------------------------------------


def cmd_decide(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    if agent not in SALES_AGENTS:
        result.update(outcome="not_sales", detail=f"{agent} 不是業務，不排拜訪")
        return True
    if not c.is_workday(today) and not args.force:
        result.update(outcome="not_working_day", detail="今天不是工作日，不排拜訪")
        return True

    path = c.state_path(SKILL, agent, f"{today.isoformat()}.json")
    state = c.load_json(path)

    def persist():
        if not args.dry_run:
            c.save_json(path, state)

    if state.get("decision") == "none" or (state.get("decision") == "visit" and state.get("done")):
        result.update(outcome="already_decided", decision=state["decision"],
                      follow_up_id=state.get("follow_up_id"),
                      detail=f"今天已經決策過了：{state['decision']}")
        return True

    if state.get("decision") == "visit":
        decision, roll = "visit", state.get("roll")
    else:
        roll = round(c.RNG.random(), 4)
        if args.force:
            decision = args.force
        else:
            decision = "visit" if roll < VISIT_CHANCE else "none"
        state.update(decision=decision, roll=roll, decided_at=c.iso(now))
        if decision == "visit":
            state["plan"] = plan_visit(today, now)
    result["roll"] = roll

    if decision == "none":
        persist()
        result.update(outcome="no_visit", decision="none", detail="今天沒有要排的拜訪")
        return True

    plan = state["plan"]
    if args.dry_run:
        result.update(outcome="would_schedule_visit", decision="visit", plan=plan,
                      detail="dry-run：沒有連線 CRM，也沒有建立待辦或排程")
        return True

    persist()
    crm = crm_login(agent)
    if state.get("customer"):
        customer = state["customer"]
    else:
        exclude = {row.get("customer") for row in my_pending_visits(crm, agent)}
        picked = pick_customer(crm, exclude)
        customer = {"id": picked["id"], "name": picked.get("name")}
        state["customer"] = customer
        persist()

    if not state.get("follow_up_id"):
        info = create_visit(crm, agent, plan, customer, state, persist)
        plan = state["plan"] = info["plan"]
        state["follow_up_id"] = info["followup"].get("id")
        state["title"] = info["followup"].get("title")
        persist()
    else:
        info = {"followup": {"id": state["follow_up_id"], "title": state.get("title")},
                "location": location_for(customer["id"])}

    visit_result(result, plan, customer, info)
    if not state.get("report_scheduled_at"):
        state["report_scheduled_at"] = schedule_report(agent, plan, now)
    state["done"] = True
    persist()
    result.update(
        outcome="visit_scheduled",
        decision="visit",
        report_scheduled_at=state["report_scheduled_at"],
        detail=(
            f"已排定 {plan['due_at'][:16].replace('T', ' ')} 拜訪 {customer.get('name')}，"
            f"拜訪當天 {state['report_scheduled_at'][11:16]} 回報里程"
        ),
    )
    return True


# --- 子指令：visit ------------------------------------------------------


def cmd_visit(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    if agent not in SALES_AGENTS and not args.any_agent:
        result.update(outcome="not_sales", detail=f"{agent} 不是業務；要硬排請加 --any-agent")
        return True
    plan = plan_visit(today, now, days=args.days, time_s=args.time, duration=args.duration,
                      seats=args.seats)
    if args.dry_run:
        result.update(outcome="would_schedule_visit", plan=plan, customer=args.customer,
                      detail="dry-run：沒有連線 CRM，也沒有建立待辦或排程")
        return True

    crm = crm_login(agent)
    if args.customer:
        picked = find_customer(crm, args.customer)
    else:
        picked = pick_customer(crm, {row.get("customer") for row in my_pending_visits(crm, agent)})
    customer = {"id": picked["id"], "name": picked.get("name")}
    scratch: dict = {}
    info = create_visit(crm, agent, plan, customer, scratch, lambda: None)
    plan = info["plan"]
    visit_result(result, plan, customer, info)
    if not args.no_schedule:
        result["report_scheduled_at"] = schedule_report(agent, plan, now)
    result.update(
        outcome="visit_scheduled" if info["created"] else "visit_exists",
        detail=(
            f"{'已排定' if info['created'] else '已經有這筆拜訪'} "
            f"{plan['due_at'][:16].replace('T', ' ')} 拜訪 {customer.get('name')}"
        ),
    )
    visit_day = date.fromisoformat(plan["visit_date"])
    if visit_day <= c.add_workdays(today, 1) and now >= c.at(today, CS_SCAN_TIME):
        # cs-agent 只在 08:00 掃「明天」的拜訪；這筆趕不上掃描，demo 要手動啟動流程 ①
        result["note"] = ("這筆拜訪趕不上 cs-agent 08:00 的掃描，流程 ① 不會自動啟動；"
                          "demo 時請手動啟動，或改用 --days 2 以上")
    return True


# --- 子指令：report-trip ------------------------------------------------

DATE_KEYS = ("visit_start_at", "visit_at", "visit_start", "due_at", "start_at", "visit_time",
             "scheduled_at", "visit_datetime")
END_KEYS = ("visit_end_at", "visit_end", "end_at")
FOLLOWUP_KEYS = ("follow_up_id", "followup_id", "follow_up")


def walk(obj, depth: int = 0):
    """走訪巢狀 dict／list，產生 (key, value)。"""
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value, depth + 1)


def first_value(obj, keys) -> object:
    for key, value in walk(obj):
        if key in keys and value not in (None, ""):
            return value
    return None


DATETIME_IN_TEXT = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


# 流程 ① 確認頁 context 的 visit 欄位：「10/15 10:00–12:00」（沒有年份）
WINDOW_IN_TEXT = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*[–\-~～]\s*(\d{1,2}):(\d{2})")


def _first_key_time(obj, keys) -> datetime | None:
    """依 keys 的優先順序找第一個能解析的時間欄位。"""
    pairs = list(walk(obj))
    for wanted in keys:
        for key, value in pairs:
            if key == wanted:
                parsed = c.parse_dt(value)
                if parsed:
                    return parsed
    return None


def window_from_text(obj, today: date) -> tuple:
    """從「M/D HH:MM–HH:MM」字串推出拜訪的開始與結束（年份取最接近今天的那一年）。"""
    for _key, value in walk(obj):
        if not isinstance(value, str):
            continue
        match = WINDOW_IN_TEXT.search(value)
        if not match:
            continue
        mo, dd, h1, m1, h2, m2 = (int(x) for x in match.groups())
        try:
            day = date(today.year, mo, dd)
        except ValueError:
            continue
        if day - today > timedelta(days=180):
            day = day.replace(year=today.year - 1)
        elif today - day > timedelta(days=180):
            day = day.replace(year=today.year + 1)
        start = c.at(day, f"{h1:02d}:{m1:02d}")
        end = c.at(day, f"{h2:02d}:{m2:02d}")
        return start, (end if end > start else None)
    return None, None


def find_visit_at(obj, today: date | None = None) -> datetime | None:
    """先找明確的時間欄位，再從字串裡找「YYYY-MM-DD HH:MM」或「M/D HH:MM–HH:MM」。"""
    parsed = _first_key_time(obj, DATE_KEYS)
    if parsed:
        return parsed
    for _key, value in walk(obj):
        if isinstance(value, str):
            match = DATETIME_IN_TEXT.search(value)
            if match:
                parsed = c.parse_dt(f"{match.group(1)}T{match.group(2)}")
                if parsed:
                    return parsed
    if today is not None:
        return window_from_text(obj, today)[0]
    return None


def find_visit_end(obj, today: date | None = None) -> datetime | None:
    parsed = _first_key_time(obj, END_KEYS)
    if parsed:
        return parsed
    if today is not None:
        return window_from_text(obj, today)[1]
    return None


def sales_ids(obj) -> set:
    """流程狀態裡標示「業務」的 HRM 員工 UUID。"""
    found = set()
    for key, value in walk(obj):
        if "sales" in str(key).lower() and isinstance(value, str):
            found.add(value)
        if "sales" in str(key).lower() and isinstance(value, dict):
            hid = value.get("hrm_employee_id")
            if isinstance(hid, str):
                found.add(hid)
    return {v for v in found if re.fullmatch(r"[0-9a-fA-F-]{36}", v)}


def date_from_title(title: str | None, today: date) -> date | None:
    """標題裡的 M/D（例如「王經理 10/15 拜訪客戶 C」），最後手段。"""
    if not title:
        return None
    match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", title)
    if not match:
        return None
    try:
        guess = date(today.year, int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None
    if guess - today > timedelta(days=180):
        guess = guess.replace(year=today.year - 1)
    return guess


def inbox_records(agent: str) -> dict:
    """agent-inbox 處理派車確認時記下的流程與確認頁內容（同一隻 agent）。"""
    data = c.load_json(c.state_path("agent-inbox", agent, "handled.json"))
    return data.get("approvals", {}) if isinstance(data, dict) else {}


class Controller:
    def __init__(self, agent: str):
        self.http = c.HttpClient(c.controller_base(), "controller",
                                 {"Authorization": f"Bearer {c.controller_token(agent)}"})

    def get(self, path: str):
        return self.http.request("GET", f"/api{path}")

    def post(self, path: str, body: dict):
        # 事件回報：連線失敗才重試；回應遺失時重跑會拿到 409（已不在等待），不會重複登記
        return self.http.request("POST", f"/api{path}", body, idempotent=False)


class Adm:
    """查自己的公務車預約與車輛目前里程（取車里程不能小於車輛里程）。"""

    def __init__(self, agent: str):
        self.agent = agent
        self.http = c.HttpClient(c.env("ADM_API_BASE", DEFAULT_ADM_BASE), "ADM")
        password = c.env("ADM_AGENT_PASSWORD")
        if not password:
            raise c.ConfigError("沒有設定 ADM_AGENT_PASSWORD")
        status, body = self.http.request("POST", "/auth/login/",
                                         {"username": agent, "password": password},
                                         idempotent=True)
        if status != 200:
            raise c.ServiceError(f"ADM 登入失敗 {status}: {c.short(body)}")

    def get(self, path: str):
        status, body = self.http.request("GET", path)
        if status != 200:
            raise c.ServiceError(f"ADM GET {path} 失敗 {status}: {c.short(body)}")
        return body

    def booking_near(self, visit_at: datetime | None, booking_id=None) -> dict | None:
        rows = [
            r for r in self.get("/vehicle-bookings/")
            if r.get("status") in ("active", "picked_up")
            and self.agent in (r.get("booked_for_username"), r.get("booked_by_username"))
        ]
        if booking_id is not None:
            for r in rows:
                if str(r.get("id")) == str(booking_id):
                    return r
        if visit_at is None:
            return None
        same_day = [r for r in rows if (c.parse_dt(r.get("start_at")) or visit_at).date() == visit_at.date()]
        if not same_day:
            return None
        return min(same_day, key=lambda r: abs((c.parse_dt(r["start_at"]) - visit_at).total_seconds()))

    def vehicle(self, vehicle_id) -> dict | None:
        for v in self.get("/vehicles/"):
            if str(v.get("id")) == str(vehicle_id):
                return v
        return None


def trip_distance(index_entry: dict | None, override: int | None) -> int:
    if override:
        return int(override)
    if index_entry and index_entry.get("one_way_km"):
        base = int(index_entry["one_way_km"]) * 2
        return max(10, min(80, base + c.RNG.randint(-3, 6)))
    return c.RNG.randint(10, 80)


def trip_note(entry: dict | None, distance: int) -> str:
    if entry and entry.get("customer_name"):
        where = f"，地點在{entry['location']}" if entry.get("location") else ""
        return f"今天拜訪 {entry['customer_name']}{where}，往返共 {distance} 公里，車況正常。"
    return f"今天拜訪客戶，往返共 {distance} 公里，車況正常。"


def cmd_report_trip(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    if agent not in SALES_AGENTS and not args.flow:
        result.update(outcome="not_sales", detail=f"{agent} 不是業務，沒有拜訪要回報")
        return True

    trips_path = c.state_path(SKILL, agent, "trips.json")
    trips = c.load_json(trips_path)
    index = c.load_json(visits_index_path(agent))
    my_hrm = c.AGENTS[agent]["hrm_employee_id"]

    if args.dry_run:
        todays = [v for v in index.values() if v.get("visit_date") == today.isoformat()]
        plans = []
        for entry in todays:
            distance = trip_distance(entry, args.distance)
            plans.append({"follow_up_id": entry["follow_up_id"], "customer": entry.get("customer_name"),
                          "distance_km": distance, "note": trip_note(entry, distance)})
        result.update(outcome="would_report", visits_today=plans,
                      detail="dry-run：沒有連線 controller／ADM，只列出狀態檔裡今天的拜訪")
        return True

    ctl = Controller(agent)
    candidates: dict = {}

    def add(flow_id, **info):
        if not flow_id:
            return
        entry = candidates.setdefault(str(flow_id), {})
        for k, v in info.items():
            if v is not None and k not in entry:
                entry[k] = v

    if args.flow:
        add(args.flow, source="arg")
    else:
        # 1) controller 若開放員工 token 列流程就用；目前列表限登入使用者，401／403 直接略過
        status, body = ctl.get("/flows?" + urllib.parse.urlencode(
            {"type": "visit", "status": "running,waiting_approval", "limit": 200}))
        if status == 200 and isinstance(body, dict):
            for item in body.get("items") or []:
                add(item.get("id"), title=item.get("title"), source="flows")
        # 2) 自己的收件匣：派車確認通知帶 flow_id
        status, body = ctl.get("/inbox?" + urllib.parse.urlencode({"limit": 200}))
        if status == 200 and isinstance(body, dict):
            for item in body.get("items") or []:
                add(item.get("flow_id"), title=item.get("title"), source="inbox")
        elif status not in (401, 403, 404):
            raise c.ServiceError(f"讀 controller 收件匣失敗 {status}: {c.short(body)}")
        # 3) agent-inbox 處理派車確認時記下的確認頁內容
        for record in inbox_records(agent).values():
            add(record.get("flow_id"), title=record.get("flow_title"), context=record.get("context"),
                source="inbox-state")

    adm = None
    adm_error = None
    reported, skipped, failed = [], [], []

    for flow_id, info in candidates.items():
        if trips.get(flow_id, {}).get("status") == "reported":
            skipped.append({"flow_id": flow_id, "reason": "already_reported"})
            continue

        status, detail = ctl.get(f"/flows/{flow_id}")
        detail = detail if status == 200 and isinstance(detail, dict) else None
        if detail is not None:
            if detail.get("type") not in (None, "visit"):
                continue
            if detail.get("status") in TERMINAL_FLOW:
                skipped.append({"flow_id": flow_id, "reason": f"flow_{detail.get('status')}"})
                continue
            owners = sales_ids(detail.get("state"))
            if owners and my_hrm not in owners:
                skipped.append({"flow_id": flow_id, "reason": "not_my_visit"})
                continue

        source = (detail or {}).get("state") or info.get("context")
        followup_id = first_value((detail or {}).get("state"), FOLLOWUP_KEYS) or first_value(
            info.get("context"), FOLLOWUP_KEYS)
        entry = index.get(str(followup_id)) if followup_id else None
        visit_at = find_visit_at(source, today) or (c.parse_dt(entry["due_at"]) if entry else None)
        visit_day = visit_at.date() if visit_at else date_from_title(
            (detail or {}).get("title") or info.get("title"), today)
        if entry is None and visit_at is not None:
            # 對不到 follow-up id 時，用時間對回自己排的拜訪（同一時間只會有一筆）
            entry = next((v for v in index.values() if c.parse_dt(v.get("due_at")) == visit_at), None)

        if not args.flow:
            if visit_day is None:
                skipped.append({"flow_id": flow_id, "reason": "visit_date_unknown"})
                continue
            if visit_day > today:
                skipped.append({"flow_id": flow_id, "reason": "visit_not_today",
                                "visit_date": visit_day.isoformat()})
                continue
            if (today - visit_day).days > 3:
                skipped.append({"flow_id": flow_id, "reason": "visit_too_old"})
                continue
            if visit_at is not None and visit_day == today and not args.force:
                # 公務車預約時段＝拜訪時段；要在結束前回報，ADM 才收得下取車登記
                visit_end = find_visit_end(source, today)
                if visit_end is None or visit_end <= visit_at:
                    minutes = (entry or {}).get("duration_minutes") or 90
                    visit_end = visit_at + timedelta(minutes=minutes)
                if now < visit_end - timedelta(minutes=REPORT_EARLIEST_BEFORE_END):
                    skipped.append({"flow_id": flow_id, "reason": "visit_not_finished"})
                    continue

        if detail is not None:
            current = next((s for s in detail.get("steps") or []
                            if s.get("name") == detail.get("current_step")), None)
            if current is not None and current.get("kind") != "wait":
                skipped.append({"flow_id": flow_id, "reason": "not_waiting_trip_report",
                                "current_step": detail.get("current_step")})
                continue

        plan = trips.get(flow_id)
        if not plan or plan.get("status") != "planned":
            pickup = args.pickup_odometer
            if pickup is None:
                state_odo = first_value((detail or {}).get("state"), ("pickup_odometer",))
                if isinstance(state_odo, int):
                    pickup = state_odo
            if pickup is None:
                try:
                    if adm is None and adm_error is None:
                        adm = Adm(agent)
                except c.ServiceError as exc:
                    adm_error = str(exc)
                if adm is not None:
                    booking_id = first_value((detail or {}).get("state"),
                                             ("booking_id", "vehicle_booking_id"))
                    booking = adm.booking_near(visit_at, booking_id)
                    if booking and isinstance(booking.get("pickup_odometer"), int):
                        pickup = booking["pickup_odometer"]
                    elif booking:
                        vehicle = adm.vehicle(booking.get("vehicle"))
                        if vehicle and isinstance(vehicle.get("odometer"), int):
                            pickup = vehicle["odometer"]
                if pickup is None:
                    odo = first_value((detail or {}).get("state"), ("odometer",))
                    if isinstance(odo, int):
                        pickup = odo
            if pickup is None:
                failed.append({"flow_id": flow_id, "reason": "odometer_unknown",
                               "error": adm_error or "查不到這趟用車的車輛里程，請手動用 --pickup-odometer 指定"})
                continue
            distance = trip_distance(entry, args.distance)
            plan = {
                "status": "planned",
                "pickup_odometer": int(pickup),
                "return_odometer": int(pickup) + distance,
                "distance_km": distance,
                "note": trip_note(entry, distance),
                "visit_date": visit_day.isoformat() if visit_day else None,
            }
            trips[flow_id] = plan
            c.save_json(trips_path, trips)

        status, body = ctl.post(
            f"/flows/{flow_id}/events",
            {
                "type": "trip_report",
                "pickup_odometer": plan["pickup_odometer"],
                "return_odometer": plan["return_odometer"],
                "note": plan["note"],
            },
        )
        if 200 <= status < 300:
            plan.update(status="reported", reported_at=c.iso(now))
            trips[flow_id] = plan
            c.save_json(trips_path, trips)
            reported.append({"flow_id": flow_id, "pickup_odometer": plan["pickup_odometer"],
                             "return_odometer": plan["return_odometer"],
                             "distance_km": plan["distance_km"]})
        elif status in (403, 404, 409, 410):
            skipped.append({"flow_id": flow_id, "reason": c.error_code(body) or f"http_{status}"})
        else:
            failed.append({"flow_id": flow_id, "reason": f"http_{status}", "error": c.short(body)})

    result.update(reported=reported, skipped=skipped, failed=failed)
    if reported:
        total = sum(r["distance_km"] for r in reported)
        result.update(outcome="trip_reported",
                      detail=f"已回報 {len(reported)} 趟拜訪的取車／還車里程（共 {total} 公里）")
    elif failed:
        # 有一趟今天該回報卻送不出去：要人補登，值得講
        result.update(outcome="report_failed", announce=True, announce_kind="error",
                      detail="有拜訪的里程回報失敗，需要人工補登", error=failed[0].get("error"))
        return False
    else:
        result.update(outcome="no_pending_trip", detail="沒有等待回報里程的拜訪")
    return not failed


# --- 子指令：status -----------------------------------------------------


def cmd_status(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    result.update(
        outcome="status",
        state=c.load_json(c.state_path(SKILL, agent, f"{today.isoformat()}.json")),
        visits=c.load_json(visits_index_path(agent)),
        trips=c.load_json(c.state_path(SKILL, agent, "trips.json")),
        detail="今日狀態查詢",
    )
    if not args.dry_run and agent in SALES_AGENTS:
        crm = crm_login(agent)
        result["pending_visits"] = [
            {"id": r.get("id"), "title": r.get("title"), "due_at": r.get("due_at")}
            for r in my_pending_visits(crm, agent, due_after=now - timedelta(days=1))
        ]
    return True


# --- main ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CRM 業務模擬（拜訪與里程回報）")
    parser.add_argument("command", choices=["decide", "visit", "report-trip", "status"])
    parser.add_argument("--agent", help="OpenClaw agent id（預設讀 OPENCLAW_AGENT_ID）")
    parser.add_argument("--dry-run", action="store_true", help="不連網、不寫入、不排程，只顯示決策")
    parser.add_argument("--date", help="覆寫今天的日期 YYYY-MM-DD（測試用）")
    parser.add_argument("--now", help="覆寫現在時間（ISO，測試用）")
    parser.add_argument("--seed", type=int, help="固定亂數種子（測試用）")
    parser.add_argument("--force", nargs="?", const=True, default=None,
                        help="decide：visit／none 跳過擲骰；report-trip：不檢查拜訪是否已結束")
    # visit
    parser.add_argument("--customer", help="visit：客戶名稱、external id 或 CRM UUID（預設隨機）")
    parser.add_argument("--days", type=int, help="visit：幾個工作天後（預設 1～3 隨機；0 代表今天）")
    parser.add_argument("--time", help="visit：拜訪時間 HH:MM")
    parser.add_argument("--duration", type=int, help="visit：拜訪時長（分鐘）")
    parser.add_argument("--seats", type=int, help="visit：出發人數")
    parser.add_argument("--no-schedule", action="store_true", help="visit：不排 report-trip 的 cron")
    parser.add_argument("--any-agent", action="store_true", help="visit：非業務也允許（demo 用）")
    # report-trip
    parser.add_argument("--flow", help="report-trip：只回報這條流程（demo 用，略過日期檢查）")
    parser.add_argument("--pickup-odometer", type=int, help="report-trip：手動指定取車里程")
    parser.add_argument("--distance", type=int, help="report-trip：手動指定往返公里數")
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    if args.seed is not None:
        c.RNG.seed(args.seed)
    result: dict = {"command": args.command, "agent": args.agent or os.environ.get("OPENCLAW_AGENT_ID"),
                    "announce": False, "dry_run": args.dry_run}

    def handler() -> bool:
        agent = c.resolve_agent(args.agent)
        now = c.now_tz(args.now)
        today = date.fromisoformat(args.date) if args.date else now.date()
        result.update(agent=agent, date=today.isoformat())
        if args.command == "decide":
            if args.force not in (None, "visit", "none"):
                raise c.ConfigError("decide 的 --force 只能是 visit 或 none")
            return cmd_decide(agent, today, now, args, result)
        if args.command == "visit":
            return cmd_visit(agent, today, now, args, result)
        if args.command == "report-trip":
            return cmd_report_trip(agent, today, now, args, result)
        return cmd_status(agent, today, now, args, result)

    c.run_main(handler, result)


if __name__ == "__main__":
    main()
