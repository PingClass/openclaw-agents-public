#!/usr/bin/env python3
"""流程 ① 步驟 8（close_visit）：在 CRM 記一筆拜訪互動，並完成拜訪待辦。

controller 派來的步驟指令（見 controller/backend/docs/flows/visit.md「派工指令格式」）原封不動
從 stdin 或檔案交給這個腳本：

  1. 解析標頭（flow_id、step、attempt、Idempotency-Key.interaction、Idempotency-Key.complete）
     與「輸入（JSON）：」後面的 JSON
  2. CRM `GET /api/v1/follow-ups/{follow_up_id}/` 取得客戶 UUID
  3. CRM `POST /api/v1/interactions/`（Idempotency-Key.interaction）：摘要照 CRM docs 的 C5 格式，
     第一行 `[拜訪]`，不寫報帳金額；external_message_id＝`<flow_id>:visit`
  4. CRM `POST /api/v1/follow-ups/{follow_up_id}/complete/`（Idempotency-Key.complete）
  5. 輸出 flow-report 要的 status／summary／output；加 --report 時直接呼叫共用 skill flow-report 回報

冪等：兩個寫入各用指令給的鍵，重試（同一個 attempt 或 controller 重派）重送同樣內容，
CRM 會重播第一次的結果，不會重複記互動或因「待辦已完成」失敗。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402
import cs_common as cs  # noqa: E402

STEP = "close_visit"
StepError = cs.StepError


def load_job(args) -> dict:
    return cs.load_step_job(
        args, step=STEP, flow_type="visit", skill="crm-visit-close",
        write_keys=("interaction", "complete"),
        key_overrides={"interaction": args.key_interaction, "complete": args.key_complete},
        required_inputs=("follow_up_id",))


# --- 拜訪摘要（CRM C5）---------------------------------------------------


def visit_summary(inp: dict) -> str:
    """CRM docs「kind=visit 的互動摘要格式建議（C5）」。

    內容只用 controller 給的輸入，**重試時一字不差**（CRM 冪等比對 payload）。
    不寫報帳金額、報帳狀態，也不寫電話地址。
    """
    start = c.parse_dt(inp.get("visit_start_at"))
    end = c.parse_dt(inp.get("visit_end_at"))
    when = start.strftime("%Y-%m-%d %H:%M") if start else "時間未知"
    if start and end:
        when += f"-{end.strftime('%H:%M')}" if end.date() == start.date() else f"-{end.strftime('%m/%d %H:%M')}"
    sales = inp.get("sales") or {}
    sales_name = sales.get("display_name") or sales.get("hrm_employee_id") or "未知"
    subject = cs.one_line(inp.get("subject")) or "客戶拜訪"
    note = cs.one_line(inp.get("visit_note")) or "業務未填寫"
    distance = inp.get("distance_km")
    if isinstance(distance, (int, float)) and not isinstance(distance, bool):
        traffic = f"公務車 往返 {int(distance)} km（車牌與起迄里程見 ADM 預約）"
    else:
        traffic = "未記錄"
    return "\n".join([
        f"[拜訪] {when} 業務：{sales_name}",
        f"待辦：{inp.get('follow_up_id')}",
        f"目的：{subject}",
        "出席：未記錄",
        f"重點：{note}",
        "結論：見重點",
        "下一步：無",
        f"交通：{traffic}",
    ])


# --- CRM 寫入 -------------------------------------------------------------


def get_follow_up(crm: cs.Crm, follow_up_id) -> dict:
    path = f"/v1/follow-ups/{urllib.parse.quote(str(follow_up_id))}/"
    status, body = crm.get(path)
    if status != 200 or not isinstance(body, dict):
        raise cs.classify_crm(status, body, f"讀取待辦 {follow_up_id}")
    return body


def find_interaction(crm: cs.Crm, customer_id, external_id: str) -> dict | None:
    rows = crm.paged("/v1/interactions/?" + urllib.parse.urlencode({"customer": customer_id}))
    for row in rows:
        if row.get("external_message_id") == external_id:
            return row
    return None


def record_interaction(crm: cs.Crm, job: dict, customer_id, summary: str) -> tuple:
    external_id = f"{job['flow_id']}:visit"
    payload = {"customer": customer_id, "summary": summary, "external_message_id": external_id}
    status, body = crm.post("/v1/interactions/", payload, job["keys"]["interaction"])
    if status in (200, 201) and isinstance(body, dict) and body.get("id"):
        return body["id"], status == 201
    if status == 400 and "external_message_id" in cs.body_text(body):
        # 同一條流程的拜訪已經記過（例如之前用別的鍵送過）：沿用那一筆，不重記
        row = find_interaction(crm, customer_id, external_id)
        if row and row.get("id"):
            return row["id"], False
    raise cs.classify_crm(status, body, "記錄拜訪互動")


def complete_follow_up(crm: cs.Crm, job: dict, follow_up_id) -> tuple:
    path = f"/v1/follow-ups/{urllib.parse.quote(str(follow_up_id))}/complete/"
    status, body = crm.post(path, {}, job["keys"]["complete"])
    if status == 200 and isinstance(body, dict):
        return body.get("status") or "done", False
    if status == 400 and "already complete" in cs.body_text(body).lower():
        current = get_follow_up(crm, follow_up_id)
        if current.get("status") == "done":
            return "done", True
    raise cs.classify_crm(status, body, "完成待辦")


# --- 主流程 ---------------------------------------------------------------


def close_visit(job: dict) -> dict:
    """回傳 flow-report 用的 {status, error_kind, summary, output}。"""
    inp = job["input"]
    follow_up_id = inp["follow_up_id"]
    customer_name = (inp.get("customer") or {}).get("name") or "客戶"
    try:
        crm = cs.Crm()
        follow_up = get_follow_up(crm, follow_up_id)
        customer_id = follow_up.get("customer")
        if not customer_id:
            raise StepError(f"待辦 {follow_up_id} 沒有客戶")
        summary = visit_summary(inp)
        interaction_id, created = record_interaction(crm, job, customer_id, summary)
        fu_status, was_done = complete_follow_up(crm, job, follow_up_id)
    except StepError as exc:
        return {"status": exc.status, "error_kind": exc.error_kind,
                "summary": str(exc)[:300], "output": {"follow_up_id": follow_up_id}}
    except c.ConfigError as exc:
        # 例如沒有 CRM_API_TOKEN：要人處理，不讓 controller 自動重試
        return {"status": "needs_human", "error_kind": None,
                "summary": str(exc)[:300], "output": {"follow_up_id": follow_up_id}}
    except c.ServiceError as exc:
        # 連線失敗、逾時（已重試過）
        return {"status": "failed", "error_kind": "system",
                "summary": f"連不上 CRM：{exc}"[:300], "output": {"follow_up_id": follow_up_id}}
    note = "（重送，沿用既有紀錄）" if not created or was_done else ""
    return {
        "status": "ok",
        "error_kind": None,
        "summary": f"已在 CRM 記錄拜訪「{customer_name}」並完成待辦{note}",
        "output": {
            "interaction_id": interaction_id,
            "follow_up_id": follow_up_id,
            "follow_up_status": fu_status,
        },
    }


def cmd_close(args, result: dict) -> bool:
    agent = cs.resolve_agent(args.agent)
    job = load_job(args)
    result.update(agent=agent, flow_id=job["flow_id"], step=STEP, attempt=job.get("attempt"),
                  follow_up_id=job["input"].get("follow_up_id"))
    if args.dry_run:
        result.update(
            outcome="would_close",
            interaction_summary=visit_summary(job["input"]),
            requests=[
                f"GET /api/v1/follow-ups/{job['input']['follow_up_id']}/",
                f"POST /api/v1/interactions/（Idempotency-Key: {job['keys']['interaction']}）",
                f"POST /api/v1/follow-ups/{job['input']['follow_up_id']}/complete/"
                f"（Idempotency-Key: {job['keys']['complete']}）",
            ],
            detail="dry-run：不連網、不寫入、不回報",
        )
        return True
    return cs.finish_step(args, job, agent, close_visit(job), result, "closed")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="流程 ① 步驟 8：CRM 記拜訪互動並完成待辦")
    p.add_argument("--message-file", help="controller 的步驟指令全文（檔案路徑，或 - 代表 stdin）")
    p.add_argument("--flow", help="沒有步驟指令時：flow_id")
    p.add_argument("--attempt", type=int, help="沒有步驟指令時：attempt")
    p.add_argument("--input-file", help="沒有步驟指令時：輸入 JSON（檔案或 -）")
    p.add_argument("--key-interaction", help="覆寫 Idempotency-Key.interaction（必須是指令給的值）")
    p.add_argument("--key-complete", help="覆寫 Idempotency-Key.complete（必須是指令給的值）")
    p.add_argument("--report", action="store_true", help="做完直接用 flow-report 回報")
    p.add_argument("--agent", help="agent id（預設 OPENCLAW_AGENT_ID，再預設 cs-agent）")
    p.add_argument("--dry-run", action="store_true", help="不連網：只解析指令、顯示互動摘要")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    result: dict = {"skill": "crm-visit-close"}
    c.run_main(lambda: cmd_close(args, result), result)


if __name__ == "__main__":
    main()
