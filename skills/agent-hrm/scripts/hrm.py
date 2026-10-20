#!/usr/bin/env python3
"""FreeForJoy HRM 人資模擬 CLI（計畫 9.3 shion 那列、流程 ③ 的資料來源）。

人資 agent（shion，HRM role=hr）以 session 帳號登入 HRM，扮演人資本人：

  decide   每週一跑一次：20% 機率建立一位下週到職的新人（假名、去識別化；部門隨機，
           業務部、客服部機率較高；主要 Employment 的到職日是下週某個工作日）
  hire     立刻建立一位新人（demo 用，可指定部門、到職日）
  status   查狀態檔與 HRM 連線（登入、部門、建立 Employment 的端點），除錯用

新人建好後，hr-agent 每週一 09:00 的 onboarding-list 會查到他，啟動流程 ③。
一律輸出單一行 JSON；成功 exit 0，失敗 exit 1。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402

SKILL = "agent-hrm"
HIRE_CHANCE = 0.20
DEFAULT_HRM_BASE = "http://localhost:18000/api"
# 建立 Employment 的端點（HRM 目前沒有，見 references/api.md「待 HRM 補上」）
DEFAULT_EMPLOYMENT_PATH = "/employments/"
# hr-agent 的 onboarding-list 每週一 09:00 掃「下週」到職的新人（計畫 2 ③ 步驟 1）
SCAN_WEEKDAY = 0
SCAN_TIME = "09:00"

# (部門名稱, HRM 部門代碼, 權重)；業務部、客服部人比較多，新人也比較多
DEPARTMENTS = (
    ("業務部", "ORG-SALES", 35),
    ("客服部", "ORG-CS", 30),
    ("資訊部", "ORG-IT", 12),
    ("管理部", "ORG-ADMIN", 12),
    ("人資部", "ORG-HR", 11),
)
EMPLOYMENT_TYPES = (("full_time", 85), ("part_time", 10), ("contractor", 5))

# 假名：常見姓氏＋常見名字用字隨機組合，不對應任何真人；不填 email、電話、地址
SURNAMES = "陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴周徐蘇葉莊呂江何蕭羅高"
GIVEN_FIRST = "宜家冠承品雅柏宇子思佳彥詠郁芷俊書沛采昱"
GIVEN_SECOND = "庭涵恩翔瑜安婷妤廷軒萱哲蓁潔宏霖寧均"


class HireError(c.ServiceError):
    """建立新人時需要人處理的問題（HRM 缺端點、缺部門、權限不足）。"""

    def __init__(self, message: str, kind: str):
        super().__init__(message, announce=True, kind=kind)


# --- HRM（session）------------------------------------------------------


def hrm_base() -> str:
    """HRM_API_BASE 寫 http://host:18000 或 …/api 都可以，統一成 …/api。"""
    base = c.env("HRM_API_BASE", DEFAULT_HRM_BASE).rstrip("/")
    return base if base.endswith("/api") else f"{base}/api"


def employment_path() -> str:
    path = c.env("HRM_EMPLOYMENT_PATH", DEFAULT_EMPLOYMENT_PATH)
    return "/" + path.strip("/") + "/"


class Hrm:
    """HRM session client（docs/agent-clock-api.md）：登入取得 sessionid、csrftoken，
    之後的寫入帶 X-CSRFToken。HRM 對員工、Employment 的建立沒有 Idempotency-Key，
    所以 POST 不在送出後重試；重跑時由狀態檔＋查詢既有資料避免重複建立。
    """

    def __init__(self, base: str):
        self.http = c.HttpClient(base, "HRM")
        self.me: dict = {}

    def login(self, username: str, password: str) -> dict:
        status, body = self.http.request(
            "POST", "/auth/login/", {"username": username, "password": password},
            idempotent=True,  # 登入重送沒有副作用
        )
        if status != 200 or not isinstance(body, dict):
            raise c.ServiceError(f"HRM 登入失敗 {status}: {c.short(body)}",
                                 announce=status in (400, 401, 403), kind="login_failed")
        self.me = body
        return body

    def get(self, path: str):
        return self.http.request("GET", path)

    def get_ok(self, path: str):
        status, body = self.get(path)
        if status != 200:
            raise c.ServiceError(f"HRM GET {path} 失敗 {status}: {c.short(body)}",
                                 announce=status in (401, 403))
        return body

    def post(self, path: str, body: dict):
        return self.http.request("POST", path, body,
                                 headers={"X-CSRFToken": self.http.cookie("csrftoken") or ""},
                                 idempotent=False)

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


