#!/usr/bin/env python3
"""流程 ① 步驟 1：找出下一個工作日的客戶拜訪，每筆啟動一條 visit 流程。

cron（工作日 08:00）叫 cs-agent 跑：

  1. CRM `GET /api/v1/follow-ups/?kind=visit&status=pending&due_after=&due_before=`
     期間＝明天 00:00 到「下一個工作日」隔天 00:00（週五跑 → 週六、週日、週一都涵蓋，
     不會因為用日曆日的「明天」而漏掉週一的拜訪）
  2. 每筆從標題「[拜訪] 客戶｜地點：…｜人數：n 人｜時長：m 分鐘｜目的：…」解析地點、人數、時長；
     格式不合、沒有負責業務的跳過並回報（announce）
  3. 每筆用共用 skill `flow-start` 啟動 visit 流程（input 照 controller/backend/docs/flows/visit.md）；
     去重交給 controller（去重鍵固定 crm:follow-up:<id>，重跑回 created=false）

課堂示範（P6，controller/deploy/demo）：`--date <今天> --demo --follow-up <id>` 只啟動指定那一筆，
input 帶 demo=true（controller 的示範模式：派車確認截止＝拜訪開始前 5 分鐘等，見 visit.md）。

一律輸出單一行 JSON；沒有拜訪時 announce=false，agent 安靜結束。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402
import cs_common as cs  # noqa: E402

WEEKDAYS = "一二三四五六日"


def scan_window(now, target: date | None) -> tuple:
    """回傳 (下一個工作日, 查詢開始, 查詢結束)。

    沒指定日期：從明天 00:00 查到下一個工作日的隔天 00:00（不含）。
    指定日期：只查那一天。
    """
    if target is not None:
        return target, c.at(target, "00:00"), c.at(target + timedelta(days=1), "00:00")
    today = now.date()
    next_workday = c.add_workdays(today, 1)
    start = c.at(today + timedelta(days=1), "00:00")
    end = c.at(next_workday + timedelta(days=1), "00:00")
    return next_workday, start, end


def day_label(day: date) -> str:
    return f"{day.month}/{day.day}（週{WEEKDAYS[day.weekday()]}）"


def followups_query(start, end) -> str:
    return "/v1/follow-ups/?" + urllib.parse.urlencode({
        "kind": "visit", "status": "pending",
        "due_after": c.iso(start), "due_before": c.iso(end),
    })


def build_input(followup: dict, parsed: dict, customer: dict | None, *, demo: bool = False) -> dict:
    """組 flow-start 的 input（欄位見 visit.md「啟動」）。demo=True 時帶 demo 標記（課堂示範模式）。"""
    start = c.parse_dt(followup.get("due_at"))
    end = start + timedelta(minutes=parsed["duration_minutes"])
    customer = customer or {}
    data = {
        "follow_up_id": followup["id"],
        "customer": {
            "name": customer.get("name") or parsed.get("customer_name"),
            "external_id": customer.get("external_customer_id") or None,
        },
        "visit_start_at": c.iso(start),
        "visit_end_at": c.iso(end),
        "location": parsed["location"],
        "seats": parsed["seats"],
        "assignee_hrm_employee_id": followup.get("assignee_hrm_employee_id"),
        "subject": parsed.get("subject"),
    }
    if demo:
        data["demo"] = True
    return data


def fetch_customer(crm: cs.Crm, cache: dict, customer_id) -> dict | None:
    """客戶名稱與 external_customer_id；讀不到就回 None（改用標題裡的客戶名稱）。"""
    if not customer_id:
        return None
    if customer_id not in cache:
        status, body = crm.get(f"/v1/customers/{urllib.parse.quote(str(customer_id))}/")
        cache[customer_id] = body if status == 200 and isinstance(body, dict) else None
    return cache[customer_id]


def start_flow(agent: str, flow_input: dict) -> dict:
    args = ["--type", "visit", "--input-file", "-",
            "--dedupe-key", f"crm:follow-up:{flow_input['follow_up_id']}", "--agent", agent]
    return cs.run_flow_skill("flow-start", "flow_start.py", args, flow_input)


def cmd_scan(args, result: dict) -> bool:
    agent = cs.resolve_agent(args.agent)
    now = c.now_tz(args.now)
    target = date.fromisoformat(args.date) if args.date else None
    day, start, end = scan_window(now, target)
    query = followups_query(start, end)
    result.update(agent=agent, target_date=day.isoformat(),
                  window={"due_after": c.iso(start), "due_before": c.iso(end)})

    if args.demo:
        result["demo"] = True
    if args.follow_up:
        result["follow_up_filter"] = args.follow_up

    if args.dry_run:
        scope = "" if target else "（含之前的非工作日）"
        only = f"（只處理待辦 {args.follow_up}）" if args.follow_up else ""
        mode = "，以示範模式啟動" if args.demo else ""
        result.update(outcome="would_scan", query=query,
                      detail=f"dry-run：會查 {day_label(day)}{scope}的待處理拜訪{only}{mode}，不連網")
        return True

    crm = cs.Crm()
    followups = crm.paged(query)
    if args.follow_up:
        # 課堂示範：只處理指定的那一筆，不碰同一天的其他（正式）拜訪
        followups = [fu for fu in followups if str(fu.get("id")) == str(args.follow_up)]
    started, existing, skipped, failed = [], [], [], []
    customers: dict = {}

    for fu in followups:
        fid = fu.get("id")
        title = fu.get("title")
        base = {"follow_up_id": fid, "title": title}
        if fu.get("kind") not in (None, "visit") or fu.get("status") not in (None, "pending"):
            continue  # 防呆：CRM 已經篩過
        try:
            parsed = cs.parse_visit_title(title)
        except cs.TitleError as exc:
            skipped.append({**base, "reason": f"標題格式不對：{exc}"})
            continue
        if c.parse_dt(fu.get("due_at")) is None:
            skipped.append({**base, "reason": f"due_at 格式不對：{fu.get('due_at')}"})
            continue
        if not fu.get("assignee_hrm_employee_id"):
            skipped.append({**base, "reason": "待辦沒有負責業務，或負責人在 CRM 沒有對應 HRM 員工"})
            continue
        customer = fetch_customer(crm, customers, fu.get("customer"))
        flow_input = build_input(fu, parsed, customer, demo=args.demo)
        if not flow_input["customer"]["name"]:
            skipped.append({**base, "reason": "查不到客戶名稱"})
            continue

        res = start_flow(agent, flow_input)
        out = res.get("result") or {}
        item = {"follow_up_id": fid, "customer": flow_input["customer"]["name"],
                "visit_start_at": flow_input["visit_start_at"]}
        if res["rc"] == 0 and out.get("ok") and out.get("flow_id"):
            item["flow_id"] = out["flow_id"]
            (started if out.get("created", True) else existing).append(item)
        else:
            item["error"] = out.get("error") or out.get("detail") or res.get("stderr") or f"rc={res['rc']}"
            failed.append(item)

    result.update(found=len(followups), started=started, existing=existing,
                  skipped=skipped, failed=failed)
    label = day_label(day)
    if not followups:
        if args.follow_up:
            result.update(outcome="no_visits",
                          detail=f"{label}找不到待處理的拜訪待辦 {args.follow_up}（日期、kind 或狀態不符）")
        else:
            result.update(outcome="no_visits", detail=f"{label}沒有要派車的客戶拜訪")
        return True
    parts = []
    if started:
        parts.append(f"新啟動 {len(started)} 條")
    if existing:
        parts.append(f"已在進行 {len(existing)} 條")
    if skipped:
        parts.append(f"跳過 {len(skipped)} 筆")
    if failed:
        parts.append(f"啟動失敗 {len(failed)} 筆")
    detail = f"{label}有 {len(followups)} 筆拜訪：" + "、".join(parts or ["沒有可處理的"])
    if skipped or failed:
        result.update(outcome="needs_attention", announce=True, announce_kind="attention", detail=detail)
        return not failed
    result.update(outcome="flows_started" if started else "already_started", detail=detail)
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="流程 ① 步驟 1：找出下一個工作日的拜訪並啟動 visit 流程")
    p.add_argument("--agent", help="agent id（預設 OPENCLAW_AGENT_ID，再預設 cs-agent）")
    p.add_argument("--date", help="只查這一天（YYYY-MM-DD，demo／補跑用）；不給就查下一個工作日")
    p.add_argument("--now", help="假裝現在是這個時間（ISO 8601，測試用）")
    p.add_argument("--dry-run", action="store_true", help="不連網，只顯示要查的期間")
    p.add_argument("--demo", action="store_true",
                   help="課堂示範：啟動的流程帶 demo=true（controller 示範模式，時限縮短）；請搭配 --date、--follow-up")
    p.add_argument("--follow-up", help="只處理這一筆 CRM 待辦跟進 id（課堂示範用）")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    result: dict = {"skill": "crm-visit-list"}
    c.run_main(lambda: cmd_scan(args, result), result)


if __name__ == "__main__":
    main()
