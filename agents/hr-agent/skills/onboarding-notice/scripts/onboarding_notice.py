#!/usr/bin/env python3
"""流程 ③ 步驟 5（onboarding_notice）：查部門主管，產生給主管的到職清單。

controller 派來的步驟指令（見 controller/backend/docs/flows/onboarding.md「onboarding_notice」）
原封不動從 stdin 或檔案交給這個腳本：

  1. 解析標頭（flow_id、step、attempt）與「輸入（JSON）：」後面的 JSON
  2. HRM `GET /api/departments/{department_id}/managers/`（service token，scope department:read）
     ——查到幾位就回報幾位；查不到是 `[]`，仍然 status=ok，**不自己挑人**（由 controller 停下並通知 HR）
  3. 產生到職清單（姓名、員工編號、部門、職稱、到職日、筆電、到職說明、主管第一週要做的事），
     **不含電話、地址、email**
  4. 輸出 flow-report 要的 status／summary／output；加 --report 時直接呼叫共用 skill flow-report 回報

只讀 HRM、不寫任何系統，所以不需要 Idempotency-Key；重派時重查 HRM 即可。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402
import hr_common as hr  # noqa: E402

STEP = "onboarding_notice"
FLOW_TYPE = "onboarding"
WEEKDAYS = "一二三四五六日"
REDACTED = "［已移除］"

# 防線：清單文字裡不該出現電話、地址。輸入只來自 controller，但姓名、會議室名稱這類自由文字還是過濾一次。
PHONE_PATTERN = re.compile(
    r"(?<![\dA-Za-z])(?:\+?886[\s\-]?\d{1,2}|\(0\d{1,2}\)|0\d{1,3})[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}(?!\d)"
)
ADDRESS_PATTERN = re.compile(
    r"[一-鿿]{0,8}(?:路|街|大道)(?:[一二三四五六七八九十\d]+段)?(?:\d+巷)?(?:\d+弄)?\d+(?:之\d+)?號(?:\d+樓)?"
)


class StepError(Exception):
    """這一步的結果不是 ok：帶 flow-report 的 status 與 error_kind。"""

    def __init__(self, message: str, status: str = "needs_human", error_kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.error_kind = error_kind


# --- 讀取指令 -------------------------------------------------------------


def load_job(args) -> dict:
    if args.message_file:
        if args.message_file == "-":
            text = sys.stdin.read()
        else:
            with open(os.path.expanduser(args.message_file), encoding="utf-8") as fh:
                text = fh.read()
        job = hr.parse_step_message(text)
    else:
        if not (args.flow and args.input_file):
            raise c.ConfigError("請給 --message-file（controller 的步驟指令），或 --flow 加 --input-file")
        if args.input_file == "-":
            data = json.load(sys.stdin)
        else:
            with open(os.path.expanduser(args.input_file), encoding="utf-8") as fh:
                data = json.load(fh)
        job = {"flow_id": args.flow, "flow_type": FLOW_TYPE, "step": STEP, "attempt": args.attempt,
               "skill": "onboarding-notice", "keys": {}, "input": data}

    if not job.get("flow_id"):
        raise c.ConfigError("步驟指令缺少 flow_id；沒有 flow_id 的流程指令一律不執行")
    if job.get("step") != STEP:
        raise c.ConfigError(f"這個 skill 只處理 {STEP}，指令的 step 是 {job.get('step') or '（沒有）'}")
    if job.get("flow_type") not in (None, FLOW_TYPE):
        raise c.ConfigError(f"這個 skill 只處理 onboarding 流程，指令的 flow_type 是 {job['flow_type']}")
    inp = job["input"]
    if not isinstance(inp, dict):
        raise c.ConfigError("輸入不是 JSON 物件")
    if department_id(inp) in (None, ""):
        raise c.ConfigError("輸入缺少 department_id（HRM 部門 id）")
    return job


def department_id(inp: dict):
    value = inp.get("department_id")
    if value in (None, ""):
        value = (inp.get("new_hire") or {}).get("department_id")
    return value


# --- HRM ------------------------------------------------------------------


def managers_path(dept) -> str:
    return f"/departments/{urllib.parse.quote(str(dept), safe='')}/managers/"


def fetch_managers(dept) -> list:
    """HRM H4：部門主管 [{employee_id, employee_number, full_name, email}]；沒有主管回 []。"""
    try:
        hrm = hr.Hrm()
        status, body = hrm.get(managers_path(dept))
    except c.ConfigError as exc:
        raise StepError(str(exc)) from exc            # 缺 HRM_API_TOKEN：要人處理
    except c.ServiceError as exc:
        raise StepError(f"連不上 HRM：{exc}", status="failed", error_kind="system") from exc
    if status == 200 and isinstance(body, list):
        return [m for m in body if isinstance(m, dict) and m.get("employee_id")]
    if status == 404:
        raise StepError(f"HRM 找不到部門 {dept}（404），請人資確認新人的部門")
    if status in (401, 403):
        raise StepError(f"HRM 拒絕（{status}），檢查 HRM_API_TOKEN 與 department:read scope：{c.short(body)}")
    if status >= 500 or status == 429:
        raise StepError(f"HRM 回 {status}", status="failed", error_kind="system")
    raise StepError(f"HRM 查部門主管回 {status}：{c.short(body)}")


# --- 到職清單 -------------------------------------------------------------


def clean(text) -> str:
    """一行文字：去掉換行，遮掉像電話、地址的片段。"""
    if text is None:
        return ""
    value = " ".join(str(text).split())
    value = PHONE_PATTERN.sub(REDACTED, value)
    return ADDRESS_PATTERN.sub(REDACTED, value)


def date_label(value) -> str:
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return clean(value) or "未提供"
    return f"{day.isoformat()}（週{WEEKDAYS[day.weekday()]}）"


def time_range(start_at, end_at) -> str:
    start = c.parse_dt(start_at)
    end = c.parse_dt(end_at)
    if not start:
        return "時間未定"
    text = f"{start.strftime('%Y-%m-%d')} {start.strftime('%H:%M')}"
    if end:
        text += f"–{end.strftime('%H:%M')}" if end.date() == start.date() else f"–{end.strftime('%m/%d %H:%M')}"
    return text


def build_checklist(inp: dict, managers: list) -> tuple:
    """回傳 (checklist 全文, items)。只放流程需要的欄位；不放電話、地址、email。"""
    hire = inp.get("new_hire") or {}
    name = clean(hire.get("name")) or "新人"
    dept = clean(hire.get("department_name")) or "未提供"
    laptop = inp.get("laptop") or {}
    orientation = inp.get("orientation") or {}
    hr_person = inp.get("hr") or {}
    hr_name = clean(hr_person.get("display_name")) or "人資"

    asset_code = clean(laptop.get("asset_code"))
    asset_name = clean(laptop.get("asset_name"))
    laptop_text = f"{asset_code}（{asset_name}）" if asset_code and asset_name else (asset_code or "尚未指派")

    room = clean(orientation.get("room_name"))
    when = time_range(orientation.get("start_at"), orientation.get("end_at"))
    orientation_text = f"{when}，{room}（主持：{hr_name}）" if room else "尚未預約"

    if not managers:
        manager_text = f"HRM 查不到{dept}的部門主管（已交由人資處理）"
    else:
        manager_text = "、".join(clean(m.get("full_name")) or clean(m.get("employee_number")) or "（未具名）"
                                for m in managers)

    items = [
        f"姓名：{name}",
        f"員工編號：{clean(hire.get('employee_number')) or '未提供'}",
        f"部門：{dept}",
        f"職稱：{clean(hire.get('job_title')) or '未提供'}",
        f"到職日：{date_label(hire.get('start_date'))}",
        f"筆電：{laptop_text}",
        f"到職說明：{orientation_text}",
        f"部門主管：{manager_text}",
        f"人資窗口：{hr_name}",
    ]
    first_week = [
        f"到職第一天參加到職說明，向{name}介紹部門與團隊成員",
        f"確認{name}已領到筆電" + (f" {asset_code}" if asset_code else "") + "，並向 IT 申請工作需要的系統帳號與權限",
        "指派一位工作夥伴（buddy），安排第一週的工作內容與教育訓練",
        "第一週結束前和新人面談一次；清單有缺漏請在確認時選「有缺漏」並說明，會轉給人資",
    ]
    items += [f"主管第一週：{task}" for task in first_week]

    lines = [f"新人到職清單：{name}（{dept}，{date_label(hire.get('start_date'))} 到職）", ""]
    lines += [f"- {item}" for item in items[:9]]
    lines += ["", "主管第一週要做的事："]
    lines += [f"{i}. {task}" for i, task in enumerate(first_week, 1)]
    comment = clean(inp.get("manager_comment"))
    if comment:
        lines += ["", f"主管上次的意見：{comment}"]
    lines += ["", "本清單不含電話、地址等個人聯絡資料；需要時請洽人資。"]
    return "\n".join(lines), items


# --- 主流程 ---------------------------------------------------------------


def notice(job: dict) -> dict:
    """回傳 flow-report 用的 {status, error_kind, summary, output, managers}。"""
    inp = job["input"]
    dept = department_id(inp)
    hire = inp.get("new_hire") or {}
    dept_name = clean(hire.get("department_name")) or f"部門 {dept}"
    try:
        managers = fetch_managers(dept)
    except StepError as exc:
        return {"status": exc.status, "error_kind": exc.error_kind, "summary": str(exc)[:300],
                "output": {"department_id": dept}, "managers": []}

    ids = [str(m["employee_id"]) for m in managers]
    checklist, items = build_checklist(inp, managers)
    names = "、".join(clean(m.get("full_name")) or str(m["employee_id"]) for m in managers)
    if not ids:
        summary = f"HRM 查不到{dept_name}的部門主管，到職清單已產生，由 controller 通知人資"
    elif len(ids) > 1:
        summary = f"HRM 的{dept_name}有 {len(ids)} 位主管（{names}），到職清單已產生，由 controller 決定確認者"
    else:
        summary = f"已產生{clean(hire.get('name')) or '新人'}的到職清單，部門主管：{names}"
    return {
        "status": "ok",
        "error_kind": None,
        "summary": summary[:300],
        "output": {"manager_hrm_employee_ids": ids, "checklist": checklist, "items": items},
        "managers": [{"hrm_employee_id": str(m["employee_id"]), "name": clean(m.get("full_name"))}
                     for m in managers],
    }


def report(job: dict, agent: str, step_result: dict) -> dict:
    args = ["--flow", job["flow_id"], "--step", STEP, "--status", step_result["status"],
            "--summary", step_result["summary"], "--output-file", "-", "--agent", agent]
    if step_result.get("error_kind"):
        args += ["--error-kind", step_result["error_kind"]]
    if step_result["status"] != "ok":
        args += ["--error", step_result["summary"]]
    if job.get("attempt") is not None:
        # 帶上指令的 attempt：controller 已重派時，過期的回報會被拒（409 stale_attempt）
        args += ["--attempt", str(job["attempt"])]
    return hr.run_flow_skill("flow-report", "flow_report.py", args, step_result["output"])


def cmd_notice(args, result: dict) -> bool:
    agent = hr.resolve_agent(args.agent)
    job = load_job(args)
    dept = department_id(job["input"])
    result.update(agent=agent, flow_id=job["flow_id"], step=STEP, attempt=job.get("attempt"),
                  department_id=dept)
    if args.dry_run:
        result.update(outcome="would_notice",
                      requests=[f"GET /api{managers_path(dept)}"],
                      detail="dry-run：不連網、不回報")
        return True

    step_result = notice(job)
    expected = ((job["input"].get("expected_manager") or {}).get("hrm_employee_id"))
    ids = step_result["output"].get("manager_hrm_employee_ids") or []
    result.update(report_status=step_result["status"], error_kind=step_result["error_kind"],
                  summary=step_result["summary"], output=step_result["output"],
                  managers=step_result["managers"],
                  matches_expected=bool(expected) and ids == [str(expected)])
    if step_result["status"] == "ok":
        result["outcome"] = "notice_ready" if ids else "no_manager"
    else:
        result["outcome"] = f"step_{step_result['status']}"
    result["detail"] = step_result["summary"]

    if not args.report:
        result["next"] = ("用 flow-report 回報：--flow {f} --step {s} --status {st}{ek}{at} "
                          "--summary <summary> --output-file <output 存成的檔案>").format(
            f=job["flow_id"], s=STEP, st=step_result["status"],
            ek=f" --error-kind {step_result['error_kind']}" if step_result["error_kind"] else "",
            at=f" --attempt {job['attempt']}" if job.get("attempt") is not None else "")
        return step_result["status"] == "ok"

    res = report(job, agent, step_result)
    out = res.get("result") or {}
    if res["rc"] == 0 and out.get("ok"):
        result.update(reported=True, flow_status=out.get("flow_status"))
        return True
    result.update(reported=False, announce=True, announce_kind="error",
                  report_error=out.get("error") or out.get("detail") or res.get("stderr") or f"rc={res['rc']}",
                  detail=f"{step_result['summary']}；但 flow-report 回報失敗，請重跑本指令（只讀 HRM，重跑安全）")
    return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="流程 ③ 步驟 5：查部門主管、產生到職清單")
    p.add_argument("--message-file", help="controller 的步驟指令全文（檔案路徑，或 - 代表 stdin）")
    p.add_argument("--flow", help="沒有步驟指令時：flow_id")
    p.add_argument("--attempt", type=int, help="沒有步驟指令時：attempt")
    p.add_argument("--input-file", help="沒有步驟指令時：輸入 JSON（檔案或 -）")
    p.add_argument("--report", action="store_true", help="做完直接用 flow-report 回報")
    p.add_argument("--agent", help="agent id（預設 OPENCLAW_AGENT_ID，再預設 hr-agent）")
    p.add_argument("--dry-run", action="store_true", help="不連網：只解析指令、顯示會查的端點")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    result: dict = {"skill": "onboarding-notice"}
    c.run_main(lambda: cmd_notice(args, result), result)


if __name__ == "__main__":
    main()