def hrm_login(agent: str) -> Hrm:
    password = c.env("HRM_AGENT_PASSWORD")
    if not password:
        raise c.ConfigError("沒有設定 HRM_AGENT_PASSWORD，無法登入 HRM")
    hrm = Hrm(hrm_base())
    me = hrm.login(agent, password)
    if me.get("role") not in ("hr", "admin"):
        raise HireError(f"{agent} 在 HRM 的 role 是 {me.get('role')}，不是 hr，不能建立員工"
                        "（請先跑 HRM 的 seed_org_roles）", kind="not_hr_role")
    if not me.get("company"):
        raise HireError(f"{agent} 的 HRM 帳號沒有公司", kind="no_company")
    return hrm


def check_employment_api(hrm: Hrm) -> None:
    """建立員工前先確認 HRM 有建立 Employment 的端點，避免建出沒有到職日的半套新人。"""
    status, body = hrm.get(employment_path() + "?page_size=1")
    if status in (404, 405):
        raise HireError(
            f"HRM 沒有建立 Employment 的端點（GET {employment_path()} 回 {status}），"
            "沒有到職日的新人 onboarding-list 查不到，所以這次不建立。請先在 HRM 補上 Employment API"
            "（見 agent-hrm 的 references/api.md）", kind="employment_api_missing")
    if status != 200:
        raise c.ServiceError(f"HRM GET {employment_path()} 失敗 {status}: {c.short(body)}",
                             announce=status in (401, 403))


def find_department(hrm: Hrm, name: str, code: str) -> dict:
    rows = [r for r in hrm.paged("/departments/?page_size=200") if r.get("is_active", True)]
    for row in rows:
        if row.get("code") == code:
            return row
    for row in rows:
        if row.get("name") == name:
            return row
    raise HireError(f"HRM 找不到「{name}」（{code}），請先跑 HRM 的 seed_org_roles", kind="department_missing")


# --- 新人計畫 -----------------------------------------------------------


def weighted(choices) -> str:
    total = sum(weight for *_, weight in choices)
    roll = c.RNG.random() * total
    for *values, weight in choices:
        if roll < weight:
            return values[0]
        roll -= weight
    return choices[-1][0]


def fake_name() -> tuple:
    """(姓, 名)：兩字名，隨機組合，不對應任何真人。"""
    return c.RNG.choice(SURNAMES), c.RNG.choice(GIVEN_FIRST) + c.RNG.choice(GIVEN_SECOND)


def next_scan_monday(now: datetime) -> date:
    """hr-agent 下一次掃描（週一 09:00）的日期：今天是週一且還沒 09:00 就是今天，否則是下一個週一。"""
    today = now.date()
    if today.weekday() == SCAN_WEEKDAY and now < c.at(today, SCAN_TIME):
        return today
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def pick_start_date(now: datetime, *, catch_scan: bool) -> date:
    """到職日：某一週的週一～週五隨機。

    catch_scan=True（decide）：選「hr-agent 下一次掃描會查到」的那一週＝下一次掃描的下週。
    週一 09:00 前跑就是下週；之後才跑會變成下下週（不然新人會被漏掉，流程 ③ 不會啟動）。
    catch_scan=False（hire）：照字面選下週。
    """
    if catch_scan:
        monday = next_scan_monday(now) + timedelta(days=7)
    else:
        monday = week_monday(now.date()) + timedelta(days=7)
    return monday + timedelta(days=c.RNG.randrange(5))


def make_employee_number(start: date, last: str, first: str, salt) -> str:
    return f"NH-{start.strftime('%Y%m%d')}-{c.stable_key('new-hire', last, first, start, salt)[:4].upper()}"


