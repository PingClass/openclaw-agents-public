#!/usr/bin/env python3
"""流程 ② 步驟 4（controller 步驟 record_decision）：把客服主管的決定寫進 CRM 案件內部備註。

契約：controller/backend/docs/flows/refund.md 的「record_decision」。

1. 向 controller 核對這一步真的是派給 finance-agent 的、輸入（note_body 等）沒被改過
2. CRM `POST /api/v1/cases/{case_id}/notes/`，kind=decision，**body 一字不改用輸入的 note_body**，
   Idempotency-Key 用指令的值；同一段內容已經寫過就沿用，不重複寫
3. 輸出 flow-report 要的 status／summary／output（note_id）；加 --report 時直接呼叫 flow-report 回報

備註內容由 controller 組好，這個腳本不改寫決定；只寫備註，不改案件狀態、不結案、不聯絡客戶。
為什麼是 finance-agent：CRM scope 裡只有 finance-agent 有 case_note:write。
只用 Python 標準函式庫。

用法：
    refund_decision_note.py --instruction-file <controller 的步驟指令|-> --report [--dry-run] [--agent finance-agent]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import finance_flow as ff  # noqa: E402

STEP = "record_decision"
FLOW_TYPE = "refund"
NOTE_KIND = "decision"
# 寫入前和 controller 保存的輸入核對的欄位
VERIFY_FIELDS = ("case_id", "note_kind", "note_body", "decision", "amount_twd")
MAX_BODY = 5000


def check_input(inp: dict) -> None:
    if inp.get("case_id") in (None, "") or not isinstance(inp.get("case_id"), (str, int)):
        raise ff.StepFailure(ff.result("needs_human", "輸入缺少 case_id，無法寫入決定備註",
                                       {"reason": "input_missing", "missing": ["case_id"]}))
    if inp.get("note_kind") != NOTE_KIND:
        raise ff.StepFailure(ff.result(
            "needs_human", f"note_kind 必須是 {NOTE_KIND}（收到 {str(inp.get('note_kind'))[:20]}），不寫入",
            {"reason": "note_kind_invalid"}))
    body = inp.get("note_body")
    if not isinstance(body, str) or not body.strip():
        raise ff.StepFailure(ff.result("needs_human", "輸入缺少 note_body（controller 組好的決定內容），不寫入",
                                       {"reason": "input_missing", "missing": ["note_body"]}))
    if len(body) > MAX_BODY:
        raise ff.StepFailure(ff.result("needs_human", f"note_body 超過 {MAX_BODY} 字，不寫入，請人工確認",
                                       {"reason": "note_body_too_long"}))


def decision_text(inp: dict) -> str:
    label = str(inp.get("decision_label") or inp.get("decision") or "決定")[:20]
    amount = inp.get("amount_twd")
    if inp.get("decision") == "approve" and isinstance(amount, int) and not isinstance(amount, bool):
        return f"{label} NT${amount:,}"
    return label


def handle(ctx: dict, args) -> dict:
    inp = ctx["input"]
    check_input(inp)
    case_id = str(inp["case_id"])
    if args.dry_run:
        step = ff.result("ok", f"dry-run：不連網、不寫入、不回報（決定：{decision_text(inp)}）", {})
        step["plan"] = [
            "GET controller /api/flows/{flow_id}（核對步驟與輸入）",
            f"GET /api/v1/cases/{case_id}/notes/?kind=decision（同一段內容寫過就沿用）",
            f"POST /api/v1/cases/{case_id}/notes/ kind=decision（Idempotency-Key: {ctx['idempotency_key']}）",
        ]
        return step

    ff.verify_dispatch(ctx, VERIFY_FIELDS)
    crm = ff.Crm()
    body = inp["note_body"]  # 一字不改
    note, created = ff.write_note_once(crm, case_id, NOTE_KIND, body, ctx["idempotency_key"],
                                       lambda n: (n.get("body") or "") == body)
    again = "" if created else "（先前已寫過，沿用那一筆）"
    return ff.result("ok", f"已把客服主管的決定寫入案件內部備註{again}：{decision_text(inp)}",
                     {"note_id": str(note["id"])})


def main(argv=None) -> None:
    ff.run_step(STEP, FLOW_TYPE, "流程 ② record_decision：把客服主管的決定寫進 CRM 案件內部備註", handle, argv)


if __name__ == "__main__":
    main()
