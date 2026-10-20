#!/usr/bin/env python3
"""流程 ③ 步驟 1：查下週到職的新人，每位啟動一條 onboarding 流程。

cron（每週一 09:00）叫 hr-agent 跑：

  1. HRM `GET /api/employees/?start_date_after=<下週一>&start_date_before=<下週日>&status=active`
     （service token，scope employee:read；含邊界，以主要 Employment 的到職日為準；有分頁）
  2. 每位新人用共用 skill `flow-start --type onboarding` 啟動流程（input 照
     controller/backend/docs/flows/onboarding.md「啟動」）；去重交給 controller
     （去重鍵固定 hrm:employee:<id>，重跑回 created=false）
  3. 沒有部門的新人跳過並回報（controller 要靠部門找主管）

一律輸出單一行 JSON；沒有新人時 announce=false，agent 安靜結束。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402
import hr_common as hr  # noqa: E402

WEEKDAYS = "一二三四五六日"


def next_week(today: date) -> tuple:
    """下週一到下週日（含）。週一跑 → 7 天後的週一；週日跑 → 隔天的週一。"""
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=6)


def week_of(day: date) -> tuple:
    """指定日期所在那一週的週一到週日（demo、補跑用）。"""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def day_label(day: date) -> str:
    return f"{day.month}/{day.day}（週{WEEKDAYS[day.weekday()]}）"


def employees_query(start: date, end: date) -> str:
    return "/employees/?" + urllib.parse.urlencode({
        "start_date_after": start.isoformat(),
        "start_date_before": end.isoformat(),
        "status": "active",
    })


def build_input(emp: dict) -> dict:
    """flow-start 的 input（onboarding.md「啟動」）。只放流程需要的欄位，不放電話、地址。

    HRM token 不回職稱，所以不帶 job_title（選填）。
    """
    return {
        "employee_id": emp["id"],
        "name": emp.get("full_name"),
        "employee_number": emp.get("employee_number"),
        "department_id": emp.get("department"),
        "department_name": emp.get("department_name"),
        "start_date": emp.get("start_date"),
    }


def skip_reason(emp: dict, start: date, end: date) -> str | None:
    for field, label in (("id", "員工 id"), ("full_name", "姓名"), ("employee_number", "員工編號")):
        if not emp.get(field):
            return f"HRM 資料缺少{label}"
    if not emp.get("department") or not emp.get("department_name"):
        return "HRM 沒有設定部門（找不到部門主管），請人資在 HRM 補上部門"
    try:
        day = date.fromisoformat(str(emp.get("start_date")))
    except ValueError:
        return f"到職日格式不對：{emp.get('start_date')}"
    if not start <= day <= end:
        return f"到職日 {day.isoformat()} 不在 {start.isoformat()}～{end.isoformat()}"
    return None


def start_flow(agent: str, flow_input: dict) -> dict:
    args = ["--type", "onboarding", "--input-file", "-",
            "--dedupe-key", f"hrm:employee:{flow_input['employee_id']}", "--agent", agent]
    return hr.run_flow_skill("flow-start", "flow_start.py", args, flow_input)


def cmd_scan(args, result: dict) -> bool:
    agent = hr.resolve_agent(args.agent)
    now = c.now_tz(args.now)
    if args.week_of:
        start, end = week_of(date.fromisoformat(args.week_of))
    else:
        start, end = next_week(now.date())
    query = employees_query(start, end)
    result.update(agent=agent, window={"start_date_after": start.isoformat(),
                                       "start_date_before": end.isoformat()})
    label = f"{day_label(start)}～{day_label(end)}"

    if args.dry_run:
        result.update(outcome="would_scan", query=f"GET /api{query}",
                      detail=f"dry-run：會查 {label} 到職的新人，不連網")
        return True

    employees = hr.Hrm().paged(query)
    started, existing, skipped, failed = [], [], [], []
    for emp in employees:
        base = {"employee_id": emp.get("id"), "employee_number": emp.get("employee_number"),
                "name": emp.get("full_name")}
        reason = skip_reason(emp, start, end)
        if reason:
            skipped.append({**base, "reason": reason})
            continue
        flow_input = build_input(emp)
        res = start_flow(agent, flow_input)
        out = res.get("result") or {}
        item = {**base, "department_name": flow_input["department_name"],
                "start_date": flow_input["start_date"]}
        if res["rc"] == 0 and out.get("ok") and out.get("flow_id"):
            item["flow_id"] = out["flow_id"]
            (started if out.get("created", True) else existing).append(item)
        else:
            item["error"] = out.get("error") or out.get("detail") or res.get("stderr") or f"rc={res['rc']}"
            failed.append(item)

    result.update(found=len(employees), started=started, existing=existing,
                  skipped=skipped, failed=failed)
    if not employees:
        result.update(outcome="no_new_hires", detail=f"{label}沒有新人到職")
        return True
    parts = []
    if started:
        parts.append(f"新啟動 {len(started)} 條")
    if existing:
        parts.append(f"已在進行 {len(existing)} 條")
    if skipped:
        parts.append(f"跳過 {len(skipped)} 位")
    if failed:
        parts.append(f"啟動失敗 {len(failed)} 位")
    detail = f"{label}有 {len(employees)} 位新人到職：" + "、".join(parts or ["沒有可處理的"])
    if skipped or failed:
        result.update(outcome="needs_attention", announce=True, announce_kind="attention", detail=detail)
        return not failed
    result.update(outcome="flows_started" if started else "already_started", detail=detail)
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="流程 ③ 步驟 1：查下週到職的新人並啟動 onboarding 流程")
    p.add_argument("--agent", help="agent id（預設 OPENCLAW_AGENT_ID，再預設 hr-agent）")
    p.add_argument("--week-of", help="改查這一天所在那一週（週一～週日，YYYY-MM-DD；demo／補跑用）")
    p.add_argument("--now", help="假裝現在是這個時間（ISO 8601，測試用）")
    p.add_argument("--dry-run", action="store_true", help="不連網，只顯示要查的期間")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    result: dict = {"skill": "onboarding-list"}
    c.run_main(lambda: cmd_scan(args, result), result)


if __name__ == "__main__":
    main()