def plan_hire(now: datetime, *, catch_scan: bool, department: str | None = None,
              start_date: date | None = None) -> dict:
    last, first = fake_name()
    dept_name = department or weighted(DEPARTMENTS)
    dept = next((d for d in DEPARTMENTS if d[0] == dept_name), None)
    if dept is None:
        raise c.ConfigError(f"部門只能是：{'、'.join(d[0] for d in DEPARTMENTS)}")
    start = start_date or pick_start_date(now, catch_scan=catch_scan)
    return {
        "last_name": last,
        "first_name": first,
        "full_name": f"{last}{first}",
        "department_name": dept[0],
        "department_code": dept[1],
        "start_date": start.isoformat(),
        "employment_type": weighted(EMPLOYMENT_TYPES),
        "employee_number": make_employee_number(start, last, first, c.iso(now)),
        "planned_at": c.iso(now),
    }


def scan_note(plan: dict, now: datetime) -> str | None:
    """新人趕不上 hr-agent 的掃描時，提醒 demo 要手動跑 onboarding-list。"""
    start = date.fromisoformat(plan["start_date"])
    if week_monday(start) == next_scan_monday(now) + timedelta(days=7):
        return None
    return (f"這位新人（{plan['start_date']} 到職）趕不上 hr-agent 週一 09:00 的 onboarding-list 掃描；"
            f"demo 時請手動跑：onboarding_list.py --agent hr-agent --week-of {plan['start_date']}")


# --- 建立（冪等）-------------------------------------------------------


def hires_path(agent: str) -> str:
    return c.state_path(SKILL, agent, "hires.json")


def find_employee(hrm: Hrm, plan: dict, department_id) -> dict | None:
    """用員工編號找已經建好的新人（回應遺失、重跑時沿用，不建第二位）。

    HRM 的員工 API 只有 DjangoFilterBackend（company、department、status、到職日），
    **沒有 search**：帶 ?search= 會被忽略、回整個公司的第一頁。所以用部門篩選＋大 page_size，
    把該部門翻完再比對員工編號（建立時一定帶部門）。
    """
    query = urllib.parse.urlencode({"department": department_id, "page_size": 1000})
    rows = hrm.paged(f"/employees/?{query}", max_pages=50)
    for row in rows:
        if row.get("employee_number") == plan["employee_number"]:
            return row
    return None


def create_hire(hrm: Hrm, entry: dict, persist) -> dict:
    """建立員工＋主要 Employment。entry 是狀態檔裡的一筆，每一步做完都寫回狀態檔。

    - 員工：先用員工編號查，有就沿用；沒有才 POST /api/employees/
    - Employment：員工詳情的 start_date 已經有值就視為建過；沒有才 POST /api/employments/
    """
    plan = entry["plan"]
    check_employment_api(hrm)
    if not entry.get("department_id"):
        dept = find_department(hrm, plan["department_name"], plan["department_code"])
        entry["department_id"] = dept["id"]
        persist()

    employee = None
    if entry.get("employee_id"):
        status, body = hrm.get(f"/employees/{entry['employee_id']}/")
        employee = body if status == 200 and isinstance(body, dict) else None
    if employee is None:
        employee = find_employee(hrm, plan, entry["department_id"])
    if employee is None:
        entry["status"] = "creating_employee"
        persist()
        status, body = hrm.post("/employees/", {
            "company": hrm.me["company"],
            "department": entry["department_id"],
            "employee_number": plan["employee_number"],
            "last_name": plan["last_name"],
            "first_name": plan["first_name"],
            "email": "",
            "phone": "",
            "status": "active",
        })
        if status != 201 or not isinstance(body, dict) or not body.get("id"):
            raise c.ServiceError(f"HRM 建立員工失敗 {status}: {c.short(body)}",
                                 announce=status in (400, 403), kind="create_employee_failed")
        employee = body
        entry["employee_created"] = True
    entry["employee_id"] = employee["id"]
    entry["status"] = "employee_created"
    persist()

    if not employee.get("start_date"):
        status, body = hrm.get(f"/employees/{employee['id']}/")
        if status == 200 and isinstance(body, dict):
            employee = body
    if not employee.get("start_date"):
        entry["status"] = "creating_employment"
        persist()
        status, body = hrm.post(employment_path(), {
            "employee": employee["id"],
            "employment_type": plan["employment_type"],
            "start_date": plan["start_date"],
            "is_primary": True,
        })
        # HRM 對內容相同的主要任職紀錄重送會回 200（冪等），也算成功
        if status not in (200, 201):
            raise c.ServiceError(f"HRM 建立 Employment 失敗 {status}: {c.short(body)}",
                                 announce=status in (400, 403, 404, 405), kind="create_employment_failed")
        entry["employment_created"] = True
        employee["start_date"] = plan["start_date"]
    entry["start_date"] = employee.get("start_date")
    entry["status"] = "done"
    entry["done"] = True
    persist()
    return entry


