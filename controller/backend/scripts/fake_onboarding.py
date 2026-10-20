"""假 agent 跑流程 ③（onboarding）：由 scripts/fake_agents.py --flow onboarding 呼叫。

扮演的角色：
- hr-agent：啟動流程（onboarding-list 的角色）、步驟 5 onboarding_notice
- it-agent：步驟 2 draft_laptop
- IT 人員（souei）：讀收件匣的 IT 任務、送 laptop_assigned
- admin-agent：步驟 4 book_room
- 部門主管（預設 main）：從收件匣確認收到到職清單
- 人資（shion）：最後讀收件匣（部門沒有主管時會收到「流程停下」的通知）

只用標準函式庫；HTTP、log、確認、回報沿用 fake_agents.py 的函式（以參數傳入，避免循環 import）。
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

TERMINAL = {"succeeded", "failed", "expired", "cancelled"}


def _next_monday(today) -> datetime:  # noqa: ANN001
    days = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days)


def run(fa: SimpleNamespace, args: argparse.Namespace) -> int:
    api = fa.Api(args.base_url)
    records = {r["agent_id"]: r for r in json.loads(fa.IDENTITIES_FILE.read_text(encoding="utf-8"))["identities"]}
    manager_ids = [r["hrm_employee_id"] for r in records.values()
                   if r.get("role") == "manager" and r.get("department") == args.department]

    start_date = (datetime.fromisoformat(args.start_date).date() if args.start_date
                  else _next_monday(datetime.now(fa.TPE).date()))
    employee_id = args.employee_id or f"fake-new-{random.randint(100000, 999999)}"
    flow = api.call("hr-agent", "POST", "/flows", {
        "type": "onboarding",
        "input": {
            "employee_id": employee_id,
            "name": "新人甲（假資料）",
            "employee_number": f"E{random.randint(10000, 99999)}",
            "department_id": args.department_id,
            "department_name": args.department,
            "job_title": "專員",
            "start_date": start_date.isoformat(),
        },
    })
    flow_id = flow["id"]
    fa.log("hr-agent", f"啟動流程 {flow_id}「{flow['title']}」（employee_id={employee_id}，created={flow['created']}）")

    last_stuck: str | None = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        detail = api.call("hr-agent", "GET", f"/flows/{flow_id}")
        if detail["status"] in TERMINAL:
            break
        name = detail["current_step"]
        step = next(s for s in detail["steps"] if s["name"] == name)
        stuck = detail.get("stuck") or {}
        if stuck.get("code") and stuck.get("code") != last_stuck:
            fa.log("controller", f"卡住：{stuck['reason']}")
        last_stuck = stuck.get("code")
        if step["status"] == "failed":
            # 例如部門沒有主管：流程停下、通知 HR，等人處理
            for notice in api.call(args.hr, "GET", "/inbox")["items"]:
                if notice["flow_id"] == flow_id:
                    fa.log(args.hr, f"收件匣：{notice['title']}｜{notice['body']}")
            return 2
        if name == args.stall_at:
            fa.log("fake_agents", f"故意不回報「{step['title']}」（{step['assignee']}）。流程 {flow_id}")
            return 0
        if step["status"] not in ("dispatched", "running", "waiting_approval"):
            time.sleep(args.poll)
            continue

        if name == "draft_laptop":
            fa.report(api, "it-agent", flow_id, step, {
                "asset_code": "LT-FAKE-003", "asset_name": "假筆電 14 吋", "specs": "i5／16GB／512GB",
                "reason": "未指派中規格最新的一台", "alternatives": ["LT-FAKE-004"], "available_count": 2,
            }, "建議配發 LT-FAKE-003")
        elif name == "await_laptop":
            tasks = [n for n in api.call(args.it, "GET", "/inbox", params={"unread_only": "true"})["items"]
                     if n["kind"] == "task" and n["flow_id"] == flow_id]
            if not tasks:
                time.sleep(args.poll)
                continue
            fa.log(args.it, f"收件匣任務：{tasks[0]['title']}")
            suggestion = api.call(args.it, "GET", f"/flows/{flow_id}")["state"]["steps"]["draft_laptop"]
            # （真的 souei 會在這裡用 session 呼叫 ADM A4 指派）
            api.call(args.it, "POST", f"/flows/{flow_id}/events",
                     {"type": "laptop_assigned", "asset_code": suggestion["asset_code"]})
            fa.log(args.it, f"已在 ADM 指派 {suggestion['asset_code']}，回報 laptop_assigned")
        elif name == "book_room":
            room_input = step["input"]["input"]
            fa.report(api, "admin-agent", flow_id, step, {
                "booking_id": random.randint(1000, 9999), "room_id": 2, "room_name": "假會議室 B",
                "start_at": room_input["preferred_start_at"], "end_at": room_input["preferred_end_at"],
                "created": True,
            }, "已代訂到職說明會議室")
        elif name == "onboarding_notice":
            fa.report(api, "hr-agent", flow_id, step, {
                "manager_hrm_employee_ids": manager_ids,
                "checklist": f"{start_date.isoformat()} 09:30 到職說明；筆電 LT-FAKE-003；第一週由主管帶領熟悉業務（假資料）",
                "items": ["到職說明", "筆電", "第一週安排"],
            }, f"HRM 部門主管 {len(manager_ids)} 位，已產生到職清單")
        elif name == "ack_manager":
            if not fa.decide(api, args.manager, flow_id, "ack", "收到，謝謝"):
                time.sleep(args.poll)
        else:
            time.sleep(args.poll)
    else:
        fa.log("fake_agents", f"{args.timeout} 秒內沒有跑完，流程 {flow_id}")
        return 1

    detail = api.call("hr-agent", "GET", f"/flows/{flow_id}")
    fa.log("controller", f"流程 {flow_id} 結束：{detail['status']}")
    for step in detail["steps"]:
        fa.log("controller", f"  {step['name']:<18} {step['status']:<10} {step['summary'] or ''}")
    return 0 if detail["status"] == "succeeded" else 1
