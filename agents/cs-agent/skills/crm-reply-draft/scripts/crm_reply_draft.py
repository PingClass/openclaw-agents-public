#!/usr/bin/env python3
"""流程 ② 步驟 5（draft_reply）：依客服主管的決定擬回覆客戶的草稿，交給 controller（不傳給客戶、不寫 CRM）。

controller 派來的步驟指令（controller/backend/docs/flows/refund.md「draft_reply」）原封不動交給 `--message-file`：

  1. 解析標頭與輸入（decision、amount_twd、manager_comment、previous_reply、max_chars……）
  2. 對照案件內部備註：CRM `GET /api/v1/cases/{case_id}/notes/?kind=decision`（case_note:read），
     最新一筆決定備註和輸入的決定、金額不一致 → needs_human（不自己挑一個金額）；
     讀不到（scope 還沒補發、CRM 連不上）只記在 tone_notes，照輸入擬稿
  3. 擬稿：預設用固定範本；主管退回重擬時，agent 依意見自己改寫後用 `--reply-file` 交進來
  4. 檢查（和 controller 一樣的規則，不符就不回報、請 agent 改稿）：
     - 同意退款：回覆裡的金額只能是 amount_twd（NT$1,200、$1200、1,200 元、1200 塊、新台幣 1200，全形也算）
     - 不退款：不得出現任何金額
     - 1000 字（max_chars）以內、不是空的；不提內部評估、推估、財會、主管、流程
  5. 輸出 flow-report 的 output（reply_text、tone_notes）；加 --report 時直接回報

不自行改金額：金額只來自主管的決定（輸入的 amount_twd）。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402
import cs_common as cs  # noqa: E402

STEP = "draft_reply"
MAX_CHARS = 1000
DECISION_LABELS = {"approve": "同意退款", "reject": "不退款", "escalate": "轉人工處理"}
# 回覆不能提到的內部字眼（refund.md：不提內部評估、推估金額、主管姓名或流程）
INTERNAL_WORDS = ("推估", "評估", "財會", "主管", "流程", "controller", "備註", "內部", "flow_id", "核定")
# 中文數字寫的金額（「一千二百元」「三千塊」）controller 的檢查抓不到，這裡先擋：金額一律用阿拉伯數字寫
_CJK_AMOUNT = re.compile(r"[零〇一二兩三四五六七八九十百千萬]+\s*(?:元|塊)")


def _name_greeting(name) -> str:
    name = (name or "").strip() if isinstance(name, str) else ""
    if not name or cs.amounts_in(name) or len(name) > 30:
        return "您好，"
    return f"{name}您好，"


def template_reply(inp: dict) -> str:
    decision = inp.get("decision")
    noun = "補償" if inp.get("category") == "compensation" else "退款"
    greet = _name_greeting(inp.get("customer_name"))
    if decision == "approve":
        if noun == "補償":
            action = f"將提供您補償 {cs.twd(inp['amount_twd'])}"
        else:
            action = f"將為您辦理退款 {cs.twd(inp['amount_twd'])}"
        return (f"{greet}關於您先前反映的問題，我們已完成確認，{action}。"
                f"{noun}作業完成後會再通知您。造成您的不便，非常抱歉，也謝謝您耐心等候。")
    return (f"{greet}關於您先前反映的問題，我們已仔細確認相關資料，很抱歉這次無法提供{noun}。"
            "若您有其他想補充的資料（例如商品照片或購買資訊），歡迎直接回覆，我們會轉給專人了解。"
            "造成您的困擾，非常抱歉，謝謝您的體諒。")


def check_reply(text, inp: dict) -> list:
    """回傳問題清單（空的＝可以送）。規則和 controller app/flows/refund.py 的 _validate_reply 相同，另外擋內部字眼。"""
    problems = []
    max_chars = inp.get("max_chars") if isinstance(inp.get("max_chars"), int) else MAX_CHARS
    if not isinstance(text, str) or not text.strip():
        return ["回覆是空的"]
    if len(text) > max_chars:
        problems.append(f"回覆超過 {max_chars} 字（目前 {len(text)} 字）")
    amounts = cs.amounts_in(text)
    if inp.get("decision") == "approve":
        wrong = [a for a in amounts if a != inp.get("amount_twd")]
        if wrong:
            problems.append(f"回覆裡的金額 {wrong} 和主管決定的 {cs.twd(inp.get('amount_twd'))} 不一致；"
                            "金額只能寫主管決定的那一個")
    elif amounts:
        problems.append(f"這次不退款，回覆不得出現任何金額（找到 {amounts}）")
    cjk = _CJK_AMOUNT.findall(unicodedata.normalize("NFKC", text))
    if cjk:
        problems.append(f"回覆裡有中文數字寫的金額（{'、'.join(cjk)}）；金額只能用阿拉伯數字寫主管決定的那一個")
    norm = unicodedata.normalize("NFKC", text).lower()
    found = [w for w in INTERNAL_WORDS if w.lower() in norm]
    if found:
        problems.append(f"回覆不能提到內部資訊：{'、'.join(found)}")
    return problems


# --- 對照案件內部備註（C4）-------------------------------------------------


def latest_decision_note(case_id) -> tuple:
    """回傳 (最新的決定備註 | None, 讀不到的原因 | None)。讀不到不算錯，只記下來。"""
    try:
        crm = cs.Crm()
        status, body = crm.get(f"/v1/cases/{urllib.parse.quote(str(case_id))}/notes/?kind=decision")
    except c.ServiceError as exc:
        return None, str(exc)
    if status != 200 or not isinstance(body, dict):
        return None, f"CRM 回 {status}"
    rows = [r for r in body.get("results") or [] if r.get("kind") == "decision"]
    if not rows:
        return None, "案件還沒有決定備註"
    rows.sort(key=lambda r: r.get("created_at") or "")
    return rows[-1], None


def note_mismatch(note: dict, inp: dict) -> str | None:
    """決定備註第一行（refund.py decision_note_body）：`【客服主管決定】同意退款 NT$1,200（…；財會推估 NT$1,500）`。"""
    first = (note.get("body") or "").splitlines()[0] if note.get("body") else ""
    if "【客服主管決定】" not in first:
        return None  # 不是 controller 組的格式（例如人手動寫的），不比對
    label = DECISION_LABELS.get(inp.get("decision"))
    if label and label not in first:
        return f"案件的決定備註是「{c.short(first)}」，和指令的決定（{label}）不一致"
    if inp.get("decision") == "approve":
        head = re.split(r"[（(]", first, maxsplit=1)[0]  # 括號裡是財會推估，不比對
        amounts = cs.amounts_in(head)
        if amounts and amounts[0] != inp.get("amount_twd"):
            return (f"案件的決定備註金額 {cs.twd(amounts[0])} 和指令的 {cs.twd(inp.get('amount_twd'))} 不一致")
    return None


# --- 主流程 ---------------------------------------------------------------


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read()


def draft(job: dict, args) -> dict:
    """回傳 {status, error_kind, summary, output}；或 {"rewrite": …}（不回報，請 agent 改稿）。"""
    inp = job["input"]
    decision = inp.get("decision")
    if decision not in ("approve", "reject"):
        return {"status": "needs_human", "error_kind": None, "output": {},
                "summary": f"決定是「{inp.get('decision_label') or decision}」，不需要擬客戶回覆，交給人處理"}
    amount = inp.get("amount_twd")
    if decision == "approve" and (isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0):
        return {"status": "needs_human", "error_kind": None, "output": {},
                "summary": f"同意退款但沒有合法的核定金額（amount_twd={amount!r}），不自行決定金額"}

    notes = []
    note, why = latest_decision_note(inp.get("case_id"))
    if note is not None:
        mismatch = note_mismatch(note, inp)
        if mismatch:
            return {"status": "needs_human", "error_kind": None, "output": {}, "summary": mismatch}
    elif why:
        notes.append(f"未能對照案件決定備註（{c.short(why)}），依指令的決定擬稿")

    revising = bool(inp.get("manager_comment") or inp.get("previous_reply"))
    if args.reply_file:
        text = _read(args.reply_file).strip()
        source = "agent"
    elif revising and not args.use_template:
        return {"rewrite": {
            "manager_comment": inp.get("manager_comment"),
            "previous_reply": inp.get("previous_reply"),
            "suggested_reply": template_reply(inp),
        }}
    else:
        text = template_reply(inp)
        source = "template"
    problems = check_reply(text, inp)
    if problems:
        return {"invalid": problems, "reply_text": text}

    label = DECISION_LABELS[decision] + (f" {cs.twd(amount)}" if decision == "approve" else "")
    tone = [f"依決定（{label}）{'以範本擬稿' if source == 'template' else '擬稿'}"]
    if revising:
        tone.append(f"已依退回意見修改：{c.short(inp.get('manager_comment') or '（未說明）')}")
    if args.tone_notes:
        tone.append(args.tone_notes.strip())
    tone += notes
    return {
        "status": "ok",
        "error_kind": None,
        "summary": f"已擬好回覆「{inp.get('customer_name') or inp.get('external_customer_id') or '客戶'}」的草稿（{label}）",
        "output": {"reply_text": text, "tone_notes": "；".join(tone)[:500]},
    }


def cmd_draft(args, result: dict) -> bool:
    agent = cs.resolve_agent(args.agent)
    job = cs.load_step_job(args, step=STEP, flow_type="refund", skill="crm-reply-draft",
                           required_inputs=("case_id", "decision"))
    inp = job["input"]
    result.update(agent=agent, flow_id=job["flow_id"], step=STEP, attempt=job.get("attempt"),
                  case_id=inp.get("case_id"), decision=inp.get("decision"), amount_twd=inp.get("amount_twd"))
    if args.dry_run:
        text = _read(args.reply_file).strip() if args.reply_file else template_reply(inp)
        result.update(outcome="would_draft", reply_text=text, problems=check_reply(text, inp),
                      detail="dry-run：不連網、不回報")
        return True

    res = draft(job, args)
    if "rewrite" in res:
        result.update(outcome="needs_rewrite", reported=False, **res["rewrite"],
                      detail=("客服主管退回重擬：依 manager_comment 改寫 previous_reply（金額規則不變），"
                              "寫進檔案後加 --reply-file 再跑一次"))
        return False
    if "invalid" in res:
        result.update(outcome="invalid_reply", reported=False, problems=res["invalid"], reply_text=res["reply_text"],
                      detail="回覆草稿不符規則（見 problems），改好再用 --reply-file 重跑；沒有回報 controller")
        return False
    return cs.finish_step(args, job, agent, res, result, "drafted")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="流程 ② 步驟 5：依客服主管決定擬回覆客戶的草稿")
    p.add_argument("--message-file", help="controller 的步驟指令全文（檔案路徑，或 - 代表 stdin）")
    p.add_argument("--flow", help="沒有步驟指令時：flow_id")
    p.add_argument("--attempt", type=int, help="沒有步驟指令時：attempt")
    p.add_argument("--input-file", help="沒有步驟指令時：輸入 JSON（檔案或 -）")
    p.add_argument("--reply-file", help="自己寫的回覆（退回重擬時用）；不給就用範本")
    p.add_argument("--use-template", action="store_true", help="退回重擬時仍用範本（主管意見與內容無關時）")
    p.add_argument("--tone-notes", help="給主管看的補充（不會送給客戶）")
    p.add_argument("--report", action="store_true", help="做完直接用 flow-report 回報")
    p.add_argument("--agent", help="agent id（預設 OPENCLAW_AGENT_ID，再預設 cs-agent）")
    p.add_argument("--dry-run", action="store_true", help="不連網：只顯示草稿與檢查結果")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    result: dict = {"skill": "crm-reply-draft"}
    c.run_main(lambda: cmd_draft(args, result), result)


if __name__ == "__main__":
    main()