def hire_result(result: dict, entry: dict) -> None:
    plan = entry["plan"]
    result.update(
        employee_id=entry.get("employee_id"),
        employee_number=plan["employee_number"],
        name=plan["full_name"],
        department=plan["department_name"],
        start_date=entry.get("start_date") or plan["start_date"],
        employment_type=plan["employment_type"],
    )


# --- 子指令：decide -----------------------------------------------------


def cmd_decide(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    if c.AGENTS[agent]["role"] != "hr":
        result.update(outcome="not_hr", detail=f"{agent} 不是人資，不建立新人")
        return True
    if today.weekday() != 0 and not args.force:
        result.update(outcome="not_monday", detail="今天不是週一，不建立新人")
        return True

    path = c.state_path(SKILL, agent, f"{today.isoformat()}.json")
    state = c.load_json(path)

    def persist():
        if not args.dry_run:
            c.save_json(path, state)

    if state.get("decision") == "none" or (state.get("decision") == "hire" and state.get("done")):
        result.update(outcome="already_decided", decision=state["decision"],
                      employee_number=(state.get("plan") or {}).get("employee_number"),
                      detail=f"今天已經決策過了：{state['decision']}")
        return True

    if state.get("decision") == "hire":
        decision, roll = "hire", state.get("roll")
    else:
        roll = round(c.RNG.random(), 4)
        if args.force in ("hire", "none"):
            decision = args.force
        else:
            decision = "hire" if roll < HIRE_CHANCE else "none"
        state.update(decision=decision, roll=roll, decided_at=c.iso(now))
        if decision == "hire":
            state["plan"] = plan_hire(now, catch_scan=True, department=args.department)
    result["roll"] = roll

    if decision == "none":
        persist()
        result.update(outcome="no_hire", decision="none", detail="這週沒有新人要建立")
        return True

    plan = state["plan"]
    if args.dry_run:
        result.update(outcome="would_hire", decision="hire", plan=plan,
                      detail="dry-run：沒有連線 HRM，也沒有建立員工")
        return True

    persist()
    hrm = hrm_login(agent)
    entry = state.setdefault("hire", {"plan": plan, "status": "planned", "source": "decide"})

    def persist_entry():
        state["hire"] = entry
        persist()
        index = c.load_json(hires_path(agent))
        index[plan["employee_number"]] = entry
        c.save_json(hires_path(agent), index)

    create_hire(hrm, entry, persist_entry)
    state["done"] = True
    persist()
    hire_result(result, entry)
    note = scan_note(plan, now)
    if note:
        result["note"] = note
    result.update(outcome="hired", decision="hire",
                  detail=f"已在 HRM 建立新人 {plan['full_name']}（{plan['department_name']}，{plan['start_date']} 到職）")
    return True


# --- 子指令：hire -------------------------------------------------------


def cmd_hire(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    if c.AGENTS[agent]["role"] != "hr":
        result.update(outcome="not_hr", detail=f"{agent} 不是人資，不建立新人")
        return True
    start = date.fromisoformat(args.start_date) if args.start_date else None
    if start is not None and start.weekday() >= 5:
        raise c.ConfigError(f"到職日 {start.isoformat()} 是週末，請選工作日")
    index_path = hires_path(agent)
    index = c.load_json(index_path)

    # 冪等：同一個 --key 已經建好就回原本那位、沒做完就接著做；
    # 不帶 --key 時，上一次沒做完（回應遺失、中途失敗）的那位接著做，不重新抽
    if args.key:
        entry = next((e for e in index.values() if e.get("key") == args.key), None)
        if entry and entry.get("done"):
            hire_result(result, entry)
            result.update(outcome="already_hired", detail=f"--key {args.key} 已經建立過 {entry['plan']['full_name']}")
            return True
    else:
        entry = next((e for e in index.values()
                      if not e.get("done") and e.get("source") == "hire" and not e.get("key")), None)
    if entry is not None:
        result["resumed"] = True
    else:
        plan = plan_hire(now, catch_scan=False, department=args.department, start_date=start)
        entry = {"plan": plan, "status": "planned", "source": "hire", "key": args.key}

    plan = entry["plan"]
    if args.dry_run:
        result.update(outcome="would_hire", plan=plan, detail="dry-run：沒有連線 HRM，也沒有建立員工")
        return True

    def persist():
        index[plan["employee_number"]] = entry
        c.save_json(index_path, index)

    # 狀態檔在確認 HRM 有 Employment 端點、找到部門之後才寫（create_hire 裡）：
    # 前置檢查失敗時什麼都沒建，不留下要接著做的紀錄
    hrm = hrm_login(agent)
    create_hire(hrm, entry, persist)
    hire_result(result, entry)
    note = scan_note(plan, now)
    if note:
        result["note"] = note
    result.update(outcome="hired",
                  detail=f"已在 HRM 建立新人 {plan['full_name']}（{plan['department_name']}，{plan['start_date']} 到職）")
    return True


# --- 子指令：status -----------------------------------------------------


def cmd_status(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    hires = c.load_json(hires_path(agent))
    result.update(
        outcome="status",
        state=c.load_json(c.state_path(SKILL, agent, f"{today.isoformat()}.json")),
        hires=[{"employee_number": k, "name": v["plan"]["full_name"], "department": v["plan"]["department_name"],
                "start_date": v["plan"]["start_date"], "status": v.get("status")} for k, v in hires.items()],
        next_scan=next_scan_monday(now).isoformat(),
        detail="狀態查詢",
    )
    if not args.dry_run and c.AGENTS[agent]["role"] == "hr":
        hrm = hrm_login(agent)
        status, _ = hrm.get(employment_path() + "?page_size=1")
        result["hrm"] = {
            "role": hrm.me.get("role"),
            "company": hrm.me.get("company"),
            "employment_api": status == 200,
            "departments": [d[0] for d in DEPARTMENTS
                            if any(r.get("code") == d[1] or r.get("name") == d[0]
                                   for r in hrm.paged("/departments/?page_size=200"))],
        }
    return True


# --- main ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HRM 人資模擬（建立下週到職的新人）")
    parser.add_argument("command", choices=["decide", "hire", "status"])
    parser.add_argument("--agent", help="OpenClaw agent id（預設讀 OPENCLAW_AGENT_ID）")
    parser.add_argument("--dry-run", action="store_true", help="不連網、不寫入，只顯示決策")
    parser.add_argument("--date", help="覆寫今天的日期 YYYY-MM-DD（測試用）")
    parser.add_argument("--now", help="覆寫現在時間（ISO，測試用）")
    parser.add_argument("--seed", type=int, help="固定亂數種子（測試用）")
    parser.add_argument("--force", choices=["hire", "none"],
                        help="decide：跳過擲骰（也略過「只在週一」的檢查）")
    parser.add_argument("--department", choices=[d[0] for d in DEPARTMENTS], help="指定部門")
    parser.add_argument("--start-date", help="hire：到職日 YYYY-MM-DD（預設下週隨機一個工作日）")
    parser.add_argument("--key", help="hire：冪等鍵；同一個鍵重跑回傳已建立的那位，不會再建一位")
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
        if args.date:
            today = date.fromisoformat(args.date)
            if not args.now:
                now = c.at(today, now.strftime("%H:%M"))
        else:
            today = now.date()
        result.update(agent=agent, date=today.isoformat())
        if args.command == "decide":
            return cmd_decide(agent, today, now, args, result)
        if args.command == "hire":
            return cmd_hire(agent, today, now, args, result)
        return cmd_status(agent, today, now, args, result)

    c.run_main(handler, result)


if __name__ == "__main__":
    main()
