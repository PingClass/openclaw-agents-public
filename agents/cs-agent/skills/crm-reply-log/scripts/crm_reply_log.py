#!/usr/bin/env python3
"""流程 ② 步驟 7（log_reply）：controller 已把主管核准的回覆送給客戶，cs-agent 在 CRM 留紀錄。

controller 派來的步驟指令（controller/backend/docs/flows/refund.md「log_reply」）原封不動交給 `--message-file`：

  1. CRM `POST /api/v1/interactions/`（Idempotency-Key.interaction）：記一筆對外回覆，
     摘要第一行 `[回覆客戶]`、註明案件 id，內容就是已送出的 reply_text；external_message_id＝`<flow_id>:reply`。
     **不寫內部評估、推估金額或主管決定的備註**
  2. CRM `POST /api/v1/follow-ups/`（Idempotency-Key.follow_up）：建後續待辦，kind／due_at／title 用輸入的
     follow_up，並關聯 case_id
  3. 輸出 flow-report 的 output（interaction_id、follow_up_id，UUID 字串）；加 --report 時直接回報

不傳訊息給客戶（controller 已經送了）、不結案（case:transition 只給人）。
冪等：兩個寫入各用指令給的鍵，重派時重送同樣內容，CRM 會重播第一次的結果。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402
import cs_common as cs  # noqa: E402

STEP = "log_reply"
CATEGORY_LABELS = {"refund": "退款", "compensation": "補償"}
FOLLOW_UP_KINDS = ("general", "visit")


def reply_summary(job: dict) -> str:
    """互動摘要：只用指令的輸入，重試時一字不差（CRM 冪等比對 payload）。"""
    inp = job["input"]
    sent = c.parse_dt(inp.get("sent_at"))
    when = sent.astimezone(c.TZ).strftime("%Y-%m-%d %H:%M") if sent else "時間未知"
    category = CATEGORY_LABELS.get(inp.get("category"), inp.get("category") or "客訴")
    return "\n".join([
        f"[回覆客戶] {when} 通路：{inp.get('channel') or '未知'}",
        f"案件：{inp.get('case_id')}（{category}）",
        f"內容：{inp.get('reply_text')}",
    ])


def find_interaction(crm: cs.Crm, customer_id, external_id: str) -> dict | None:
    rows = crm.paged("/v1/interactions/?" + urllib.parse.urlencode({"customer": customer_id}))
    for row in rows:
        if row.get("external_message_id") == external_id:
            return row
    return None


def record_interaction(crm: cs.Crm, job: dict) -> tuple:
    inp = job["input"]
    external_id = f"{job['flow_id']}:reply"
    payload = {"customer": inp["customer_id"], "summary": reply_summary(job), "external_message_id": external_id}
    status, body = crm.post("/v1/interactions/", payload, job["keys"]["interaction"])
    if status in (200, 201) and isinstance(body, dict) and body.get("id"):
        return str(body["id"]), status == 201
    if status == 400 and "external_message_id" in cs.body_text(body):
        # 同一條流程的回覆已經記過（例如之前用別的鍵送過）：沿用那一筆，不重記
        row = find_interaction(crm, inp["customer_id"], external_id)
        if row and row.get("id"):
            return str(row["id"]), False
    raise cs.classify_crm(status, body, "記錄回覆互動")


def follow_up_payload(inp: dict) -> dict:
    fu = inp.get("follow_up") or {}
    kind = fu.get("kind") or "general"
    if kind not in FOLLOW_UP_KINDS:
        raise cs.StepError(f"follow_up.kind「{kind}」不是 CRM 的選項（general／visit）")
    if not fu.get("due_at") or c.parse_dt(fu.get("due_at")) is None:
        raise cs.StepError(f"follow_up.due_at 格式不對：{fu.get('due_at')!r}")
    title = (fu.get("title") or "").strip() or f"追蹤案件回覆：{inp.get('customer_name') or '客戶'}"
    return {"customer": inp["customer_id"], "case": inp["case_id"], "title": title[:200],
            "due_at": fu["due_at"], "kind": kind}


def create_follow_up(crm: cs.Crm, job: dict, payload: dict) -> tuple:
    status, body = crm.post("/v1/follow-ups/", payload, job["keys"]["follow_up"])
    if status in (200, 201) and isinstance(body, dict) and body.get("id"):
        return str(body["id"]), status == 201
    raise cs.classify_crm(status, body, "建立後續待辦")


def log_reply(job: dict) -> dict:
    inp = job["input"]
    name = inp.get("customer_name") or inp.get("external_customer_id") or "客戶"
    base = {"case_id": inp.get("case_id")}
    try:
        if not isinstance(inp.get("reply_text"), str) or not inp["reply_text"].strip():
            raise cs.StepError("輸入沒有 reply_text（controller 送出的回覆），無法記錄")
        fu_payload = follow_up_payload(inp)
        crm = cs.Crm()
        interaction_id, i_created = record_interaction(crm, job)
        follow_up_id, f_created = create_follow_up(crm, job, fu_payload)
    except cs.StepError as exc:
        return {"status": exc.status, "error_kind": exc.error_kind, "summary": str(exc)[:300], "output": base}
    except c.ConfigError as exc:
        return {"status": "needs_human", "error_kind": None, "summary": str(exc)[:300], "output": base}
    except c.ServiceError as exc:
        return {"status": "failed", "error_kind": "system", "summary": f"連不上 CRM：{exc}"[:300], "output": base}
    note = "" if (i_created and f_created) else "（重送，沿用既有紀錄）"
    return {
        "status": "ok",
        "error_kind": None,
        "summary": f"已在 CRM 記錄回覆「{name}」並建立後續待辦（{fu_payload['due_at'][:10]}）{note}",
        "output": {"interaction_id": interaction_id, "follow_up_id": follow_up_id},
    }


def cmd_log(args, result: dict) -> bool:
    agent = cs.resolve_agent(args.agent)
    job = cs.load_step_job(
        args, step=STEP, flow_type="refund", skill="crm-reply-log", write_keys=("interaction", "follow_up"),
        key_overrides={"interaction": args.key_interaction, "follow_up": args.key_follow_up},
        required_inputs=("case_id", "customer_id"))
    result.update(agent=agent, flow_id=job["flow_id"], step=STEP, attempt=job.get("attempt"),
                  case_id=job["input"].get("case_id"))
    if args.dry_run:
        result.update(
            outcome="would_log",
            interaction_summary=reply_summary(job),
            requests=[f"POST /api/v1/interactions/（Idempotency-Key: {job['keys']['interaction']}）",
                      f"POST /api/v1/follow-ups/（Idempotency-Key: {job['keys']['follow_up']}）"],
            detail="dry-run：不連網、不寫入、不回報",
        )
        return True
    return cs.finish_step(args, job, agent, log_reply(job), result, "logged")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="流程 ② 步驟 7：CRM 記回覆互動、建後續待辦")
    p.add_argument("--message-file", help="controller 的步驟指令全文（檔案路徑，或 - 代表 stdin）")
    p.add_argument("--flow", help="沒有步驟指令時：flow_id")
    p.add_argument("--attempt", type=int, help="沒有步驟指令時：attempt")
    p.add_argument("--input-file", help="沒有步驟指令時：輸入 JSON（檔案或 -）")
    p.add_argument("--key-interaction", help="覆寫 Idempotency-Key.interaction（必須是指令給的值）")
    p.add_argument("--key-follow-up", help="覆寫 Idempotency-Key.follow_up（必須是指令給的值）")
    p.add_argument("--report", action="store_true", help="做完直接用 flow-report 回報")
    p.add_argument("--agent", help="agent id（預設 OPENCLAW_AGENT_ID，再預設 cs-agent）")
    p.add_argument("--dry-run", action="store_true", help="不連網：只顯示會寫入的互動摘要")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    result: dict = {"skill": "crm-reply-log"}
    c.run_main(lambda: cmd_log(args, result), result)


if __name__ == "__main__":
    main()
